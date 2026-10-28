"""application/chat/conversation_service.py — 对话原文的用例封装。

职责：
- 记录对话轮次（user + assistant）
- 提供历史对话给 system prompt 拼装用
- 时间感知（"用户上次对话是 X 天前"）
- session 关闭的 sentinel marker（log_session_close）

变化：使用 Authorizer 替代内联 _check_permission；新增 session_close 相关方法。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from companion_ever.application.authz import Authorizer
from companion_ever.domain.entities import ConversationMessage
from companion_ever.infrastructure.persistence.conversation_repo import ConversationRepo


class ConversationService:
    """对话原文的用例封装 + 权限校验。"""

    def __init__(
        self,
        authorizer: Authorizer,
        conv_repo: ConversationRepo,
    ):
        self.authorizer = authorizer
        self.conv_repo = conv_repo

    # === 写入（3 个）===

    def log_turn(
        self,
        *,
        user_id: str,
        persona_id: str,
        turn_index: int,
        role: str,
        content: str,
    ) -> int:
        """记录一条对话消息（user / assistant）。

        Raises:
            PermissionError: user 不拥有 persona
            ValueError: content 空 / role 不合法
            sqlite3.IntegrityError: UNIQUE 冲突
        """
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.log_turn(
            user_id=user_id,
            persona_id=persona_id,
            turn_index=turn_index,
            role=role,
            content=content,
        )

    def log_session_close(
        self,
        user_id: str,
        persona_id: str,
    ) -> int:
        """在 conversation_log 写一行 role='session_close' 的 marker。

        触发时机：会话关闭事件（用户退出 / 窗口关闭 / CLI /exit）。
        调用方在调用本方法后通常应立即调用
        ExtractionService.extract_last_closed_session()，让抽取服务
        处理该 marker 之前的对话。

        turn_index 自动取 max(existing) + 1，避免与既有 UNIQUE(user, persona, turn, role) 冲突。

        Args:
            user_id, persona_id: 用户 + 人设

        Returns:
            新 marker 的 id

        Raises:
            PermissionError: user 不拥有 persona
        """
        self.authorizer.require_persona(user_id, persona_id)

        # 计算下一个可用 turn_index
        next_turn = self._next_marker_turn_index(user_id, persona_id)
        return self.conv_repo.log_session_close(
            user_id=user_id,
            persona_id=persona_id,
            turn_index=next_turn,
        )

    def _next_marker_turn_index(self, user_id: str, persona_id: str) -> int:
        """下一个 session_close marker 的 turn_index（max + 1）。

        用 max(existing turns) + 1 而不是 max(real turns) + 1——这样
        session_close 不会污染真实对话的 turn 编号（让 real turns 保持
        单调连续：1, 2, 3, ...）。
        """
        # 读最近 1 条就够（任意一行）
        recent = self.conv_repo.get_recent_messages(
            user_id=user_id, persona_id=persona_id, limit=1
        )
        # 同时也要看历史 markers
        markers = self.conv_repo.get_last_n_session_closes(
            user_id=user_id, persona_id=persona_id, n=1
        )
        all_known = recent + markers
        if not all_known:
            return 0
        return max(m.turn_index for m in all_known) + 1

    # === 读取（5 个） ===

    def log_thinking(
        self,
        *,
        message_id: int,
        user_id: str,
        persona_id: str,
        turn_index: int,
        content: str,
    ) -> int:
        """记录一条思考过程。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.log_thinking(
            message_id=message_id, user_id=user_id, persona_id=persona_id,
            turn_index=turn_index, content=content,
        )

    def get_thinking_map(
        self, *, user_id: str, persona_id: str, message_ids: list[int],
    ) -> dict[int, str]:
        """批量取思考记录 {message_id: think}"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.get_thinking_map(
            user_id=user_id, persona_id=persona_id, message_ids=message_ids,
        )

    def delete_all_thinking(self, *, user_id: str, persona_id: str) -> int:
        """删除全部思考记录。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.delete_all_thinking(
            user_id=user_id, persona_id=persona_id,
        )

    def delete_session_close(self, marker_id: int) -> bool:
        """删除一个 session_close marker（抽取失败时回滚）。"""
        return self.conv_repo.delete_session_close(marker_id)

    def get_active_session(
        self, user_id: str, persona_id: str,
    ) -> Optional[dict]:
        """未关闭 session 的信息（最后一个 marker 之后还有 message）。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.get_active_session(
            user_id=user_id, persona_id=persona_id,
        )

    def list_active_sessions(self) -> list[dict]:
        """列出所有未关闭的 session（系统内部用，不做权限校验）。"""
        return self.conv_repo.list_active_sessions()

    def get_max_turn_index(self, user_id: str, persona_id: str) -> int:
        """当前最大的 turn_index（没有则 0）——用来算下一轮的 turn_index。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.get_max_turn_index(
            user_id=user_id, persona_id=persona_id,
        )

    def get_recent_messages(
        self,
        user_id: str,
        persona_id: str,
        limit: int = 20,
    ) -> list[ConversationMessage]:
        """获取最近 N 条对话消息（按时间正序，自动过滤 session_close）。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.get_recent_messages(
            user_id=user_id, persona_id=persona_id, limit=limit,
        )

    def get_last_conversation_at(
        self,
        user_id: str,
        persona_id: str,
    ) -> Optional[str]:
        """获取最近一次真实对话的时间（不含 session_close marker）。

        Returns:
            ISO 时间字符串，或 None（从未对话过）

        权限校验：避免泄露"用户存在但没对话过"的信息。
        """
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.get_last_conversation_at(
            user_id=user_id, persona_id=persona_id,
        )

    def count(self, user_id: str, persona_id: str) -> int:
        """统计 user/assistant 对话总条数。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.count(user_id=user_id, persona_id=persona_id)

    def get_since(
        self,
        *,
        user_id: str,
        persona_id: str,
        since: datetime,
        limit: int = 100,
    ) -> list[ConversationMessage]:
        """获取 since 之后的所有对话（按时间正序，过滤 session_close）。

        用途：后台抽取 service 读上次触发后的对话。
        """
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.get_since(
            user_id=user_id,
            persona_id=persona_id,
            since=since,
            limit=limit,
        )

    def get_messages_between(
        self,
        *,
        user_id: str,
        persona_id: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        since_id: Optional[int] = None,
        until_id: Optional[int] = None,
        limit: int = 1000,
    ) -> list[ConversationMessage]:
        """获取 user/assistant 消息（不含 session_close）。

        支持两种边界（可同时使用）：
        - 时间：(since, until]
        - id：(since_id, until_id]
        """
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.get_messages_between(
            user_id=user_id,
            persona_id=persona_id,
            since=since,
            until=until,
            since_id=since_id,
            until_id=until_id,
            limit=limit,
        )

    def get_last_n_session_closes(
        self,
        user_id: str,
        persona_id: str,
        n: int = 2,
    ) -> list[ConversationMessage]:
        """返回最近 n 个 session_close marker（时间正序）。

        用途：抽取服务用它定位最近一个 session 的范围。
        """
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.get_last_n_session_closes(
            user_id=user_id,
            persona_id=persona_id,
            n=n,
        )


__all__ = ["ConversationService"]