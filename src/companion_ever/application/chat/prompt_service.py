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
from companion_ever.application.invariants import (
    check_prompt_invariants,
    strict_mode,
)
from companion_ever.application.memory.note_service import NoteService
from companion_ever.application.persona.block_service import BlockService
from companion_ever.domain.enums import NoteType
from companion_ever.infrastructure.trace import current_trace
from companion_ever.prompts import CHAT_FOOTER
from companion_ever.utils.time import describe_gap, format_now, parse_sqlite_ts


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
        import time as _time
        _t0 = _time.perf_counter()

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
            related_notes=related_notes,
        )

        # 3. 拼装 messages 列表
        #    历史对话只在这里出现一次（作为原生 role 消息）——
        #    不要再在 system_prompt 里渲染一遍 "## 对话历史"，否则会重复送两遍。
        messages = [{"role": "system", "content": system_prompt}]
        for msg in recent_messages:
            messages.append({
                "role": msg.role,
                "content": self._render_message(msg),
            })

        self._record_trace(
            user_id, persona_id, system_prompt, messages,
            related_notes=related_notes,
            ms=(_time.perf_counter() - _t0) * 1000,
        )

        return {
            "system_prompt": system_prompt,
            "messages": messages,
        }

    def _record_trace(
        self,
        user_id: str,
        persona_id: str,
        system_prompt: str,
        messages: list[dict],
        related_notes: list,
        ms: float,
    ) -> None:
        """把 prompt 组装的细节记进 trace，并跑一遍廉价不变量检查。"""
        tr = current_trace()
        if tr is None:
            return

        history = messages[1:]
        history_chars = sum(len(str(m.get("content", ""))) for m in history)
        note_chars = sum(
            len(n.content) if isinstance(n.content, str) else len(str(n.content))
            for n in related_notes
        )

        tr.step(
            "prompt.build",
            ms=ms,
            system_chars=len(system_prompt),
            history_msgs=len(history),
            history_chars=history_chars,
            notes=len(related_notes),
            note_chars=note_chars,
            total_chars=len(system_prompt) + history_chars,
        )

        # --- 不变量检查 ---
        last_db_message = None
        try:
            recent = self.conversation_service.get_recent_messages(
                user_id, persona_id, limit=1,
            )
            if recent:
                last_db_message = recent[0].content
        except Exception:
            last_db_message = None

        # 只数"用户/角色记忆"，persona_basics 是人设定义不算
        total_notes = 0
        try:
            memory_types = [
                t for t in NoteType if t != NoteType.PERSONA_BASICS
            ]
            total_notes = len(self.note_service.find_notes(
                user_id=user_id, persona_id=persona_id,
                types=memory_types, limit=1000,
            ))
        except Exception:
            total_notes = 0

        violations = check_prompt_invariants(
            messages=messages,
            system_prompt=system_prompt,
            retrieved_count=len(related_notes),
            last_db_message=last_db_message,
            total_notes=total_notes,
        )
        tr.step(
            "invariants.check",
            passed=not violations,
            violations=[v.to_dict() for v in violations],
        )
        if violations and strict_mode():
            raise ValueError(
                "prompt 不变量被违反: " + "; ".join(v.message for v in violations)
            )

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

        格式：[2026-09-28 17:30:42] 你好（仅 user 消息带时间戳，assistant 不带）

        注意：
        1. 不再加 `user:` / `assistant:` 前缀——
           role 已经在 messages 列表的 role 字段中，避免 LLM 模仿格式
        2. 时间戳从 SQLite UTC 转成本地时区，与用户感知一致
        """
        # assistant 消息不带时间戳。
        # 原因（实测结论）：
        # 1. 它紧跟在 user 消息后面，时间戳冗余
        # 2. 所有消息都带 [时间戳] 前缀时，模型会学会在自己的回复里
        #    也加上同样的前缀（实测 2/2 出现），
        #    而且它往往会照抄上一条的时间戳 -> 回答里的时间是错的
        # 3. 只留 user 消息带时间戳时，模型不再会模仿（实测 0/2）
        if msg.role == "assistant":
            return msg.content
        ts = _utc_ts_to_local(msg.created_at, self.tz_offset_hours)
        return f"[{ts}] {msg.content}"

    def _render_system_prompt(
        self,
        persona_basics: dict | None,
        blocks: list,
        last_conv_at: str | None,
        related_notes: list | None = None,
    ) -> str:
        """渲染完整 system prompt 文本。

        包含 5 个段落：
        1. 当前时间 + 上次对话
        2. persona_basics JSON
        3. 3 个 Block（persona_runtime / user_profile / relationship_state）
        4. 相关记忆（retrieval from notes）
        5. 结尾指令

        注意：**对话历史不在这里**。
        历史以原生 role 消息的形式放在 messages 列表里（见 build()），
        避免同一段历史被送两遍（system prompt 一遍 + messages 一遍）。
        """
        sections: list[str] = []

        # 段落 1：当前时间
        sections.append(self._render_time_section(last_conv_at))

        # 段落 2：persona_basics
        sections.append(self._render_persona_basics(persona_basics))

        # 段落 3：3 个 Block
        sections.append(self._render_blocks(blocks))

        # 段落 4：相关记忆（如果有）
        if related_notes:
            sections.append(self._render_related_notes(related_notes))

        # 段落 5：结尾指令
        sections.append(self._render_footer())

        return "\n\n".join(sections)

    def _render_time_section(self, last_conv_at: str | None) -> str:
        """当前时间信息（包含"这一轮是什么性质"的指引）。

        为什么重写这个段（一行行的历史）：
        原来的写法是

            - 现在（这是用户发本条消息的时间）：2026-10-28 周三 10:54
            - 用户上次对话的起始时间（注意：这不是当前消息时间）：2026-10-28 00:08:47

        问题：
        1. 那个"起始时间"写得很易让模型当成"现在"，实际上模型就回答过
           "现在还是凌晨"（而实际是上午 10:54）
        2. 时间戳写在那里模型并不会真的去思考它。

        现在的写法：
        - 给准确的"现在"
        - 用人话说的间隔（不给绝对时间戳，避免被当成"现在"）
        - **直接告诉模型这一轮该怎么开口**（连续还是新开一次）
        """
        now_str = format_now(self.tz_offset_hours)
        lines = [
            "## 当前时间信息",
            f"- **现在**：{now_str}",
        ]

        last_dt = parse_sqlite_ts(last_conv_at)
        if last_dt is None:
            lines.append(
                "- **这一轮**：这是用户的第一次说话，"
                "主动开口、建立一点感情基础。"
            )
            return "\n".join(lines)

        now_utc = datetime.now(timezone.utc)
        gap_seconds = int((now_utc - last_dt).total_seconds())
        gap_text = describe_gap(gap_seconds)

        # 用人话的上一次说话时间（本地时区）
        local_last = last_dt.astimezone(
            timezone(timedelta(hours=self.tz_offset_hours))
        )
        lines.append(
            f"- **用户上一次说话**："
            f"{local_last.strftime('%m月%d日 %H:%M')}，距今 {gap_text}"
        )

        # 这一轮的性质（给模型的开口指引）
        if gap_seconds < 10 * 60:
            hint = (
                "用户刚说完还没多久，话题是**连续**的——"
                "直接接着往下谈，不要重新打招呼、不要随意改口背景。"
            )
        elif gap_seconds < 3 * 3600:
            hint = (
                f"用户距上次说话过了 {gap_text}，属于**同一次随意聊的延续**——"
                "可以自然地接上上一句话，不必重新介绍。"
            )
        elif gap_seconds < 16 * 3600:
            hint = (
                f"用户今天之前来过，距上次说话过了 {gap_text}——"
                "还是同一天的事，可以紧凑一下但不必重新报到身份。"
            )
        else:
            hint = (
                f"用户隔了 {gap_text} 才再来，这是**一次新的对话**——"
                "之前的话题已经过去了，**不要假装刚才还在聊**；"
                "可以自然地关心一下他这段时间过得怎么样。"
            )
        lines.append(f"- **这一轮**：{hint}")

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