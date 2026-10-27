"""application/chat/prompt_service.py — system prompt 拼装。

每轮对话必送的内容：
1. 当前时间（感知"刚才/昨天"等相对时间）— 已转成用户本地时区
2. persona_basics JSON（人设宪法）
3. 3 个 Block 内容（运行时状态、用户画像、关系进展）
4. 对话历史（跨 session 全部，按 history_max_messages cap）— 时间戳已转本地时区
5. 相关记忆（query-aware retrieval from notes）
6. 结尾指令

关键设计：
- Chat LLM 在对话过程中不应被 session_close 截断——
  历史消息跨 session 全部加载（但 cap 在 history_max_messages 防止 context 爆炸）。
- Extraction 工作流仍然只看 [最新 marker, 上一 marker] 之间的对话（按 session 划分）。
- DB 里 created_at 是 UTC（SQLite CURRENT_TIMESTAMP），prompt 渲染时统一转成本地时区。

依赖：
- authorizer: 权限校验
- block_service: 读 3 个 Block
- conversation_service: 读对话历史
- note_service: 读 persona_basics note
- retrieval_service: query 召回
- prompts.CHAT_FOOTER: 行为指引脚注
- utils.time.format_now
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from companion_ever.application.authz import Authorizer
from companion_ever.application.chat.conversation_service import ConversationService
from companion_ever.application.chat.retrieval_service import RetrievalService
from companion_ever.application.memory.note_service import NoteService
from companion_ever.application.persona.block_service import BlockService
from companion_ever.domain.enums import NoteType
from companion_ever.prompts import CHAT_FOOTER
from companion_ever.utils.time import format_now


def _utc_ts_to_local(ts_str: str | None, tz_offset_hours: int) -> str:
    """把 SQLite UTC 时间字符串（"YYYY-MM-DD HH:MM:SS"）转成用户本地时区。

    SQLite 的 CURRENT_TIMESTAMP 返回 UTC 时间。
    这里转成本地时区以便 LLM 看到的"对话时间"与用户感知一致。
    """
    if not ts_str:
        return ""
    try:
        ts_clean = ts_str.split(".")[0]
        dt_utc = datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc,
        )
        local_dt = dt_utc.astimezone(timezone(timedelta(hours=tz_offset_hours)))
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return ts_str


class PromptService:
    """system prompt 拼装 + LLM 友好的上下文。"""

    def __init__(
        self,
        authorizer: Authorizer,
        conversation_service: ConversationService,
        block_service: BlockService,
        note_service: NoteService,
        retrieval_service: RetrievalService,
        tz_offset_hours: int = 8,
        recent_message_limit: int = 20,
        history_max_messages: int = 50,
    ):
        self.authorizer = authorizer
        self.conversation_service = conversation_service
        self.block_service = block_service
        self.note_service = note_service
        self.retrieval_service = retrieval_service
        self.tz_offset_hours = tz_offset_hours
        self.recent_message_limit = recent_message_limit
        self.history_max_messages = history_max_messages

    def build(
        self,
        user_id: str,
        persona_id: str,
        query: str = "",
    ) -> dict:
        """拼装完整上下文，返回 LLM 调用所需的结构。

        Args:
            user_id, persona_id: 用户 + persona
            query: 当前用户输入（用于 query-aware 检索；空字符串则跳过检索）

        Returns:
            {
                "system_prompt": str,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    ...
                ]
            }

        Raises:
            PermissionError: user 不存在 / 不拥有 persona
        """
        # 0. 权限校验
        self.authorizer.require_persona(user_id, persona_id)

        # 1. 读所有需要的数据
        persona_basics = self._get_persona_basics(user_id, persona_id)
        blocks = self.block_service.get_blocks(user_id, persona_id)
        # 用 history_max_messages（不是 recent_message_limit）——
        # 防止 context 无限增长，同时保留足够历史给 LLM
        recent_messages = self._get_recent_messages(
            user_id, persona_id, limit=self.history_max_messages,
        )
        last_conv_at = self.conversation_service.get_last_conversation_at(
            user_id, persona_id
        )

        # 2. 拼装 system_prompt
        related_notes = []
        if query:
            related_notes = self.retrieval_service.retrieve(
                user_id=user_id,
                persona_id=persona_id,
                query=query,
            )
        system_prompt = self._render_system_prompt(
            persona_basics=persona_basics,
            blocks=blocks,
            last_conv_at=last_conv_at,
            recent_messages=recent_messages,
            related_notes=related_notes,
        )

        # 3. 拼装 messages 列表（system + history）
        messages = [{"role": "system", "content": system_prompt}]
        for msg in recent_messages:
            messages.append({
                "role": msg.role,
                "content": self._render_message(msg),
            })

        return {
            "system_prompt": system_prompt,
            "messages": messages,
        }

    # === 内部 helper ===

    def _get_recent_messages(
        self,
        user_id: str,
        persona_id: str,
        limit: int,
    ) -> list:
        """获取对话消息（跨 session 的全部历史）。

        关键设计：chat LLM 在对话过程中不应被 session_close 截断——
        之前的实现只取"上一个 session_close marker 之后"的消息，
        导致新 session 看不到旧 session 间的旧对话（要靠 extraction → notes → retrieval 间接补回）。

        现在改为：直接取所有 user/assistant 消息（session_close 行已被
        get_messages_between 自动过滤）。这样 chat LLM 在每轮对话时
        都看到完整历史，对话连贯性更好。

        Extraction 工作流仍然只看 [最新 marker, 上一 marker] 之间的对话——
        这是两个 LLM 职责的清晰分离。

        Args:
            user_id, persona_id: 用户 + persona
            limit: 最多取多少条（避免无限增长，按 id DESC + 反转得到最近 N 条）

        Returns:
            按 id 升序的最近 limit 条 user/assistant 消息
        """
        return self.conversation_service.get_messages_between(
            user_id=user_id,
            persona_id=persona_id,
            limit=limit,
        )

    def _get_persona_basics(
        self,
        user_id: str,
        persona_id: str,
    ) -> dict | None:
        """读 persona_basics note 并解析为 dict。

        Returns:
            dict (parsed JSON) or None (无 persona_basics)
        """
        notes = self.note_service.find_notes(
            user_id=user_id,
            persona_id=persona_id,
            types=[NoteType.PERSONA_BASICS],
        )
        if not notes:
            return None
        content = notes[0].content
        if isinstance(content, dict):
            return content
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return {"_raw": content}

    def _render_message(self, msg) -> str:
        """渲染单条对话消息（仅时间戳 + 内容，不重复 role tag）。

        格式：[2026-09-28 17:30:42] 你好

        注意：
        1. 不再加 `user:` / `assistant:` 前缀——
           role 已经在 messages 列表的 role 字段中，避免 LLM 模仿格式
        2. 时间戳从 SQLite UTC 转成本地时区，与用户感知一致
        """
        ts = _utc_ts_to_local(msg.created_at, self.tz_offset_hours)
        return f"[{ts}] {msg.content}"

    def _render_system_prompt(
        self,
        persona_basics: dict | None,
        blocks: list,
        last_conv_at: str | None,
        recent_messages: list,
        related_notes: list | None = None,
    ) -> str:
        """渲染完整 system prompt 文本。

        包含 6 个段落：
        1. 当前时间 + 上次对话
        2. persona_basics JSON
        3. 3 个 Block（persona_runtime / user_profile / relationship_state）
        4. 当前 session 的对话历史
        5. 相关记忆（retrieval from notes）
        6. 结尾指令
        """
        sections: list[str] = []

        # 段落 1：当前时间
        sections.append(self._render_time_section(last_conv_at))

        # 段落 2：persona_basics
        sections.append(self._render_persona_basics(persona_basics))

        # 段落 3：3 个 Block
        sections.append(self._render_blocks(blocks))

        # 段落 4：对话历史（跨 session 全部最近的消息）
        sections.append(self._render_recent_messages(recent_messages))

        # 段落 5：相关记忆（如果有）
        if related_notes:
            sections.append(self._render_related_notes(related_notes))

        # 段落 6：结尾指令
        sections.append(self._render_footer())

        return "\n\n".join(sections)

    def _render_time_section(self, last_conv_at: str | None) -> str:
        """当前时间信息。

        注意：
        1. "现在" 用本地时区显示（format_now 已经处理）
        2. "上次对话" 的时间戳也从 SQLite UTC 转成本地时区
        3. 明确区分"现在"和"上次对话"，避免 LLM 混淆
        """
        now_str = format_now(self.tz_offset_hours)
        lines = [
            "## 当前时间信息",
            f"- **现在（这是用户发本条消息的时间）**：{now_str}",
        ]
        if last_conv_at:
            # 从 SQLite UTC 转本地时区显示
            display = _utc_ts_to_local(last_conv_at, self.tz_offset_hours)
            lines.append(
                f"- 用户上次对话的起始时间（注意：这不是当前消息时间，是历史）：{display}"
            )
        return "\n".join(lines)

    def _render_persona_basics(self, persona_basics: dict | None) -> str:
        """persona_basics JSON（结构化展示）。"""
        if persona_basics is None:
            return "## 人设基础\n（未设置）"
        return "## 人设基础\n```json\n" + json.dumps(
            persona_basics, ensure_ascii=False, indent=2
        ) + "\n```"

    def _render_blocks(self, blocks: list) -> str:
        """3 个 Block 的内容（每个一段）。"""
        if not blocks:
            return "## 当前状态\n（暂无）"

        sections = ["## 当前状态"]
        block_labels = {
            "persona_runtime": "助手运行时状态",
            "user_profile": "用户画像",
            "relationship_state": "关系进展",
        }
        for b in blocks:
            label = block_labels.get(b.block_type.value, b.block_type.value)
            sections.append(
                f"### {label}\n```json\n"
                + json.dumps(b.fields, ensure_ascii=False, indent=2)
                + "\n```"
            )

        return "\n\n".join(sections)

    def _render_recent_messages(self, messages: list) -> str:
        """对话历史（跨 session 最近的 N 条，拼接进 prompt）。

        注意：这里包含所有 session 的 user/assistant 消息（session_close 行
        已被 get_messages_between 自动过滤）。LLM 在每轮对话时看到完整历史。
        """
        if not messages:
            return "## 对话历史\n（暂无）"
        lines = [f"## 对话历史（共 {len(messages)} 条）"]
        for m in messages:
            ts = m.created_at or ""
            if ts and " " in ts:
                ts = ts.split(".")[0]
            lines.append(f"- [{ts}] {m.role}: {m.content}")
        return "\n".join(lines)

    def _render_related_notes(self, notes: list) -> str:
        """相关记忆（query 召回，来自历史 session 抽取的 notes）。"""
        if not notes:
            return ""
        lines = ["## 相关记忆"]
        for n in notes[:10]:
            content = (
                n.content
                if isinstance(n.content, str)
                else json.dumps(n.content, ensure_ascii=False)
            )
            lines.append(
                f"- ({n.type.value}, importance={n.importance:.2f}) {content}"
            )
        return "\n".join(lines)

    def _render_footer(self) -> str:
        """结尾指令（从 prompts 包加载）。"""
        return CHAT_FOOTER


__all__ = ["PromptService"]