"""自动 session 边界管理的测试。

核心问题：session 边界不能依赖用户记得按 /quit。
用户可能直接关掉 App、切后台、断网 —— 边界就没了，
摘要、抽取、上下文的边界全乱。

所以要自动判定：离上次说话超过阈值 → 上一个 session 算结束。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class FakeLLM:
    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, **kwargs) -> str:
        self.calls += 1
        return json.dumps({
            "facts": [],
            "blocks": [],
            "reasoning": "test",
        }, ensure_ascii=False)

    def stream_chat(self, messages, **kwargs):
        self.calls += 1
        yield "ok"


def main() -> int:
    from companion_ever.config import AppConfig
    from companion_ever.app import build_app
    import companion_ever.app as app_mod
    from companion_ever.infrastructure.ai.traced_client import TracedLLMClient

    tmpdir = tempfile.mkdtemp(prefix="idle_test_")
    llm = FakeLLM()
    app_mod.create_llm_client = lambda cfg: TracedLLMClient(llm)

    cfg = AppConfig(
        db_path=os.path.join(tmpdir, "test.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "fake-model"},
        idle_session_minutes=120,
        retrieval={"always_include_types": ["profile_fact"], "top_k": 10},
    )
    ctx = build_app(cfg)
    ctx.repos.user.create("alice")
    ctx.services.persona.create_persona(
        "alice", "苏苏", '{"name": "苏苏", "personality": ["温柔"]}',
    )

    sc = ctx.services.session_close

    # ---------- 1. 没有未关闭 session 时不动作 ----------
    assert sc.close_if_stale("alice", "苏苏", 120) is False
    print("[OK] 没有未关闭的 session 时不动作")

    # ---------- 2. 刚刚说过话 -> 不关 ----------
    ctx.services.conversation.log_turn(
        user_id="alice", persona_id="苏苏", turn_index=1,
        role="user", content="在吗",
    )
    assert sc.close_if_stale("alice", "苏苏", 120) is False
    assert sc.close_if_stale("alice", "苏苏", 0) is False, "0 应该禁用自动判定"
    print("[OK] 刚说过话 / 阈值为 0 -> 不关")

    # ---------- 3. 空闲超时 -> 自动关 ----------
    # 把最后一条消息的时间改到 3 小时前
    ctx.conn.execute(
        "UPDATE conversation_log SET created_at = datetime('now', '-3 hours') "
        "WHERE user_id = 'alice' AND role = 'user'"
    )
    before_markers = ctx.conn.execute(
        "SELECT COUNT(*) FROM conversation_log WHERE role = 'session_close'"
    ).fetchone()[0]

    assert sc.close_if_stale("alice", "苏苏", 120) is True, (
        "空闲 3 小时（阈值 2 小时）应该自动关闭 session"
    )

    # 后台线程会写 marker，等一下
    for _ in range(50):
        time.sleep(0.1)
        n = ctx.conn.execute(
            "SELECT COUNT(*) FROM conversation_log WHERE role = 'session_close'"
        ).fetchone()[0]
        if n > before_markers:
            break
    after_markers = ctx.conn.execute(
        "SELECT COUNT(*) FROM conversation_log WHERE role = 'session_close'"
    ).fetchone()[0]
    assert after_markers == before_markers + 1, (
        f"自动关闭应该写 1 个 marker（{before_markers} -> {after_markers}）"
    )
    print("[OK] 空闲超时 -> 自动写 session_close marker")

    # ---------- 4. 自动关闭后，active session 变空 ----------
    active = ctx.services.conversation.get_active_session("alice", "苏苏")
    assert active is None, f"自动关闭后不该还有 active session: {active}"
    print("[OK] 自动关闭后 active session 变空")

    # ---------- 5. 再调一次不会重复关 ----------
    assert sc.close_if_stale("alice", "苏苏", 120) is False
    print("[OK] 不会重复关闭")

    # ---------- 6. list_active_sessions 能列出未关闭的 ----------
    ctx.services.conversation.log_turn(
        user_id="alice", persona_id="苏苏", turn_index=99,
        role="user", content="我又回来了",
    )
    actives = ctx.services.conversation.list_active_sessions()
    assert any(a["user_id"] == "alice" for a in actives), (
        f"list_active_sessions 应该列出 alice: {actives}"
    )
    print(f"[OK] list_active_sessions 列出 {len(actives)} 个未关闭 session")

    ctx.conn.close()
    print("[ALL] 自动 session 边界管理测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
