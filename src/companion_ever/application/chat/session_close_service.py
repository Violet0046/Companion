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

import logging
import threading

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from companion_ever.application.authz import Authorizer
from companion_ever.application.chat.conversation_service import ConversationService
from companion_ever.infrastructure.trace import trace_scope
from companion_ever.utils.time import parse_sqlite_ts

logger = logging.getLogger(__name__)

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
        # 正在进行的记录任务（防止重复触发）
        self._closing: set[tuple[str, str]] = set()

    # ============ 自动管理 session 边界 ============

    def close_if_stale(
        self,
        user_id: str,
        persona_id: str,
        idle_minutes: int,
    ) -> bool:
        """如果上一个 session 已空闲超时，自动关闭它。

        为什么要这个：
        session 边界不能依赖用户记得按 /quit。用户可能直接关掉 App、
        切后台、断网，边界就没了。边界没了，摘要、抽取、上下文的边界全乱。

        三个触发点都调这个函数：
        - 用户发下一条消息时（回来了 -> 先关上一个）
        - 服务器定时任务（用户永远不回来也能关）
        - 进程启动时（CLI / 服务器重启）

        Args:
            idle_minutes: 空闲阈值（分钟）。0 = 禁用自动判定。

        Returns:
            True = 已自动关闭（抽取在后台跑，不阻塞调用方）
        """
        if idle_minutes <= 0:
            return False

        active = self.conversation_service.get_active_session(
            user_id=user_id, persona_id=persona_id,
        )
        if not active:
            return False  # 没有未关闭的 session

        last_at = parse_sqlite_ts(active["last_message_at"])
        if last_at is None:
            return False

        gap = (datetime.now(timezone.utc) - last_at).total_seconds()
        if gap < idle_minutes * 60:
            return False

        # 空闲超时 -> 自动关闭（抽取在后台跑，不阻塞当前消息）
        self.close_session_background(user_id, persona_id)
        return True

    def is_closing(self, user_id: str, persona_id: str) -> bool:
        """这个角色当前有没有正在进行的记录任务。"""
        return (user_id, persona_id) in self._closing

    def close_session_background(self, user_id: str, persona_id: str) -> bool:
        """在后台线程里关闭 session（不阻塞调用方）。

        用于自动边界管理：抽取是一次完整 LLM 调用（几十秒），
        不能让用户等它。

        Returns:
            True = 已启动；False = 已经有任务在跑了（不重复启动）
        """
        key = (user_id, persona_id)
        if key in self._closing:
            return False
        self.authorizer.require_persona(user_id, persona_id)
        self._closing.add(key)
        thread = threading.Thread(
            target=self._close_worker,
            args=(user_id, persona_id),
            daemon=True,
            name="session-close",
        )
        thread.start()
        return True

    def _close_worker(self, user_id: str, persona_id: str) -> None:
        """后台线程的实现（异常不影响主流程）。"""
        try:
            self.close_session(user_id, persona_id)
        except Exception:
            logger.exception(
                "自动关闭 session 失败: user=%s persona=%s",
                user_id, persona_id,
            )
        finally:
            self._closing.discard((user_id, persona_id))

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

            # 抽取整体失败（LLM 调用就失败）时回滚 marker。
            # 否则下次抽取的范围是 [本 marker, 下个 marker]，
            # 这段内容将永远不会被抽取——记忆静默丢数据。
            # 回滚后下次抽取的范围会自动包含它。
            final_marker_id = marker_id
            if state.get("extraction_failed"):
                self.conversation_service.delete_session_close(marker_id)
                tr.step("extraction.rolled_back", marker_id=marker_id,
                        reason="; ".join(report.errors)[:200])
                final_marker_id = 0  # 0 = 已回滚，本次未成功

            return SessionCloseReport(
                marker_id=final_marker_id,
                extraction=report,
            )


__all__ = ["SessionCloseReport", "ExtractionReport", "SessionCloseService"]