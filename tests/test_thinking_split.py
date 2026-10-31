"""思考记录分表存储的测试。

核心约定：
- conversation_log 只存**干净正文**（要送进 prompt 的那份）
- thinking_log 存**思考过程**（只给用户看，绝不进 prompt）
- 用户可以通过 show_thinking 设置决定界面上展不展示

这三个约定一旦破功，prompt 就会被污染 —— 所以必须有测试守着。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class ThinkLLM:
    """会同时吐出 think 块和模仿前缀的假 LLM。"""

    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, **kwargs) -> str:
        self.calls += 1
        return (
            "<think>The user is asking about the interview. "
            "I should reference the note about 星海科技.</think>"
            "[2026-10-28 00:01:28] 宝宝，星海科技那边有消息了吗～"
        )

    def stream_chat(self, messages, **kwargs):
        self.calls += 1
        yield "<think>在想星海科技</think>宝宝，星海科技那边有消息了吗～"


def main() -> int:
    from companion_ever.config import AppConfig
    from companion_ever.bootstrap.app import build_app
    import companion_ever.bootstrap.app as app_mod
    from companion_ever.infrastructure.llm.traced_client import TracedLLMClient

    tmpdir = tempfile.mkdtemp(prefix="thinking_test_")
    llm = ThinkLLM()
    app_mod.create_llm_client = lambda cfg: TracedLLMClient(llm)

    cfg = AppConfig(
        db_path=os.path.join(tmpdir, "test.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "fake-model"},
        retrieval={"top_k": 10},
    )
    ctx = build_app(cfg)

    ctx.repos.user.create("alice")
    ctx.services.persona.create_persona(
        "alice", "苏苏",
        json.dumps({"name": "苏苏", "personality": ["温柔"]}, ensure_ascii=False),
    )

    # ---------- 跑一轮对话 ----------
    ctx.services.chat.chat("alice", "苏苏", "星海科技那边有消息了吗")

    # ---------- 1. conversation_log 里是干净正文 ----------
    msgs = ctx.services.conversation.get_recent_messages("alice", "苏苏", limit=10)
    assistant = [m for m in msgs if m.role == "assistant"]
    assert assistant, "没有 assistant 消息"
    body = assistant[-1].content
    assert "<think>" not in body, f"conversation_log 里混进了 think: {body[:80]}"
    assert not body.startswith("["), f"conversation_log 里混进了模仿前缀: {body[:60]}"
    assert "星海科技" in body, "正文丢了"
    print(f"[OK] conversation_log 只存干净正文: {body[:44]}")

    # ---------- 2. thinking_log 里存着思考过程 ----------
    tmap = ctx.services.conversation.get_thinking_map(
        user_id="alice", persona_id="苏苏", message_ids=[m.id for m in msgs],
    )
    assert tmap, "thinking_log 里没有思考记录"
    think = list(tmap.values())[0]
    assert "星海科技" in think, f"思考记录内容不对: {think[:80]}"
    assert "<think>" not in think, "thinking_log 里不该保留 <think> 标签本身"
    print(f"[OK] thinking_log 存了思考过程: {think[:56]}...")

    # ---------- 3. prompt 里绝不能有 think ----------
    r = ctx.services.prompt.build("alice", "苏苏", query="星海科技")
    whole = "".join(m["content"] for m in r["messages"])
    assert "<think>" not in whole, "prompt 里混进了 <think>"
    assert "The user is asking about the interview" not in whole, (
        "prompt 里混进了思考内容"
    )
    print("[OK] prompt 里没有 think")

    # ---------- 4. 设置能控制展示 ----------
    assert ctx.services.settings.get_show_thinking("alice", "苏苏") is True, (
        "默认应该是展示"
    )
    ctx.services.settings.set_show_thinking("alice", "苏苏", False)
    assert ctx.services.settings.get_show_thinking("alice", "苏苏") is False
    ctx.services.settings.set_show_thinking("alice", "苏苏", True)
    assert ctx.services.settings.get_show_thinking("alice", "苏苏") is True
    print("[OK] show_thinking 设置可读可写")

    # ---------- 5. 一键清空思考记录（隐私）----------
    n = ctx.services.conversation.delete_all_thinking(
        user_id="alice", persona_id="苏苏",
    )
    assert n == 1, f"应该删掉 1 条，实际 {n}"
    tmap2 = ctx.services.conversation.get_thinking_map(
        user_id="alice", persona_id="苏苏", message_ids=[m.id for m in msgs],
    )
    assert not tmap2, "清空后不该还有思考记录"
    # 正文不受影响
    msgs2 = ctx.services.conversation.get_recent_messages("alice", "苏苏", limit=10)
    assert "星海科技" in [m.content for m in msgs2 if m.role == "assistant"][0]
    print("[OK] 一键清空思考记录，正文不受影响")

    ctx.conn.close()
    print("[ALL] 思考记录分表存储测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
