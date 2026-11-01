"""user 表的 CRUD + persona_ids CSV 管理。"""
from __future__ import annotations

import sqlite3
from typing import Optional

from companion_ever.domain.entities import User



def row_to_user(row: sqlite3.Row) -> User:
    """user 表行 → User 实体。"""
    return User(
        id=row["id"],
        persona_ids=_parse_csv(row["persona_ids"]),
    )


def _parse_csv(s: str) -> list[str]:
    """'a,b,c' → ['a', 'b', 'c']; '' 或 None → []。"""
    if not s:
        return []
    return [item.strip() for item in s.split(",") if item.strip()]



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

    def display_name(self, user_id: str) -> str:
        """Keep internal account IDs out of user-facing persona descriptions."""
        row = self.conn.execute('SELECT coalesce(login_name,id) AS name FROM user WHERE id=?', (user_id,)).fetchone()
        return row['name'] if row else user_id

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

        角色业务入口在同一写事务中调用；底层方法不单独提交。

        Raises:
            ValueError: user 不存在 / 已拥有此 persona / persona_id 空
        """
        if not persona_id or not persona_id.strip() or ',' in persona_id:
            raise ValueError("persona_id 不能为空或含逗号")
        persona_id = persona_id.strip()

        personas = self.list_personas(user_id)
        if not personas and not self.exists(user_id):
            raise ValueError(f"User '{user_id}' 不存在")
        if persona_id in personas:
            raise ValueError(
                f"User '{user_id}' 已拥有 persona '{persona_id}'"
            )

        personas.append(persona_id)
        # read-modify-write 由上层角色事务保护。
        self.conn.execute(
            "UPDATE user SET persona_ids = ? WHERE id = ?",
            (",".join(personas), user_id),
        )

    def remove_persona(self, user_id: str, persona_id: str) -> None:
        """从 user 移除 persona_id。

        仅修改有效角色列表；业务调用必须经过 PersonaService.deactivate，
        在同一事务中使在途执行失效。历史数据继续保留，角色授权拒绝停用后的访问。

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
