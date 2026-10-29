"""笔记的用例封装 + 权限校验。"""
from __future__ import annotations

from typing import Optional

from companion_ever.services.authz import Authorizer
from companion_ever.domain.entities import Note
from companion_ever.domain.enums import NoteType
from companion_ever.storage.note_repo import NoteRepo


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

    # === 写入（2 个）===

    def create_note(
        self,
        user_id: str,
        persona_id: str,
        type: NoteType,
        content: str,
        *,
        source_quote: Optional[str] = None,
        importance: float = 0.5,
    ) -> str:
        """通用：创建一条笔记。

        LLM 调用场景：抽取 service 自动调，传入 facts。
        用户调用场景：通过 CLI 输入。

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
            type=type,
            content=content,
            source_quote=source_quote,
            importance=importance,
        )

    def remember(self, user_id: str, persona_id: str, type: NoteType,
                 content: str, *, source_quote: str | None = None,
                 importance: float = 0.5) -> tuple[str, bool]:
        """幂等写入抽取事实，返回 (note_id, 是否新增)。"""
        self.authorizer.require_persona(user_id, persona_id)
        self._check_importance(importance)
        return self.note_repo.remember(user_id=user_id, persona_id=persona_id,
                                       type=type, content=content,
                                       source_quote=source_quote, importance=importance)

    def create_persona_basics_note(
        self,
        user_id: str,
        persona_id: str,
        content: str,
    ) -> str:
        """写入 persona 的基础设定（创建或更新，幂等）。

        行为：
        - 如果不存在 persona_basics 笔记 → 创建
        - 如果已存在 → 更新 content（保持 type / source_quote / importance）

        幂等：重复调用只会更新内容，不会创建多条。

        调用场景：
        - 模板初始化（PersonaService.create_persona 内部调）
        - 用户自填人设
        - LLM 重新生成人设

        内部固定字段：
        - type = PERSONA_BASICS
        - importance = 0.9（关键设定）
        - source_quote = '[persona_setup]'

        Returns:
            该 persona_basics 笔记的 id

        Raises:
            PermissionError: user 不存在 / 不拥有 persona
            ValueError: content 空
        """
        self.authorizer.require_persona(user_id, persona_id)

        # 查找已存在的 persona_basics 笔记
        existing = self.note_repo.find(
            user_id=user_id,
            persona_id=persona_id,
            types=[NoteType.PERSONA_BASICS],
        )

        if existing:
            # 更新现有（保持其他字段不变）
            note_id = existing[0].id
            self.note_repo.update(note_id, content=content)
            return note_id

        # 创建新的（固定参数）
        return self.note_repo.create(
            user_id=user_id,
            persona_id=persona_id,
            type=NoteType.PERSONA_BASICS,
            content=content,
            source_quote="[persona_setup]",
            importance=0.9,
        )

    # === 读取（3 个）===

    def get_note(self, note_id: str, user_id: str) -> Optional[Note]:
        """读单条笔记（带权限校验）。

        Returns:
            None if 笔记不存在 / 不属于 user
            否则 Note 实体

        重要：返回 None 而不是抛异常，防"note 存在但不属于你"的信息泄露。

        同时会调用 bump_access（更新 last_accessed_at，Ebbinghaus 公式依赖）。
        """
        note = self.note_repo.get(note_id)
        if note is None or note.user_id != user_id:
            return None
        self.note_repo.bump_access(note_id)
        return note

    def find_notes(
        self,
        user_id: str,
        persona_id: str,
        *,
        type: Optional[NoteType] = None,
        types: Optional[list[NoteType]] = None,
        is_archived: bool = False,
        limit: int = 100,
    ) -> list[Note]:
        """按条件查询（限定 user_id + persona_id）。

        Args:
            user_id: 用户 id（必填，自动限定）
            persona_id: persona id（必填，自动限定）
            type: 单选 type 过滤（与 types 二选一）
            types: 多选 type 过滤（IN 子句）
            is_archived: 默认 False（不含归档）
            limit: 最多返回多少条

        Returns:
            按 updated_at DESC 排序的笔记列表
        """
        return self.note_repo.find(
            user_id=user_id,
            persona_id=persona_id,
            type=type,
            types=types,
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
        return self.note_repo.search(
            query=query,
            user_id=user_id,
            persona_id=persona_id,
            limit=limit,
        )

    # === 更新（1 个）===

    def update_note(
        self,
        note_id: str,
        user_id: str,
        *,
        content: Optional[str] = None,
        importance: Optional[float] = None,
        source_quote: Optional[str] = None,
    ) -> None:
        """更新笔记字段（带权限校验）。

        校验：note 必须属于 user。
        仅更新提供的字段（其他字段保持不变）。
        updated_at 自动刷新。

        Raises:
            PermissionError: note 不存在 / 不属于 user
            ValueError: importance 越界
        """
        self.authorizer.require_note_owner(note_id, user_id)
        if importance is not None:
            self._check_importance(importance)
        self.note_repo.update(
            note_id,
            content=content,
            importance=importance,
            source_quote=source_quote,
        )

    # === 删除（2 个）===

    def archive_note(self, note_id: str, user_id: str) -> bool:
        """软删除（带权限校验）。

        Returns:
            True if 成功（之前未归档）
            False if 之前已归档

        Raises:
            PermissionError: note 不存在 / 不属于 user
        """
        self.authorizer.require_note_owner(note_id, user_id)
        return self.note_repo.archive(note_id)

    def unarchive_note(self, note_id: str, user_id: str) -> bool:
        """恢复（带权限校验）。

        Returns:
            True if 成功（之前已归档）
            False if 之前未归档

        Raises:
            PermissionError: note 不存在 / 不属于 user
        """
        self.authorizer.require_note_owner(note_id, user_id)
        return self.note_repo.unarchive(note_id)

    # === 内部 helper（LLM 不可见）===

    @staticmethod
    def _check_importance(importance: float) -> None:
        """校验 importance 在 [0, 1]。"""
        if not 0 <= importance <= 1:
            raise ValueError(
                f"importance 必须在 [0, 1] 之间, 实际 {importance}"
            )


__all__ = ["NoteService"]