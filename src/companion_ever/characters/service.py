"""角色配置与生命周期用例。跨模块操作由装配入口显式注入。"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from companion_ever.characters.access import Authorizer
from companion_ever.characters.repository import PersonaRepo
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.characters.users import UserRepo


class PersonaService:
    def __init__(
        self,
        user_repo: UserRepo,
        persona_repo: PersonaRepo,
        authorizer: Authorizer,
        *,
        on_created: Iterable[Callable[[str, str], None]] = (),
        on_deactivated: Iterable[Callable[[str, str], None]] = (),
        on_restored: Iterable[Callable[[str, str], None]] = (),
    ):
        self.user_repo = user_repo
        self.persona_repo = persona_repo
        self.authorizer = authorizer
        self._on_created = tuple(on_created)
        self._on_deactivated = tuple(on_deactivated)
        self._on_restored = tuple(on_restored)

    def create_persona(self, user_id: str, persona_id: str, definition: str) -> None:
        if not isinstance(persona_id, str) or not persona_id.strip() or ',' in persona_id:
            raise ValueError('persona_id 不能为空或含逗号')
        persona_id = persona_id.strip()
        if not isinstance(definition, str) or not definition.strip():
            raise ValueError('角色设定不能为空')
        with transaction(self.persona_repo.conn):
            if not self.user_repo.exists(user_id):
                raise ValueError(f"User '{user_id}' 不存在")
            if self.exists(user_id, persona_id):
                raise ValueError('角色已启用')
            if self.persona_repo.get(user_id, persona_id) is not None:
                raise ValueError('角色设定已保留，请恢复角色，不能用创建覆盖')
            self.user_repo.add_persona(user_id, persona_id)
            self.persona_repo.put(user_id, persona_id, definition)
            for initialize in self._on_created:
                initialize(user_id, persona_id)

    def get_definition(self, user_id: str, persona_id: str) -> dict | None:
        self.authorizer.require_persona(user_id, persona_id)
        return self.persona_repo.get(user_id, persona_id)

    def set_definition(self, user_id: str, persona_id: str, content: str, *, expected_revision: int | None = None) -> str:
        with transaction(self.persona_repo.conn):
            self.authorizer.require_persona(user_id, persona_id)
            current = self.persona_repo.get(user_id, persona_id)
            if expected_revision is not None and (current is None or current['revision'] != expected_revision):
                raise ValueError('角色状态或设定已改变，拒绝旧编辑结果')
            return self.persona_repo.put(user_id, persona_id, content)

    def deactivate(self, user_id: str, persona_id: str) -> None:
        """撤销访问并使旧执行失效；钩子必须使用同一连接事务且不能调用模型。"""
        with transaction(self.persona_repo.conn):
            self.authorizer.require_persona(user_id, persona_id)
            self.user_repo.remove_persona(user_id, persona_id)
            self.persona_repo.invalidate_edits(user_id, persona_id)
            for invalidate in self._on_deactivated:
                invalidate(user_id, persona_id)

    def restore(self, user_id: str, persona_id: str) -> None:
        """重新授权，保留内容；未完成整理可重新排队，旧执行不能写回。"""
        with transaction(self.persona_repo.conn):
            if not self.user_repo.exists(user_id):
                raise ValueError(f"User '{user_id}' 不存在")
            if self.exists(user_id, persona_id):
                raise ValueError('角色已启用')
            if self.persona_repo.get(user_id, persona_id) is None:
                raise ValueError('没有可恢复的角色设定')
            self.user_repo.add_persona(user_id, persona_id)
            for resume in self._on_restored:
                resume(user_id, persona_id)

    def exists(self, user_id: str, persona_id: str) -> bool:
        return self.authorizer.owns_persona(user_id, persona_id)

    def list_user_personas(self, user_id: str) -> list[str]:
        return self.user_repo.list_personas(user_id)

    def list_users(self):
        """现有联系人入口的用户列表，不读取角色的历史留存数据。"""
        return self.user_repo.list_all()
