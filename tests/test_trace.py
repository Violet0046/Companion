"""trace 诊断系统测试。

验证：
1. 一轮对话后 trace 文件被写到磁盘
2. 链路 steps 齐全（prompt.build / llm.chat / db.log_turn / invariants.check）
3. request 产物里能看到"模型实际收到的完整请求"
4. 不变量能抓到"历史取到了最早的 N 条而不是最近的 N 条"这个真实 bug
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class FakeLLM:
    """假 LLM —— 返回带 <think> 块的内容，模拟推理模型。"""

    model = "fake-model"

    def chat(self, messages, **kwargs) -> str:
        return "<think>心里想：她今天心情不错</think>嘿嘿，在想你呀"

    def stream_chat(self, messages, **kwargs):
        yield "嘿嘿，在想你呀"


def _build(tmpdir: str):
    from companion_ever.config import AppConfig
    from companion_ever.app import build_app
    import companion_ever.app as app_mod

    from companion_ever.infrastructure.ai.traced_client import TracedLLMClient

    # 与生产路径一致：create_llm_client 返回的是包了 trace 的客户端
    app_mod.create_llm_client = lambda cfg: TracedLLMClient(FakeLLM())

    cfg = AppConfig(
        db_path=os.path.join(tmpdir, "test.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "test-model"},
        retrieval={"always_include_types": ["profile_fact"], "top_k": 5},
    )
    return build_app(cfg)


def main() -> int:
    from companion_ever.infrastructure.trace import TraceStore, default_trace_dir

    tmpdir = tempfile.mkdtemp(prefix="trace_test_")
    ctx = _build(tmpdir)
    db_path = os.path.join(tmpdir, "test.db")
    store = TraceStore(default_trace_dir(db_path))

    # ---------- 1. 建用户 + 角色 ----------
    ctx.repos.user.create("alice")
    ctx.services.persona.create_persona(
        "alice", "舒舒",
        json.dumps({"name": "舒舒", "personality": ["温柔"]}, ensure_ascii=False),
    )

    # ---------- 2. 跑一轮对话 ----------
    resp = ctx.services.chat.chat("alice", "舒舒", "你在干嘛呢")
    assert "在想你" in resp, f"LLM 响应不对: {resp!r}"
    print("[OK] 对话跑通")

    # ---------- 3. trace 文件已落盘 ----------
    rows = store.list_traces(user_id="alice", limit=10)
    assert len(rows) == 1, f"期望 1 条 trace，实际 {len(rows)}"
    path, data = rows[0]
    assert data["kind"] == "chat"
    assert data["status"] == "ok", f"trace 状态异常: {data['status']}"
    print(f"[OK] trace 已落盘: {data['trace_id']}")

    # ---------- 4. 链路 steps 齐全 ----------
    names = [s["name"] for s in data["steps"]]
    for expected in ("prompt.build", "invariants.check", "llm.chat", "db.log_turn"):
        assert expected in names, f"缺少 step: {expected}（实际: {names}）"
    print(f"[OK] 链路 steps 齐全: {names}")

    # prompt.build 的细节
    pb = next(s for s in data["steps"] if s["name"] == "prompt.build")
    d = pb["detail"]
    assert d["system_chars"] > 0
    assert d["total_chars"] > 0
    print(f"[OK] prompt.build 细节: system={d['system_chars']} total={d['total_chars']}")

    # ---------- 5. request 产物 = 模型实际看到的 ----------
    req_path = store.artifact_path(path, "request")
    assert req_path is not None and req_path.exists(), "request 产物缺失"
    req = req_path.read_text(encoding="utf-8")
    assert "你在干嘛呢" in req, "request 产物里没有用户输入"
    assert "## 人设基础" in req, "request 产物里没有 system prompt"
    assert "[1] system" in req, "request 产物缺少 role 标记"
    assert "[2] user" in req, "request 产物缺少 user 消息"
    print(f"[OK] request 产物 {len(req):,} 字符，含 system prompt + 用户输入")

    # ---------- 6. response 产物 ----------
    resp_path = store.artifact_path(path, "response")
    assert resp_path is not None and resp_path.exists(), "response 产物缺失"
    print("[OK] response 产物存在")

    # ---------- 7. 不变量能抓到"历史取到最早的 N 条" ----------
    # 塞 60 条历史（超过 history_max_messages=50）
    for i in range(70):
        ctx.services.conversation.log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=100 + i, role="user", content=f"历史消息 {i}",
        )
    ctx.services.conversation.log_turn(
        user_id="alice", persona_id="舒舒",
        turn_index=999, role="user", content="这是最新的一句：今天天气不错",
    )

    ctx.services.chat.chat("alice", "舒舒", "现在几点")
    rows2 = store.list_traces(user_id="alice", limit=2)
    latest = rows2[0][1]
    inv = next(s for s in latest["steps"] if s["name"] == "invariants.check")
    violations = inv["detail"].get("violations") or []
    rules = {v["rule"] for v in violations}
    assert "history_is_recent" in rules, (
        "不变量没有抓到『历史取到了最早的 N 条』这个 bug —— "
        f"实际 violations: {violations}"
    )
    print(f"[OK] 不变量抓到了真实 bug: {sorted(rules)}")

    # ---------- 8. /quit 的抽取链路也被追踪 ----------
    ctx.services.session_close.close_session("alice", "舒舒")
    rows3 = store.list_traces(user_id="alice", kind="extraction", limit=5)
    assert len(rows3) >= 1, "抽取链路没有产生 trace"
    ext_path, ext_data = rows3[0]
    ext_names = [s_["name"] for s_ in ext_data["steps"]]
    for expected in ("db.log_session_close", "extraction.workflow"):
        assert expected in ext_names, f"抽取 trace 缺少 step: {expected}（实际 {ext_names}）"
    assert ext_data["steps"][0]["detail"].get("marker_id"), "抽取 trace 缺少 marker_id"
    print(f"[OK] /quit 抽取链路也被追踪: {ext_names}")

    ctx.conn.close()
    print("[ALL] trace 诊断系统测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
