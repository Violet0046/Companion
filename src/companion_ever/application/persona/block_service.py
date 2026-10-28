"""application/persona/block_service.py — Memory Block 的读写与合并策略。

Block 是"永远注入 prompt 的紧凑画像"，note 是"按需召回的事实库"。
两者的区别决定了 Block 的更新方式必须和 note 不同。

合并策略（为什么不能只靠 LLM）
--------------------------------
Block 的字段级 merge 是 `{**existing, **new}`，但**字段内部的 list 是整体替换**。
所以如果 LLM 每次只写"这次会话观察到的新东西"，旧的画像就会被冲掉。

两道防线：

1. **读-改-写**（主）：抽取时把现有 Block 内容喂给 LLM，让它在旧的基础上改
   —— 它能主动移除已过时的信息。
2. **应用层合并策略**（兜底）：不信任 LLM 的输出格式
   - 关系里程碑（key_moments）本质是累积的 → 新旧取并集，永不丢失"第一次"
   - 关系进度（stage_progress）只能递增，不能倒退
   - 所有 list 去重 + 限长，防止无限膨胀
"""
from __future__ import annotations

import json
from typing import Any, Optional

from companion_ever.application.authz import Authorizer
from companion_ever.domain.entities import Block
from companion_ever.domain.enums import BlockType
from companion_ever.infrastructure.persistence.block_repo import BlockRepo


# ---- 合并策略表 ----

# 本质是累积的字段：{block_type: {field: 上限}}
# 里程碑是"第一次发生的事"，不会因为时间过去而变假，所以取并集。
UNION_FIELDS: dict[BlockType, dict[str, int]] = {
    BlockType.RELATIONSHIP_STATE: {"key_moments": 12},
}

# 只允许单调不减的字段
MONOTONIC_FIELDS: dict[BlockType, set[str]] = {
    BlockType.RELATIONSHIP_STATE: {"stage_progress"},
}

# list 字段的长度上限，防止无限膨胀
LIST_CAPS: dict[BlockType, dict[str, int]] = {
    BlockType.USER_PROFILE: {
        "personality": 8,
        "preferences": 8,
        "communication_style_hint": 6,
    },
    BlockType.RELATIONSHIP_STATE: {"key_moments": 12},
}


def _dedup(items: list) -> list:
    """去重但保持顺序（字符串按原样比较，其他按 repr）。"""
    seen: set[str] = set()
    out: list = []
    for it in items:
        key = it if isinstance(it, str) else json.dumps(it, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _merge_field(
    block_type: BlockType,
    field: str,
    old_value: Any,
    new_value: Any,
) -> Any:
    """按策略合并单个字段。"""
    # 1. 单调递增
    if field in MONOTONIC_FIELDS.get(block_type, set()):
        if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
            return max(old_value, new_value)
        return new_value

    # 2. list 字段
    if isinstance(old_value, list) and isinstance(new_value, list):
        cap = LIST_CAPS.get(block_type, {}).get(field)
        union = UNION_FIELDS.get(block_type, {}).get(field)
        if union:
            # 累积型：旧 + 新，取并集（旧的"第一次"不会丢）
            merged = _dedup(list(old_value) + list(new_value))
        else:
            # 可重写型：LLM 已经看过旧值并做过判断，以它为准
            merged = _dedup(list(new_value))
        if cap:
            merged = merged[:cap]
        return merged

    # 3. 标量：新的覆盖旧的
    return new_value


class BlockService:
    """Memory Block 的用例封装。"""

    def __init__(self, authorizer: Authorizer, block_repo: BlockRepo):
        self.authorizer = authorizer
        self.block_repo = block_repo

    def get_blocks(self, user_id: str, persona_id: str) -> list[Block]:
        """读该 (user, persona) 的所有 Block。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.block_repo.get_all(user_id=user_id, persona_id=persona_id)

    def get_block(
        self,
        user_id: str,
        persona_id: str,
        block_type: BlockType,
    ) -> Optional[Block]:
        """读单个 Block。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.block_repo.get(user_id, persona_id, block_type)

    def update_block_fields(
        self,
        user_id: str,
        persona_id: str,
        block_type: BlockType,
        fields: dict[str, Any],
    ) -> None:
        """更新 Block 的 fields。

        合并语义：**字段级 merge + 字段内按策略合并**
        （而不是简单的 `{**old, **new}`，那样 list 会被整体替换）

        Args:
            user_id, persona_id: 用户 + persona
            block_type: 3 种 BlockType 之一
            fields: 新字段（dict）— 会和现有字段合并

        Raises:
            PermissionError: user 不拥有 persona
        """
        self.authorizer.require_persona(user_id, persona_id)

        existing = self.block_repo.get(user_id, persona_id, block_type)
        if existing:
            merged: dict[str, Any] = {}
            for k, new_v in fields.items():
                if k in existing.fields:
                    merged[k] = _merge_field(
                        block_type, k, existing.fields[k], new_v,
                    )
                else:
                    merged[k] = new_v
            # 没被本次更新的字段保持原样
            for k, v in existing.fields.items():
                merged.setdefault(k, v)
        else:
            merged = dict(fields)

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
        """整体替换 Block 的 fields（不做任何合并）。"""
        self.authorizer.require_persona(user_id, persona_id)
        self.block_repo.upsert(
            user_id=user_id,
            persona_id=persona_id,
            block_type=block_type,
            fields=dict(fields),
        )


__all__ = ["BlockService", "UNION_FIELDS", "MONOTONIC_FIELDS", "LIST_CAPS"]
