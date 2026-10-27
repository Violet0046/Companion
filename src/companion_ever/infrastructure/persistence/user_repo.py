"""user_repo.py — user 表的 CRUD + persona_ids CSV 管理。

设计要点：
- 单字段 PK（id TEXT）
- persona_ids 是 CSV 字符串（应用层 split/join）
- 跨用户可以同名 persona（无 UNIQUE(persona_id) 约束）
- add/remove_persona 是 read-modify-write（非原子，单进程 OK）

变化：返回 User 实体（替代原 dict）。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from companion_ever.domain.entities import User
from .mappers import row_to_user


class UserRepo:
    """user 表 CRUD + persona_ids CSV 操作。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # === 基础 CRUD ===

    def create(self, user_id: str) -> None:
        """注册新用户（persona_ids 为空）。

        Raises:
            ValueError: user_id 已存在
        """
        try:
            self.conn.execute(
                "INSERT INTO user (id, persona_ids) VALUES (?, '')",
                (user_id,),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"User '{user_id}' 已存在") from e

    def get(self, user_id: str) -> Optional[User]:
        """获取用户。

        Returns:
            None if 不存在
            否则 User 实体
        """
        row = self.conn.execute(
            "SELECT id, persona_ids FROM user WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return row_to_user(row)

    def list_all(self) -> list[User]:
        """列出所有用户。"""
        rows = self.conn.execute(
            "SELECT id, persona_ids FROM user ORDER BY id"
        ).fetchall()
        return [row_to_user(r) for r in rows]

    def exists(self, user_id: str) -> bool:
        """检查用户是否存在。"""
        row = self.conn.execute(
            "SELECT 1 FROM user WHERE id = ?",
            (user_id,),
        ).fetchone()
        return row is not None

    def delete(self, user_id: str) -> None:
        """删除用户（CASCADE 自动删除 notes/memory_blocks/conversation_log）。

        Raises:
            ValueError: 用户不存在
        """
        cur = self.conn.execute(
            "DELETE FROM user WHERE id = ?",
            (user_id,),
        )
        if cur.rowcount == 0:
            raise ValueError(f"User '{user_id}' 不存在")

    # === persona_ids CSV 操作 ===

    def list_personas(self, user_id: str) -> list[str]:
        """返回 user 拥有的所有 persona_id（CSV split）。

        用户不存在时返回 []。
        """
        row = self.conn.execute(
            "SELECT persona_ids FROM user WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return []
        s = row["persona_ids"]
        if not s:
            return []
        return [item.strip() for item in s.split(",") if item.strip()]

    def owns_persona(self, user_id: str, persona_id: str) -> bool:
        """检查 user 是否拥有 persona_id。"""
        return persona_id in self.list_personas(user_id)

    def add_persona(self, user_id: str, persona_id: str) -> None:
        """给 user 添加 persona_id（追加到 CSV 末尾）。

        应用层 read-modify-write（非原子，单进程 OK）。
        多进程场景需要事务或唯一约束保护。

        Raises:
            ValueError: user 不存在 / 已拥有此 persona / persona_id 空
        """
        if not persona_id or not persona_id.strip():
            raise ValueError("persona_id 不能为空")
        persona_id = persona_id.strip()

        personas = self.list_personas(user_id)
        if not personas and not self.exists(user_id):
            raise ValueError(f"User '{user_id}' 不存在")
        if persona_id in personas:
            raise ValueError(
                f"User '{user_id}' 已拥有 persona '{persona_id}'"
            )

        personas.append(persona_id)
        self.conn.execute(
            "UPDATE user SET persona_ids = ? WHERE id = ?",
            (",".join(personas), user_id),
        )

    def remove_persona(self, user_id: str, persona_id: str) -> None:
        """从 user 移除 persona_id。

        应用层 read-modify-write。
        注意：不会删除 user 与该 persona 相关的 notes/blocks/conversation_log。
        这些数据靠 (user_id, persona_id) 复合查询时自然被过滤。

        Raises:
            ValueError: user 不存在 / 未拥有此 persona
        """
        personas = self.list_personas(user_id)
        if not personas and not self.exists(user_id):
            raise ValueError(f"User '{user_id}' 不存在")
        if persona_id not in personas:
            raise ValueError(
                f"User '{user_id}' 未拥有 persona '{persona_id}'"
            )

        personas.remove(persona_id)
        self.conn.execute(
            "UPDATE user SET persona_ids = ? WHERE id = ?",
            (",".join(personas), user_id),
        )


__all__ = ["UserRepo"]