"""block_repo.py — memory_block 表的 CRUD。

设计要点：
- 每个 (user_id, persona_id) 最多有 3 条 Block（persona_runtime / user_profile / relationship_state）
- UNIQUE(user_id, persona_id, block_type) 保证唯一性
- upsert 语义：插入或更新
- 返回 Block 实体（fields 已 JSON 解析为 dict；调用方无需再 json.loads）
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Optional

from companion_ever.domain.entities import Block
from companion_ever.domain.enums import BlockType
from .mappers import row_to_block


def _new_block_id() -> str:
    """生成新的 block id（UUID 格式）。"""
    return f"block_{uuid.uuid4().hex[:12]}"


class BlockRepo:
    """memory_block 表 CRUD。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # === 查询 ===

    def get(
        self,
        user_id: str,
        persona_id: str,
        block_type: BlockType,
    ) -> Optional[Block]:
        """获取单个 Block。

        Returns:
            None if 不存在
            否则 Block 实体（fields 已 JSON 解析）
        """
        row = self.conn.execute(
            "SELECT id, user_id, persona_id, block_type, fields_json, updated_at "
            "FROM memory_block "
            "WHERE user_id = ? AND persona_id = ? AND block_type = ?",
            (user_id, persona_id, block_type.value),
        ).fetchone()
        return row_to_block(row) if row else None

    def get_all(self, user_id: str, persona_id: str) -> list[Block]:
        """获取 (user, persona) 的全部 Block（最多 3 个）。

        按 block_type 排序（便于 system prompt 拼接）。
        """
        rows = self.conn.execute(
            "SELECT id, user_id, persona_id, block_type, fields_json, updated_at "
            "FROM memory_block "
            "WHERE user_id = ? AND persona_id = ? "
            "ORDER BY block_type",
            (user_id, persona_id),
        ).fetchall()
        return [row_to_block(r) for r in rows]

    # === 写入 ===

    def upsert(
        self,
        user_id: str,
        persona_id: str,
        block_type: BlockType,
        fields: dict[str, Any] | str,
    ) -> None:
        """插入或更新一个 Block。

        如果不存在 → INSERT（生成新 id）
        如果存在 → UPDATE（fields_json + updated_at）

        注：FK 要求 user_id 在 user 表里存在，persona_id 由应用层校验归属。
        此方法不校验 persona 归属（由 service 层负责）。

        Args:
            user_id: 用户 id
            persona_id: persona id
            block_type: 3 种之一
            fields: 字段 dict（或已序列化的 JSON 字符串）
        """
        if isinstance(fields, dict):
            fields_json = json.dumps(fields, ensure_ascii=False)
        else:
            fields_json = fields

        existing = self.get(user_id, persona_id, block_type)
        if existing is None:
            # INSERT
            self.conn.execute(
                "INSERT INTO memory_block "
                "(id, user_id, persona_id, block_type, fields_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    _new_block_id(),
                    user_id,
                    persona_id,
                    block_type.value,
                    fields_json,
                ),
            )
        else:
            # UPDATE
            self.conn.execute(
                "UPDATE memory_block "
                "SET fields_json = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = ? AND persona_id = ? AND block_type = ?",
                (
                    fields_json,
                    user_id,
                    persona_id,
                    block_type.value,
                ),
            )

    # === 删除 ===

    def delete(
        self,
        user_id: str,
        persona_id: str,
        block_type: BlockType,
    ) -> bool:
        """删除单个 Block。

        Returns:
            True if 删除成功
            False if 不存在
        """
        cur = self.conn.execute(
            "DELETE FROM memory_block "
            "WHERE user_id = ? AND persona_id = ? AND block_type = ?",
            (user_id, persona_id, block_type.value),
        )
        return cur.rowcount > 0

    def delete_all(self, user_id: str, persona_id: str) -> int:
        """删除 (user, persona) 的全部 Block（最多 3 个）。

        Returns:
            删除的 Block 数量
        """
        cur = self.conn.execute(
            "DELETE FROM memory_block WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        )
        return cur.rowcount


__all__ = ["BlockRepo"]