"""application/chat/extraction_service.py — 后台抽取 service。

职责：
- 处理最近一个已关闭 session 的对话
- 调 LLM 抽取 facts + 更新 3 个 Block
- 不动 persona_basics（专门由 PersonaEditService 处理）

触发模式（单一）：
- 仅"会话关闭触发"
- 会话关闭时调用方先调 conversation_service.log_session_close() 写 marker
- 再调本类的 extract_last_closed_session() 处理 marker 之前的对话
- 不需要 DB 字段标记"已处理"——session 边界由 conversation_log 中的
  role='session_close' sentinel 行显式定义

设计要点：
- 单一触发：调用方负责在 session 关闭时调用一对方法（log + extract）
- LLM 调用是同步的（后台线程里跑）
- 返回 ExtractionReport（含 session_start/end、成功/失败统计）

副作用：
- 调用方对同一已关闭 session 多次调用：notes 表可能产生重复条目（无 UNIQUE 约束）；
  blocks 表因为是 merge 语义所以幂等。如需严格去重，由调用方保证只触发一次。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from companion_ever.application.llm_json import parse_json_response
from companion_ever.application.memory.note_service import NoteService
from companion_ever.application.persona.block_service import BlockService
from companion_ever.domain.enums import BlockType, NoteType
from companion_ever.infrastructure.ai.llm_client import LLMClient
from companion_ever.prompts import EXTRACTION_SYSTEM
from .conversation_service import ConversationService


# === LLM 输出 Schema（pydantic） ===

class ExtractedFact(BaseModel):
    """LLM 抽取的单条 fact."""

    type: NoteType
    content: str
    importance: float = Field(default=0.5, ge=0, le=1)
    source_quote: str | None = None


class BlockUpdate(BaseModel):
    """单个 Block 的 fields 更新."""

    block_type: BlockType
    fields: dict[str, Any]


class ExtractionResult(BaseModel):
    """LLM 抽取的完整结果."""

    facts: list[ExtractedFact] = Field(default_factory=list)
    blocks: list[BlockUpdate] = Field(default_factory=list)
    reasoning: str = ""


class ExtractionReport(BaseModel):
    """抽取服务执行的报告。"""

    facts_created: int = 0
    blocks_updated: int = 0
    conversations_processed: int = 0
    session_start: datetime | None = None
    session_end: datetime | None = None
    errors: list[str] = Field(default_factory=list)


class ExtractionService:
    """后台抽取 service。"""

    def __init__(
        self,
        conversation_service: ConversationService,
        note_service: NoteService,
        block_service: BlockService,
        llm_client: LLMClient,
    ):
        self.conversation_service = conversation_service
        self.note_service = note_service
        self.block_service = block_service
        self.llm_client = llm_client

    # === 用例：抽取最近一个已关闭 session ===

    def extract_last_closed_session(
        self,
        user_id: str,
        persona_id: str,
    ) -> ExtractionReport:
        """处理最近一个已关闭 session 的对话。

        触发时机：调用方在 ConversationService.log_session_close() 之后立即调用本方法。

        Session 定义：相邻两个 session_close marker 之间（包含 latest marker）
        的 user/assistant 消息。

        行为：
        - 如果 conversation_log 中没有 session_close marker → 返回空报告
        - 如果只有一个 marker → 处理 [最开始, 那个 marker] 之间的对话
        - 如果有 ≥2 个 marker → 处理 [倒数第二个 marker, 最新 marker] 之间的对话

        Args:
            user_id, persona_id: 用户 + 人设

        Returns:
            ExtractionReport（含 session_start / session_end / 成功/失败统计）
        """
        report = ExtractionReport()

        # 1. 读最近 2 个 session_close markers
        markers = self.conversation_service.get_last_n_session_closes(
            user_id=user_id, persona_id=persona_id, n=2,
        )
        if not markers:
            return report

        last_marker = markers[-1]
        prev_marker = markers[-2] if len(markers) >= 2 else None

        # 2. 用 id 边界读 user/assistant 消息（id 顺序比时间戳更可靠——
        #    SQLite CURRENT_TIMESTAMP 是秒级，多次 marker 写入可能同一秒）
        since_id = prev_marker.id if prev_marker is not None else None
        until_id = last_marker.id

        messages = self.conversation_service.get_messages_between(
            user_id=user_id,
            persona_id=persona_id,
            since_id=since_id,
            until_id=until_id,
            limit=1000,
        )
        if not messages:
            return report

        report.conversations_processed = len(messages)
        report.session_start = self._parse_ts(messages[0].created_at)
        report.session_end = self._parse_ts(messages[-1].created_at)

        # 3. 调 LLM 抽取
        result = self._call_llm_extract(messages)

        # 4. 写 notes（user-side facts）
        for fact in result.facts:
            try:
                self.note_service.create_note(
                    user_id=user_id,
                    persona_id=persona_id,
                    type=fact.type,
                    content=fact.content,
                    source_quote=fact.source_quote,
                    importance=fact.importance,
                )
                report.facts_created += 1
            except Exception as e:
                report.errors.append(f"create_note failed: {e}")

        # 5. 更新 3 个 Block（merge 到现有 fields）
        for block_update in result.blocks:
            try:
                self.block_service.update_block_fields(
                    user_id=user_id,
                    persona_id=persona_id,
                    block_type=block_update.block_type,
                    fields=block_update.fields,
                )
                report.blocks_updated += 1
            except Exception as e:
                report.errors.append(
                    f"update_block {block_update.block_type} failed: {e}"
                )

        return report

    # === 内部 ===

    @staticmethod
    def _parse_ts(ts: str | None) -> datetime | None:
        """把 SQLite 时间字符串解析为 aware datetime（UTC）。

        接受 "2026-09-28 17:30:42" 或带 microseconds 形式。
        """
        if not ts:
            return None
        try:
            ts_clean = ts.split(".")[0]
            dt = datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return None

    def _call_llm_extract(
        self,
        messages: list,
    ) -> ExtractionResult:
        """调 LLM 抽取，返回结构化结果."""
        formatted_messages = [{"role": "system", "content": EXTRACTION_SYSTEM}]

        convo_text = self._format_messages(messages)
        formatted_messages.append({
            "role": "user",
            "content": f"## 待处理的对话（{len(messages)} 条）\n\n{convo_text}",
        })

        response_text = self.llm_client.chat(formatted_messages)

        data = parse_json_response(response_text)
        if data is None:
            return ExtractionResult(reasoning="LLM 解析失败")

        try:
            return ExtractionResult(
                facts=[ExtractedFact(**f) for f in data.get("facts", [])],
                blocks=[BlockUpdate(**b) for b in data.get("blocks", [])],
                reasoning=data.get("reasoning", ""),
            )
        except (ValueError, TypeError) as e:
            return ExtractionResult(reasoning=f"LLM 解析失败: {e}")

    def _format_messages(self, messages: list) -> str:
        """把对话消息格式化成文本."""
        lines = []
        for m in messages:
            ts = m.created_at or ""
            lines.append(f"[{ts}] {m.role}: {m.content}")
        return "\n".join(lines)


__all__ = [
    "ExtractedFact",
    "BlockUpdate",
    "ExtractionResult",
    "ExtractionReport",
    "ExtractionService",
]