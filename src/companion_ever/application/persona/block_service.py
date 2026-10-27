"""application/persona/block_service.py — 3 个 memory blocks 的用例封装 + LLM 友好接口。

LLM 调用场景：
- LLM 分析对话后生成新的 fields dict
- 调用 update_block_fields(user_id, persona_id, block_type, fields={...})
- 自动 JSON 序列化 + 写入 DB

字段类型约定：
- repo 层返回 Block 实体（fields 已 JSON 解析为 dict）
- service 层直接用 dict，无需再 json.loads
"""
from __future__ import annotations

from typing import Any, Optional

from companion_ever.application.authz import Authorizer
from companion_ever.domain.entities import Block
from companion_ever.domain.enums import BlockType
from companion_ever.infrastructure.persistence.block_repo import BlockRepo


class BlockService:
    """memory_block 的 LLM-friendly 操作。"""

    def __init__(self, authorizer: Authorizer, block_repo: BlockRepo):
        self.authorizer = authorizer
        self.block_repo = block_repo

    # === 读取（LLM 上下文用）===

    def get_blocks(self, user_id: str, persona_id: str) -> list[Block]:
        """获取 (user, persona) 的全部 Block。"""
        return self.block_repo.get_all(user_id, persona_id)

    def get_block(
        self,
        user_id: str,
        persona_id: str,
        block_type: BlockType,
    ) -> Optional[Block]:
        """获取单个 Block。

        Returns:
            None if 不存在
            否则 Block 实体（fields 已 dict）
        """
        return self.block_repo.get(user_id, persona_id, block_type)

    # === 写入（LLM 主要调用）===

    def update_block_fields(
        self,
        user_id: str,
        persona_id: str,
        block_type: BlockType,
        fields: dict[str, Any],
    ) -> None:
        """更新 Block 的 fields（merge，不是覆盖）。

        行为：
        - 已有 fields 与新 fields 合并（新覆盖旧）
        - LLM 可以只传"想改的几个字段"，其他保留

        Args:
            user_id, persona_id: 用户 + persona
            block_type: 3 种 BlockType 之一
            fields: 新字段（dict）— 会和现有字段合并

        Raises:
            PermissionError: user 不拥有 persona
        """
        self.authorizer.require_persona(user_id, persona_id)

        # 获取现有 fields（如果有）
        existing = self.block_repo.get(user_id, persona_id, block_type)
        if existing:
            merged = {**existing.fields, **fields}
        else:
            merged = fields

        # 写回
        self.block_repo.upsert(
            user_id=user_id,
            persona_id=persona_id,
            block_type=block_type,
            fields=merged,
        )

    def replace_block_fields(
        self,
        user_id: str,
        persona_id: str,
        block_type: BlockType,
        fields: dict[str, Any],
    ) -> None:
        """替换 Block 的 fields（完全覆盖，不是 merge）。

        适用场景：完全重写某个 Block 的内容。

        Raises:
            PermissionError: user 不拥有 persona
        """
        self.authorizer.require_persona(user_id, persona_id)
        self.block_repo.upsert(
            user_id=user_id,
            persona_id=persona_id,
            block_type=block_type,
            fields=fields,
        )

    # === 初始化（persona_service 调）===

    def initialize_blocks(
        self,
        user_id: str,
        persona_id: str,
    ) -> None:
        """初始化 3 个 Block 为空 fields。

        调用场景：PersonaService.create_persona 内部调。
        如果 Block 已存在则不动（idempotent）。
        """
        for bt in (
            BlockType.PERSONA_RUNTIME,
            BlockType.USER_PROFILE,
            BlockType.RELATIONSHIP_STATE,
        ):
            self.block_repo.upsert(
                user_id=user_id,
                persona_id=persona_id,
                block_type=bt,
                fields={},
            )


__all__ = ["BlockService"]