"""三个严重 bug 的回归测试。

1. get_messages_between 无边界时必须取**最近**的 N 条（不是最早的）
2. /quit 必须直接进 close 节点，**不能调 LLM**
3. 自然语言查询（"我最近面试怎么样了"）必须能召回到相关记忆

这三个 bug 都是"看不见"类型的——功能看起来正常，实际行为是错的。
所以必须有测试守着，否则改代码时很容易改回去。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class CountingLLM:
    """会记录调用次数的假 LLM。"""

    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list = []

    def chat(self, messages, **kwargs) -> str:
        self.calls += 1
        self.seen_messages.append(messages)
        return "<think>想一下</think>好的呀~"

    def stream_chat(self, messages, **kwargs):
        self.calls += 1
        self.seen_messages.append(messages)
        yield "好的呀~"


def _build(tmpdir: str, llm):
    from companion_ever.config import AppConfig
    from companion_ever.app import build_app
    import companion_ever.app as app_mod
    from companion_ever.infrastructure.ai.traced_client import TracedLLMClient

    app_mod.create_llm_client = lambda cfg: TracedLLMClient(llm)

    cfg = AppConfig(
        db_path=os.path.join(tmpdir, "test.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "fake-model"},
        retrieval={"always_include_types": ["profile_fact"], "top_k": 10},
    )
    return build_app(cfg)


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="bugfix_test_")
    llm = CountingLLM()
    ctx = _build(tmpdir, llm)

    ctx.repos.user.create("alice")
    ctx.services.persona.create_persona(
        "alice", "舒舒",
        json.dumps({"name": "舒舒", "personality": ["温柔"]}, ensure_ascii=False),
    )

    # ---------- Bug 1: 历史必须取最近的 ----------
    # 塞 80 条历史 + 1 条最新的
    for i in range(80):
        ctx.services.conversation.log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=10 + i, role="user", content=f"老消息 {i}",
        )
    ctx.services.conversation.log_turn(
        user_id="alice", persona_id="舒舒",
        turn_index=999, role="user", content="这是最新的一句：今天天气不错",
    )

    msgs = ctx.services.conversation.get_messages_between(
        user_id="alice", persona_id="舒舒", limit=50,
    )
    assert len(msgs) == 50, f"期望 50 条，实际 {len(msgs)}"
    last = msgs[-1].content
    assert "这是最新的一句" in last, (
        "get_messages_between 取到的不是最新消息 —— "
        f"最后一条是: {last[:60]}"
    )
    assert msgs[0].turn_index > 10, "取到的是最早的 50 条而不是最近的"
    print("[OK] Bug1: get_messages_between 取到的是最近的 50 条")

    # prompt 里也必须是最新的
    r = ctx.services.prompt.build("alice", "舒舒", query="最新")
    hist = r["messages"][1:]
    assert "这是最新的一句" in hist[-1]["content"], "prompt 里的历史不是最新的"
    print("[OK] Bug1: prompt 里的历史是最新的")

    # ---------- Bug 2: /quit 不能走 chat 节点（不产生"回复"）----------
    result = ctx.workflows.chat.invoke({
        "user_id": "alice",
        "persona_id": "舒舒",
        "user_input": "/quit",
        "next_turn_index": 1000,
    })
    assert result.get("close_report") is not None, "/quit 没有触发 close 节点"
    # chat 节点没跑 -> 结果里没有 response
    assert "response" not in result, (
        "/quit 仍然走了 chat 节点（返回了 response）—— 白跑了一次 LLM"
    )
    # 且没有任何一次 LLM 调用收到过"聊天用"的 system prompt
    chat_system_marker = "## 当前时间信息"
    chat_calls = [
        m for m in llm.seen_messages
        if m and isinstance(m[0], dict) and chat_system_marker in str(m[0].get("content", ""))
    ]
    assert not chat_calls, (
        f"/quit 触发了 {len(chat_calls)} 次聊天 LLM 调用（应该 0 次）"
    )
    # /quit 本身也不该被当成一句话写进对话日志
    quit_rows = ctx.conn.execute(
        "SELECT COUNT(*) FROM conversation_log WHERE content = '/quit'"
    ).fetchone()[0]
    assert quit_rows == 0, f"/quit 被写进 conversation_log 了（{quit_rows} 条）"
    print("[OK] Bug2: /quit 直接进 close 节点，0 次聊天 LLM 调用，不写日志")

    # ---------- Bug 3: 自然语言查询要能召回 ----------
    from companion_ever.domain.enums import NoteType

    ctx.services.note.create_note(
        user_id="alice", persona_id="舒舒",
        type=NoteType.EVENT,
        content="用户明天上午11点有一场字节跳动的面试",
        importance=0.9,
    )
    ctx.services.note.create_note(
        user_id="alice", persona_id="舒舒",
        type=NoteType.PREFERENCE,
        content="用户喜欢喝美式咖啡",
        importance=0.6,
    )

    hits = ctx.services.note.search_notes("alice", "舒舒", "我最近面试怎么样了", limit=10)
    assert len(hits) > 0, "自然语言查询零命中 —— 检索 bug 没修好"
    contents = " ".join(h.content for h in hits if isinstance(h.content, str))
    assert "面试" in contents, f"召回的内容不相关: {contents[:120]}"
    print(f"[OK] Bug3: 自然语言查询召回到 {len(hits)} 条相关记忆")

    # 短关键词也要能召回
    hits2 = ctx.services.note.search_notes("alice", "舒舒", "美式咖啡", limit=10)
    assert len(hits2) > 0, "短关键词查询零命中"
    print(f"[OK] Bug3: 短关键词查询召回到 {len(hits2)} 条")

    # ---------- Bug 4: 抽取失败必须回滚 marker（否则记忆静默丢数据）----------
    class FailingLLM(CountingLLM):
        def chat(self, messages, **kwargs):
            self.calls += 1
            raise TimeoutError("simulated LLM timeout")

    failing = FailingLLM()
    tmpdir2 = tmpdir + "_2"
    os.makedirs(tmpdir2, exist_ok=True)
    ctx2 = _build(tmpdir2, failing)
    ctx2.repos.user.create("bob")
    ctx2.services.persona.create_persona(
        "bob", "苏苏",
        json.dumps({"name": "苏苏", "personality": ["温柔"]}, ensure_ascii=False),
    )
    ctx2.services.conversation.log_turn(
        user_id="bob", persona_id="苏苏", turn_index=1,
        role="user", content="你好，我是来让你记住我的",
    )

    markers_before = ctx2.conn.execute(
        "select count(*) from conversation_log where role='session_close'"
    ).fetchone()[0]

    report = ctx2.services.session_close.close_session("bob", "苏苏")
    assert report.extraction.errors, "抽取失败应记录错误，而不是静默失败"
    assert "TimeoutError" in report.extraction.errors[0], (
        f"错误信息不对: {report.extraction.errors[0]}"
    )

    markers_after = ctx2.conn.execute(
        "select count(*) from conversation_log where role='session_close'"
    ).fetchone()[0]
    assert markers_after == markers_before, (
        f"抽取失败后 marker 没回滚（{markers_before} -> {markers_after}），"
        "下次抽取将跳过这段内容，记忆会静默丢数据"
    )
    assert report.marker_id == 0, "回滚后 marker_id 应标记为 0（表示未成功）"
    print("[OK] Bug4: 抽取失败记录错误 + 回滚 marker（不静默丢记忆）")

    ctx2.conn.close()
    ctx.conn.close()
    print("[ALL] 三个严重 bug 的回归测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
