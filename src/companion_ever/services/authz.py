"""集中化的授权检查。"""
from __future__ import annotations

from typing import Optional

from companion_ever.storage.note_repo import NoteRepo
from companion_ever.storage.user_repo import UserRepo


class Authorizer:
    """授权检查的单一入口。"""

    def __init__(self, user_repo: UserRepo, note_repo: Optional[NoteRepo] = None):
        self.user_repo = user_repo
        self.note_repo = note_repo

    def require_persona(self, user_id: str, persona_id: str) -> None:
        """校验 user_id 存在且拥有 persona_id。

        主动优先使用**权威来源**：persona_basics note 存在 → 这个 persona 确实存在。
        `user.persona_ids` 只是一个**反规范化的 CSV 缓存**，可能与真实数据漂移，
        而一旦漂移，整个系统就会报“用户不拥有 persona”——即使用户之前
        真的用过这个 persona。

        Raises:
            PermissionError: user 不存在 / 不拥有 persona
        """
        if not self.user_repo.exists(user_id):
            raise PermissionError(f"User '{user_id}' 不存在")
        if self.user_repo.owns_persona(user_id, persona_id):
            return
        # CSV 缓存不一致 → 用权威来源再确认一次
        if self._persona_exists_in_data(user_id, persona_id):
            return
        raise PermissionError(
            f"User '{user_id}' 不拥有 persona '{persona_id}'"
        )

    def _persona_exists_in_data(self, user_id: str, persona_id: str) -> bool:
        """权威判定：这个 (user, persona) 真的有数据吗。"""
        if self.note_repo is None:
            return False
        from companion_ever.storage.persona_repo import PersonaRepo
        return PersonaRepo(self.note_repo.conn).get(user_id,persona_id) is not None

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