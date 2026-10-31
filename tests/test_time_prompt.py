"""时间信息段与历史格式的测试。

两个约定：

1. `## 当前这一轮` 要说清楚"现在"、"隔了多久"、"本轮用户说的话"、"这一轮该怎么开场"
   —— 曾经的 bug：那一行"用户上次对话的起始时间"让模型以为现在还是凌晨，
   回答出错误的当前时间。

2. 只有 user 消息带时间戳，assistant 不带
   —— 实测：全部消息都带 -> 模型 2/2 会模仿并自己加时间戳；
      只有 user 带 -> 模型 0/2 模仿。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class FakeLLM:
    model = "fake-model"

    def chat(self, messages, **kwargs) -> str:
        return "好的呀～"

    def stream_chat(self, messages, **kwargs):
        yield "好的呀～"


def main() -> int:
    from companion_ever.config import AppConfig
    from companion_ever.bootstrap.app import build_app
    import companion_ever.bootstrap.app as app_mod
    from companion_ever.infrastructure.llm.traced_client import TracedLLMClient

    app_mod.create_llm_client = lambda cfg: TracedLLMClient(FakeLLM())

    tmpdir = tempfile.mkdtemp(prefix="timefmt_test_")
    cfg = AppConfig(
        db_path=os.path.join(tmpdir, "test.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "fake-model"},
        tz_offset_hours=8,
        retrieval={"top_k": 10},
    )
    ctx = build_app(cfg)
    ctx.repos.user.create("alice")
    ctx.services.persona.create_persona(
        "alice", "苏苏", '{"name": "苏苏", "personality": ["温柔"]}',
    )

    # 造几条历史
    for i in range(3):
        ctx.services.conversation.log_turn(
            user_id="alice", persona_id="苏苏", turn_index=1 + i,
            role="user", content=f"用户消息 {i}",
        )
        ctx.services.conversation.log_turn(
            user_id="alice", persona_id="苏苏", turn_index=1 + i,
            role="assistant", content=f"助手回复 {i}",
        )

    r = ctx.services.prompt.build("alice", "苏苏", query="在吗")
    system = r["messages"][0]["content"]
    history = r["messages"][1:]

    # ---------- 1. 时间段结构 ----------
    assert "## 当前这一轮" in system
    assert "**现在**" in system, "缺少『现在』"
    assert "用户上一次说话" in system, "缺少『用户上一次说话』"
    assert "**这一轮**" in system, "缺少『这一轮』"

    # 旧的那行误导性文案必须没了
    assert "上次对话的起始时间" not in system, (
        "『用户上次对话的起始时间』这行还在 —— 就是它让模型把现在说成凌晨"
    )
    assert "注意：这不是当前消息时间" not in system
    print("[OK] 时间段结构正确（现在 / 隔了多久 / 这一轮怎么开场）")

    # ---------- 1b. 本轮用户输入只出现一次，且只在 system 里 ----------
    marker = "本轮输入的独特标记"
    current_id = ctx.services.conversation.log_turn(
        user_id="alice", persona_id="苏苏", turn_index=9,
        role="user", content=marker,
    )
    r2 = ctx.services.prompt.build(
        "alice", "苏苏", query=marker, current_message_id=current_id)
    system2 = r2["messages"][0]["content"]
    msgs2 = r2["messages"]
    assert f"- **用户本轮说的话**：{marker}" in system2, (
        "本轮用户输入必须在 system 里显式标注——"
        "模型要回答哪一句不能靠猜（历史上曾出现两条连续 user 消息）"
    )
    # 只出现一次：system 里标注了，messages 里就不能再放一遍
    assert system2.count(marker) == 1, "本轮输入在 system 里出现了多次"
    assert not any(marker in m["content"] for m in msgs2[1:]), (
        "本轮输入又出现在 messages 里了——同一句话被送了两遍"
    )
    # 历史停在本轮之前
    assert msgs2[-1]["role"] == "assistant", "历史应以助手上一条回复结尾"
    print("[OK] 本轮用户输入只在 system 里出现一次，历史停在本轮之前")

    # ---------- 2. 历史格式：user 带时间戳，assistant 不带 ----------
    user_msgs = [m for m in history if m["role"] == "user"]
    asst_msgs = [m for m in history if m["role"] == "assistant"]
    assert user_msgs and asst_msgs

    for m in user_msgs:
        assert m["content"].startswith("["), f"user 消息应该带时间戳: {m['content'][:40]}"
    for m in asst_msgs:
        assert not m["content"].startswith("["), (
            f"assistant 消息不该带时间戳: {m['content'][:40]}"
        )
    print(f"[OK] 历史格式：user 带时间戳（{len(user_msgs)} 条）、assistant 不带（{len(asst_msgs)} 条）")

    # ---------- 3. 不同间隔 -> 不同的开场指引 ----------
    from datetime import datetime, timedelta, timezone

    ps = ctx.services.prompt

    def _utc_str(seconds_ago: int) -> str:
        t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        return t.strftime("%Y-%m-%d %H:%M:%S")

    # 连续（刚说完 1 分钟）
    sec_now = ps._render_time_section(_utc_str(60))
    # 很久以前（3 天前）
    sec_old = ps._render_time_section(_utc_str(3 * 86400))

    assert "连续" in sec_now, f"刚说完应该是连续: {sec_now}"
    assert "新的对话" in sec_old, f"隔很久应该是新对话: {sec_old}"
    assert "不要假装" in sec_old, "新对话时应该提醒不要假装刚聊过"
    print("[OK] 间隔感知：刚说完 -> 连续；隔 3 天 -> 新对话（提示不要假装刚聊过）")

    # 第一次说话（没有历史）
    sec_first = ps._render_time_section(None)
    assert "第一次说话" in sec_first, f"第一次说话应有说明: {sec_first}"
    print("[OK] 第一次说话有专门的引导")

    ctx.conn.close()
    print("[ALL] 时间信息段与历史格式测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
