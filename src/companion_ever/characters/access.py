"""有效角色访问检查。设定留存不授予访问权。"""
from companion_ever.characters.users import UserRepo


class Authorizer:
    def __init__(self, user_repo: UserRepo):
        self.user_repo = user_repo

    def require_persona(self, user_id: str, persona_id: str) -> None:
        if not self.user_repo.exists(user_id):
            raise PermissionError(f"User '{user_id}' 不存在")
        if not self.owns_persona(user_id, persona_id):
            raise PermissionError(f"User '{user_id}' 未启用 persona '{persona_id}'")

    def owns_persona(self, user_id: str, persona_id: str) -> bool:
        row = self.user_repo.conn.execute('SELECT disabled FROM user WHERE id=?', (user_id,)).fetchone()
        return bool(row and not row['disabled'] and self.user_repo.owns_persona(user_id, persona_id))
