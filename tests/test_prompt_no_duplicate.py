"""prompt_service 回归测试：历史上下文不能重复送入。

Bug 记录：
- 之前 build() 同时做两件事：
  ① 把历史渲染进 system_prompt 的 "## 对话历史" 段
  ② 把历史作为 role 消息追加到 messages 列表
- 结果：每条历史消息被送给 LLM 两次（浪费 token + LLM 可能误解为重复对话）

修复：历史只以原生 role 消息形式出现在 messages 里，system_prompt 不再渲染。

验证：
1. system_prompt 里不含任何历史消息内容
2. messages 里每条历史消息只出现一次
3. system_prompt 仍包含其余 5 段（时间 / 人设 / 状态 / 记忆 / 指引）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from companion_ever.config import AppConfig, LLMConfig
from companion_ever.app import build_app


def _make_ctx(db_path: str):
    cfg = AppConfig(
        db_path=db_path,
        llm=LLMConfig(api_key="x", base_url="x", model="x"),
    )
    return build_app(cfg)


def test_history_not_duplicated():
    """历史消息只送一次。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        ctx = _make_ctx(db_path)
        ctx.repos.user.create("alice")
        ctx.services.persona.create_persona(
            user_id="alice", persona_id="p1", definition='{"name": "舒舒"}',
        )

        # 写 2 轮对话（内容带唯一标记，方便检测）
        turns = [
            (1, "user", "UNIQUE_MSG_A_我叫周艺科"),
            (1, "assistant", "UNIQUE_MSG_B_你好周艺科"),
            (2, "user", "UNIQUE_MSG_C_我今天很累"),
            (2, "assistant", "UNIQUE_MSG_D_早点休息"),
        ]
        for turn, role, content in turns:
            ctx.services.conversation.log_turn(
                user_id="alice", persona_id="p1",
                turn_index=turn, role=role, content=content,
            )

        result = ctx.services.prompt.build("alice", "p1", query="")
        sp = result["system_prompt"]
        msgs = result["messages"]

        # ① system_prompt 里不能有历史内容
        for _, _, content in turns:
            assert content not in sp, (
                f"system_prompt 不应包含历史消息 '{content}'——"
                f"历史只应以 role 消息形式出现在 messages 里"
            )

        # ② messages 里每条历史只出现一次
        for _, _, content in turns:
            count = sum(1 for m in msgs if content in m["content"])
            assert count == 1, (
                f"历史消息 '{content}' 在 messages 里出现了 {count} 次，应为 1 次"
            )

        # ③ system_prompt 仍包含其余 5 段
        assert "## 当前时间信息" in sp
        assert "## 人设基础" in sp
        assert "## 你对用户的了解" in sp
        assert "## 行为指引" in sp
        assert "## 对话历史" not in sp, "system_prompt 不应再有 '## 对话历史' 段"

        print("[OK] 历史上下文没有重复送入")
        ctx.conn.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_history_appears_as_role_messages():
    """历史以原生 user/assistant role 消息形式出现（不是文本段）。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        ctx = _make_ctx(db_path)
        ctx.repos.user.create("alice")
        ctx.services.persona.create_persona(
            user_id="alice", persona_id="p1", definition='{"name": "舒舒"}',
        )
        ctx.services.conversation.log_turn(
            user_id="alice", persona_id="p1",
            turn_index=1, role="user", content="HIST_USER",
        )
        ctx.services.conversation.log_turn(
            user_id="alice", persona_id="p1",
            turn_index=1, role="assistant", content="HIST_ASSISTANT",
        )

        msgs = ctx.services.prompt.build("alice", "p1", query="")["messages"]

        # 第一个是 system，之后交替 user/assistant
        assert msgs[0]["role"] == "system"
        user_msg = next(m for m in msgs if "HIST_USER" in m["content"])
        assist_msg = next(m for m in msgs if "HIST_ASSISTANT" in m["content"])
        assert user_msg["role"] == "user"
        assert assist_msg["role"] == "assistant"

        # 时间戳保留（本地时区）
        assert user_msg["content"].startswith("["), "应保留时间戳前缀"

        print("[OK] 历史以原生 role 消息形式呈现")
        ctx.conn.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    test_history_not_duplicated()
    test_history_appears_as_role_messages()
    print("[ALL] prompt 重复历史回归测试通过")
