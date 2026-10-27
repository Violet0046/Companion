"""application/chat/session_close_service.py — 会话关闭的用例。

职责：
- 写 session_close sentinel marker
- 立即触发该 session 的抽取（通过 LangGraph extraction_workflow）

调用方（CLI / web 关闭窗口）：一行调用 close_session()

调用链：
    chat_workflow.close_node
        ↓
    SessionCloseService.close_session()
        ├─ conversation_service.log_session_close()    ← 写 marker
        └─ extraction_workflow.invoke({user_id, persona_id})    ← LangGraph 抽取
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from companion_ever.application.authz import Authorizer
from companion_ever.application.chat.conversation_service import ConversationService
from companion_ever.infrastructure.trace import trace_scope

if TYPE_CHECKING:
    from langgraph.graph import CompiledGraph
    from companion_ever.workflows.extraction_workflow import ExtractionState


@dataclass
class ExtractionReport:
    """抽取 workflow 的输出报告（与 extraction service 同形）。"""

    facts_created: int = 0
    blocks_updated: int = 0
    conversations_processed: int = 0
    session_start: datetime | None = None
    session_end: datetime | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class SessionCloseReport:
    """会话关闭的合并报告。"""

    marker_id: int
    extraction: ExtractionReport


def _report_from_state(state: "ExtractionState") -> ExtractionReport:
    """从 extraction_workflow state 构造 ExtractionReport。"""
    messages = state.get("messages") or []
    return ExtractionReport(
        facts_created=state.get("facts_created", 0),
        blocks_updated=state.get("blocks_updated", 0),
        conversations_processed=len(messages),
        session_start=messages[0].created_at if messages else None,
        session_end=messages[-1].created_at if messages else None,
        errors=list(state.get("errors") or []),
    )


class SessionCloseService:
    """关闭会话 = 写 marker + 触发 extraction_workflow。"""

    def __init__(
        self,
        authorizer: Authorizer,
        conversation_service: ConversationService,
        extraction_graph: "CompiledGraph",
    ):
        self.authorizer = authorizer
        self.conversation_service = conversation_service
        self.extraction_graph = extraction_graph

    def close_session(
        self,
        user_id: str,
        persona_id: str,
    ) -> SessionCloseReport:
        """会话关闭：写 marker + 立即抽取（走 extraction_workflow）。

        抽取失败不影响 marker（marker 已持久化，下次可重试）。

        Args:
            user_id, persona_id: 用户 + 人设

        Returns:
            SessionCloseReport（marker_id + ExtractionReport）

        Raises:
            PermissionError: user 不拥有 persona
        """
        import time as _time

        with trace_scope("extraction", user_id, persona_id) as tr:
            _t0 = _time.perf_counter()
            self.authorizer.require_persona(user_id, persona_id)

            # 1. 写 sentinel marker
            marker_id = self.conversation_service.log_session_close(
                user_id=user_id, persona_id=persona_id,
            )
            tr.step(
                "db.log_session_close",
                ms=(_time.perf_counter() - _t0) * 1000,
                marker_id=marker_id,
            )

            # 2. 触发 extraction_workflow
            _t1 = _time.perf_counter()
            state: ExtractionState = self.extraction_graph.invoke({
                "user_id": user_id,
                "persona_id": persona_id,
            })
            tr.step(
                "extraction.workflow",
                ms=(_time.perf_counter() - _t1) * 1000,
                messages_loaded=len(state.get("messages") or []),
                facts_created=state.get("facts_created", 0),
                blocks_updated=state.get("blocks_updated", 0),
                errors=list(state.get("errors") or []),
            )
            report = _report_from_state(state)

            return SessionCloseReport(
                marker_id=marker_id,
                extraction=report,
            )


__all__ = ["SessionCloseReport", "ExtractionReport", "SessionCloseService"]