"""application/authz.py — 集中化的授权检查。

消除各 service 中重复的 _check_permission / _check_note_owner。

公开 API：
- require_persona(user_id, persona_id): 校验用户存在且拥有 persona，失败抛 PermissionError
- require_note_owner(note_id, user_id): 校验 note 存在且属于 user，失败抛 PermissionError
- owns_persona(user_id, persona_id): 非抛错版（用于 PersonaService.exists 这种合法"还没拥有"的场景）
"""
from __future__ import annotations

from typing import Optional

from companion_ever.infrastructure.persistence.note_repo import NoteRepo
from companion_ever.infrastructure.persistence.user_repo import UserRepo


class Authorizer:
    """授权检查的单一入口。"""

    def __init__(self, user_repo: UserRepo, note_repo: Optional[NoteRepo] = None):
        self.user_repo = user_repo
        self.note_repo = note_repo

    def require_persona(self, user_id: str, persona_id: str) -> None:
        """校验 user_id 存在且拥有 persona_id。

        Raises:
            PermissionError: user 不存在 / 不拥有 persona
        """
        if not self.user_repo.exists(user_id):
            raise PermissionError(f"User '{user_id}' 不存在")
        if not self.user_repo.owns_persona(user_id, persona_id):
            raise PermissionError(
                f"User '{user_id}' 不拥有 persona '{persona_id}'"
            )

    def require_note_owner(self, note_id: str, user_id: str) -> None:
        """校验 note 存在且属于 user。

        Raises:
            PermissionError: note 不存在 / 不属于 user
        """
        if self.note_repo is None:
            raise RuntimeError(
                "Authorizer.require_note_owner 需要 note_repo；"
                "构造 Authorizer 时请传入 note_repo 参数"
            )
        note = self.note_repo.get(note_id)
        if note is None:
            raise PermissionError(f"Note '{note_id}' 不存在")
        if note.user_id != user_id:
            raise PermissionError(
                f"Note '{note_id}' 不属于 user '{user_id}'"
            )

    def owns_persona(self, user_id: str, persona_id: str) -> bool:
        """非抛错版——用于 PersonaService.exists 这种合法"还没拥有"的场景。"""
        return self.user_repo.owns_persona(user_id, persona_id)


__all__ = ["Authorizer"]