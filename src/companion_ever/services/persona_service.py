"""跨多 repo 的 persona 管理。"""
from __future__ import annotations

from companion_ever.services.authz import Authorizer
from companion_ever.services.note_service import NoteService
from companion_ever.domain.enums import BlockType
from companion_ever.storage.block_repo import BlockRepo
from companion_ever.storage.user_repo import UserRepo


class PersonaService:
    """跨多 repo 的 persona 管理。

    单一职责：
    - 创建一个完整的 persona 状态（涉及 3 张表 + 复用 NoteService）
    - 检查 user 是否拥有 persona（其他 service 调用）
    - 列出 user 的所有 persona
    """

    def __init__(
        self,
        user_repo: UserRepo,
        block_repo: BlockRepo,
        note_service: NoteService,
        authorizer: Authorizer,
    ):
        self.user_repo = user_repo
        self.block_repo = block_repo
        self.note_service = note_service
        self.authorizer = authorizer

    # === 用例：创建 persona ===

    def create_persona(
        self,
        user_id: str,
        persona_id: str,
        definition: str,
    ) -> None:
        """创建一个完整的 persona 状态。

        编排：
        1. 校验 definition 非空
        2. user_repo.exists(user_id)             校验用户存在
        3. user_repo.owns_persona(...)             校验不重复
        4. user_repo.add_persona(...)             注册归属
        5. note_service.create_persona_basics_note(...) 写定义笔记
        6. block_repo.upsert x 3                  初始化 3 个 Block

        Args:
            user_id: 用户 id
            persona_id: persona id（同一 user 内不可重复）
            definition: persona_basics 笔记的内容

        Raises:
            ValueError: definition 空 / user 不存在 / 已拥有 persona
        """
        if not definition or not definition.strip():
            raise ValueError("definition 不能为空")

        # 1. 用户存在
        if not self.user_repo.exists(user_id):
            raise ValueError(f"User '{user_id}' 不存在")

        # 2. 幂等：user 不能已有这个 persona
        if self.user_repo.owns_persona(user_id, persona_id):
            raise ValueError(
                f"User '{user_id}' 已拥有 persona '{persona_id}'"
            )

        # 3. 先注册归属（NoteService 写入笔记前会校验权限）
        self.user_repo.add_persona(user_id, persona_id)

        # 4. 写 persona_basics 笔记（通过 NoteService）
        self.note_service.create_persona_basics_note(
            user_id=user_id,
            persona_id=persona_id,
            content=definition.strip(),
        )

        # 5. 初始化 3 个 Block（空 fields，由后续 LLM 抽取填入）
        for block_type in (
            BlockType.PERSONA_RUNTIME,
            BlockType.USER_PROFILE,
            BlockType.RELATIONSHIP_STATE,
        ):
            self.block_repo.upsert(
                user_id=user_id,
                persona_id=persona_id,
                block_type=block_type,
                fields={},
            )

    # === 权限校验（其他 service 调用）===

    def exists(self, user_id: str, persona_id: str) -> bool:
        """user 是否拥有 persona_id。

        这是其他 service 做权限校验的统一入口。
        """
        return self.authorizer.owns_persona(user_id, persona_id)

    # === 列表 ===

    def list_user_personas(self, user_id: str) -> list[str]:
        """列出 user 拥有的所有 persona_id。"""
        return self.user_repo.list_personas(user_id)


__all__ = ["PersonaService"]