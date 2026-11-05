"""角色配置与生命周期用例。跨模块操作由装配入口显式注入。"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import json
import uuid

from companion_ever.characters.access import Authorizer
from companion_ever.characters.repository import PersonaRepo
from companion_ever.characters.templates import (
    TEMPLATE_FIELDS, USER_PLACEHOLDER, build_definition, template_cards)
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.characters.users import UserRepo


def _basics_of(content: str) -> dict:
    """角色设定文本 → dict；不是 JSON 时原样留着。"""
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return {'_raw': content}
    return parsed if isinstance(parsed, dict) else {'_raw': content}


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
        self.before_deactivate = None

    def create_persona(self, user_id: str, persona_id: str, definition: str) -> None:
        """Insert an explicit storage identity (imports/fixtures); public creation uses create_named."""
        if not isinstance(persona_id, str) or not persona_id.strip() or ',' in persona_id:
            raise ValueError('persona_id 不能为空或含逗号')
        persona_id = persona_id.strip()
        if not isinstance(definition, str) or not definition.strip():
            raise ValueError('角色设定不能为空')
        # {user} 占位符替换成实际 user_id——模板与自定义设定走同一条路。
        definition = definition.replace(USER_PLACEHOLDER, self.user_repo.display_name(user_id))
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

    def create_named(self, user_id, name, definition=None, template_id=None):
        """Public creation separates the chosen name from the immutable storage ID."""
        name=name.strip()
        if not name or len(name)>60 or ',' in name:raise ValueError('角色名须为1至60个字符，不能包含逗号')
        with transaction(self.persona_repo.conn):
            if not self.user_repo.exists(user_id):raise PermissionError('用户不存在')
            existing=self.persona_repo.conn.execute('SELECT persona_id FROM persona_definition WHERE user_id=? AND display_name=? AND is_archived=0',(user_id,name)).fetchone()
            if existing:
                if self.exists(user_id,existing[0]):raise ValueError('同名角色已启用')
                raise ValueError('同名角色已保留，请恢复，或彻底删除后重新创建')
            if template_id:definition=build_definition(template_id,self.user_repo.display_name(user_id))
            if not definition or not definition.strip():raise ValueError('要指定 template_id 或 definition 之一')
            basics=_basics_of(definition);basics['name']=name
            pid='role_'+uuid.uuid4().hex
            self.create_persona(user_id,pid,json.dumps(basics,ensure_ascii=False))
            self.persona_repo.conn.execute('UPDATE persona_definition SET display_name=? WHERE user_id=? AND persona_id=?',(name,user_id,pid))
            return pid

    def resolve_name(self,user_id,value):
        """CLI boundary only: internal services and workers always use exact IDs."""
        if self.exists(user_id,value):return value
        rows=self.persona_repo.conn.execute('SELECT persona_id FROM persona_definition WHERE user_id=? AND display_name=? AND is_archived=0',(user_id,value)).fetchall()
        if len(rows)==1 and self.exists(user_id,rows[0][0]):return rows[0][0]
        raise ValueError('找不到有效角色')

    def purge(self,user_id,persona_id):
        from companion_ever.characters.purge import purge_persona
        with transaction(self.persona_repo.conn):
            if not self.user_repo.exists(user_id):raise PermissionError('用户不存在')
            if self.user_repo.owns_persona(user_id,persona_id):raise ValueError('请先删除角色，再进行彻底删除')
            if self.persona_repo.get(user_id,persona_id) is None:raise LookupError('已删除的角色不存在')
            purge_persona(self.persona_repo.conn,user_id,persona_id)

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
            if self.before_deactivate is not None:self.before_deactivate(user_id,persona_id)
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

    def templates(self):
        """角色模板目录 + 自定义表单的字段描述。"""
        return {'templates': template_cards(), 'fields': TEMPLATE_FIELDS}

    def create_from_template(self, user_id: str, persona_id: str, template_id: str) -> None:
        """用内置模板创建角色（模板里的用户占位符替换成实际 user_id）。

        Raises:
            KeyError: 模板不存在
            ValueError: 用户/角色已存在或设定为空
        """
        definition = build_definition(template_id, self.user_repo.display_name(user_id))
        self.create_persona(user_id, persona_id, definition)

    def list_archived(self, user_id: str) -> list[dict]:
        """已停用（删除）的角色。

        判定：设定行还在，但不在有效角色列表里——停用只撤销访问，数据保留。
        """
        if not self.user_repo.exists(user_id):
            raise PermissionError(f"User '{user_id}' 不存在")
        active = set(self.user_repo.list_personas(user_id))
        archived = []
        for row in self.persona_repo.list_by_user(user_id):
            persona_id = row['persona_id']
            if persona_id in active:
                continue
            basics = _basics_of(row['content'])
            archived.append({
                'persona_id': persona_id,
                'name': row['display_name'] or basics.get('name') or persona_id,
                'identity': basics.get('identity', ''),
            })
        return archived
