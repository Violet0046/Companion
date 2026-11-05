"""对话原文的用例封装。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from companion_ever.characters.access import Authorizer
from companion_ever.domain.entities import ConversationMessage
from companion_ever.conversations.repository import ConversationRepo
from companion_ever.utils.text import extract_keywords


class ConversationService:
    """对话原文的用例封装 + 权限校验。"""

    def __init__(
        self,
        authorizer: Authorizer,
        conv_repo: ConversationRepo,
        *, memory_jobs=None, background=None,
    ):
        self.authorizer = authorizer
        self.conv_repo = conv_repo
        self.memory_jobs = memory_jobs
        self.background = background

    def external_context(self, user, persona, *, current_group_id=None):
        """Bounded, unprocessed source messages; collecting never schedules memory."""
        self.authorizer.require_persona(user, persona)
        if self.memory_jobs is None or self.background is None:
            return []
        limit = self.background.external_context_message_limit
        upper = self.conv_repo.message_upper_bound(user)
        sources = []
        for cid in self.conv_repo.external_source_ids(user, persona, current_group_id):
            try:
                scope = self.conv_repo.memory_scope(user, persona, cid)
                lower = self.memory_jobs.progress({**scope, 'persona_id': persona})
                messages = self.conv_repo.memory_messages(user, persona, cid, since_id=lower,
                                                           until_id=upper, limit=limit + 1)
            except PermissionError:
                continue
            if messages:
                sources.append({'conversation_id': cid, 'kind': scope['kind'], 'name': scope['name'],
                                'member_revision': scope['member_revision'], 'messages': messages})
        sources.sort(key=lambda s: (s['messages'][-1].created_at, s['messages'][-1].id), reverse=True)
        sources = sources[:2]
        newest = sorted((m for s in sources for m in s['messages']),
                        key=lambda m: (m.created_at, m.id), reverse=True)[:limit]
        chosen = {}
        remaining = 4000
        for message in newest:
            if remaining <= 0:
                break
            text = message.content[:remaining]
            remaining -= len(text)
            chosen[message.id] = {'id': message.id, 'created_at': message.created_at,
                                  'sender_kind': message.sender_kind, 'sender_id': message.sender_id,
                                  'content': text, 'truncated': len(text) < len(message.content)}
            # Quoted text is deliberately not expanded across source/size boundaries.
            if message.quoted_message_id is not None:
                chosen[message.id]['quoted_message_id'] = message.quoted_message_id
        result = []
        for source in sources:
            messages = [chosen[m.id] for m in source['messages'] if m.id in chosen]
            if messages:
                result.append({**source, 'messages': messages,
                               'omitted_earlier': source['messages'][0].id != messages[0]['id']})
        return self.visible_external_context(user, persona, result)

    def visible_external_context(self, user, persona, sources):
        """Fence leave/rejoin and role deactivation after collecting source text."""
        self.authorizer.require_persona(user, persona)
        visible = []
        for source in sources:
            try:
                scope = self.conv_repo.memory_scope(user, persona, source['conversation_id'])
            except PermissionError:
                continue
            if scope['member_revision'] == source['member_revision']:
                visible.append(source)
        return visible

    def quote(self,user,persona,message_id):
        self.authorizer.require_persona(user,persona)
        return self.conv_repo.quote_for_persona(user,persona,message_id)

    def memory_messages(self,user,persona,cid,**bounds):
        self.authorizer.require_persona(user,persona)
        return self.conv_repo.memory_messages(user,persona,cid,**bounds)

    def membership_events(self,user,persona,cid,after,through):
        self.authorizer.require_persona(user,persona)
        return self.conv_repo.membership_events(user,persona,cid,after,through)

    # === 写入（3 个）===

    def mark_read(self, user, persona, message_id):
        self.authorizer.require_persona(user,persona)
        self.conv_repo.mark_read(user,persona,message_id)

    def should_notify(self, user, persona, message_id):
        return self.authorizer.owns_persona(user,persona) and self.conv_repo.should_notify(user,persona,message_id)

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

        触发时机：显式维护调用或服务判断空闲边界；关闭页面不写标记。
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

    def unread_count(self, user_id: str, persona_id: str, after_id: int = 0) -> dict:
        """按客户端已读游标统计正式助手消息，保留现有本机已读语义。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.unread_count(user_id, persona_id, after_id)

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

    def list_conversations(self) -> list[dict]:
        """供宿主扫描的会话范围；调用方逐个校验有效角色。"""
        return self.conv_repo.list_conversations()

    def pending_input(self, user_id, persona_id, after_id, *, conversation_id=None):
        """供记忆调度读取原话积累，不暴露会话表给调度器。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.pending_input(user_id, persona_id, after_id,conversation_id=conversation_id)

    # === 游标分页 / 日期 / 搜索（微信式历史浏览）===

    def get_page(
        self,
        user_id: str,
        persona_id: str,
        *,
        before_id: Optional[int] = None,
        after_id: Optional[int] = None,
        date: Optional[str] = None,
        limit: int = 50,
        tz_offset_hours: int = 0,
    ) -> list[ConversationMessage]:
        """按游标取一页消息（时间正序）。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.get_page(
            user_id=user_id, persona_id=persona_id,
            before_id=before_id, after_id=after_id, date=date, limit=limit,
            tz_offset_hours=tz_offset_hours,
        )

    def list_dates(
        self, user_id: str, persona_id: str, tz_offset_hours: int = 0,
    ) -> list[dict]:
        """有对话的日期列表（降序）+ 每天消息数。

        tz_offset_hours 用来按**本地日期**分组（DB 存的是 UTC）。
        """
        self.authorizer.require_persona(user_id, persona_id)
        return self.conv_repo.list_dates(
            user_id=user_id, persona_id=persona_id,
            tz_offset_hours=tz_offset_hours,
        )

    def search_messages(
        self,
        user_id: str,
        persona_id: str,
        query: str,
        limit: int = 50,
    ) -> list[ConversationMessage]:
        """搜索对话记录。

        关键词抽取重用 utils/text.py（与记忆检索同一套逻辑）。
        """
        self.authorizer.require_persona(user_id, persona_id)
        short_kw, long_kw = extract_keywords(query)
        patterns = short_kw + long_kw
        if not patterns:
            return []
        return self.conv_repo.search(
            user_id=user_id, persona_id=persona_id,
            patterns=patterns, limit=limit,
        )

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
