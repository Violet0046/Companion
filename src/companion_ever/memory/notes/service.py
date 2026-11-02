"""笔记的用例封装 + 权限校验。"""
from __future__ import annotations

from typing import Optional

from companion_ever.characters.access import Authorizer
from companion_ever.domain.entities import Note
from companion_ever.domain.enums import MemoryKind
from companion_ever.memory.notes.repository import NoteRepo
from companion_ever.infrastructure.database.connection import transaction


class NoteConflictError(ValueError):
    """调用方读取的版本已过期，不能覆盖较新的笔记。"""


class NoteService:
    """笔记的用例封装 + 权限校验。

    单一职责：
    - 权限校验（user 必须拥有 persona_id）—— 通过 Authorizer
    - 业务校验（content 非空、importance 在 [0,1]）
    - 调用 NoteRepo 完成 CRUD + FTS5 检索
    """

    def __init__(self, authorizer: Authorizer, note_repo: NoteRepo):
        self.authorizer = authorizer
        self.note_repo = note_repo

    # === 显式写入 ===

    def sources(self,user,persona,note_ids):
        self.authorizer.require_persona(user,persona)
        return self.note_repo.sources(user,persona,note_ids)

    def create_note(
        self,
        user_id: str,
        persona_id: str,
        kind: MemoryKind,
        content: str,
        *,
        source_quote: Optional[str] = None,
        importance: float = 0.5,
    ) -> str:
        """显式创建笔记；自动抽取由 MemoryWriter 校验并原子提交。

        校验：
        - user 必须拥有 persona（authorizer.require_persona）
        - content 非空
        - importance ∈ [0, 1]

        Returns:
            新 note 的 id（note_<uuid>）

        Raises:
            PermissionError: user 不存在 / 不拥有 persona
            ValueError: content 空 / importance 越界
        """
        self.authorizer.require_persona(user_id, persona_id)
        self._check_importance(importance)
        return self.note_repo.create(
            user_id=user_id,
            persona_id=persona_id,
            kind=kind,
            content=content,
            source_quote=source_quote,
            importance=importance,
        )
    # === 读取 ===

    def get_note(self, note_id: str, user_id: str) -> Optional[Note]:
        """读单条笔记（带权限校验）。

        Returns:
            None if 笔记不存在 / 不属于 user
            否则 Note 实体

        重要：返回 None 而不是抛异常，防"note 存在但不属于你"的信息泄露。

        同时更新访问元数据；当前召回评分尚未使用这些字段。
        """
        note = self.note_repo.get(note_id)
        if note is None or note.user_id != user_id:
            return None
        self.authorizer.require_persona(user_id, note.persona_id)
        self.note_repo.bump_access(note_id)
        return note

    def find_notes(
        self,
        user_id: str,
        persona_id: str,
        *,
        kind: Optional[MemoryKind] = None,
        kinds: Optional[list[MemoryKind]] = None,
        is_archived: bool = False,
        limit: int = 100,
    ) -> list[Note]:
        """按条件查询（限定 user_id + persona_id）。

        Args:
            user_id: 用户 id（必填，自动限定）
            persona_id: persona id（必填，自动限定）
            kind: 单选 kind 过滤（与 kinds 二选一）
            kinds: 多选 kind 过滤（IN 子句）
            is_archived: 默认 False（不含归档）
            limit: 最多返回多少条

        Returns:
            按 updated_at DESC 排序的笔记列表
        """
        self.authorizer.require_persona(user_id,persona_id)
        return self.note_repo.find(
            user_id=user_id,
            persona_id=persona_id,
            kind=kind,
            kinds=kinds,
            is_archived=is_archived,
            limit=limit,
        )

    def search_notes(
        self,
        user_id: str,
        persona_id: str,
        query: str,
        limit: int = 20,
    ) -> list[Note]:
        """FTS5 全文检索（限定 user_id + persona_id）。

        Args:
            query: 搜索关键词（建议 ≥3 字，trigram 限制）
            limit: 最多返回多少条

        Returns:
            按 FTS5 相关度排序的笔记列表
        """
        self.authorizer.require_persona(user_id,persona_id)
        return self.note_repo.search(
            query=query,
            user_id=user_id,
            persona_id=persona_id,
            limit=limit,
        )


    # === 显式编辑与删除 ===

    def edit_content(self, user_id: str, persona_id: str, note_id: str, content: str, revision: int) -> Note:
        content=content.strip()
        if not content:
            raise ValueError('记忆内容不能为空')
        with transaction(self.note_repo.conn):
            note=self._scoped_note(user_id,persona_id,note_id)
            if note.is_archived:
                raise LookupError('记忆不存在或已删除')
            if note.revision!=revision:
                raise NoteConflictError('记忆已更新，请重新打开后再编辑')
            if note.content!=content:
                self.note_repo.edit_content(note,content)
            return self.note_repo.get(note_id)

    def delete_note(self, user_id: str, persona_id: str, note_id: str, revision: int) -> None:
        with transaction(self.note_repo.conn):
            note=self._scoped_note(user_id,persona_id,note_id)
            if note.is_archived:
                return  # 重复删除不新增版本或事实操作。
            if note.revision!=revision:
                raise NoteConflictError('记忆已更新，请重新打开后再删除')
            self.note_repo.archive(note_id)

    # === 内部 helper（LLM 不可见）===

    def _scoped_note(self, user_id: str, persona_id: str, note_id: str) -> Note:
        self.authorizer.require_persona(user_id,persona_id)
        note=self.note_repo.get(note_id)
        if note is None or (note.user_id,note.persona_id)!=(user_id,persona_id):
            raise LookupError('记忆不存在或不属于当前角色')
        return note


    @staticmethod
    def _check_importance(importance: float) -> None:
        """校验 importance 在 [0, 1]。"""
        if not 0 <= importance <= 1:
            raise ValueError(
                f"importance 必须在 [0, 1] 之间, 实际 {importance}"
            )


__all__ = ["NoteService", "NoteConflictError"]
