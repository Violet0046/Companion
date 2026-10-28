"""infrastructure/persistence/setting_repo.py — user_settings 表 CRUD。

这张表存的是"展示偏好"，不是业务数据：
- 数据两边都存着（conversation_log 的干净正文 + thinking_log 的思考过程）
- 这里只决定"给不给你看"
"""
from __future__ import annotations

import sqlite3
from typing import Any


class SettingRepo:
    """user_settings 表 CRUD。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, *, user_id: str, persona_id: str) -> dict[str, Any]:
        """读设置（没有记录时返回默认值）。"""
        row = self.conn.execute(
            "SELECT show_thinking FROM user_settings "
            "WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        ).fetchone()
        if row is None:
            return {"show_thinking": True}
        return {"show_thinking": bool(row["show_thinking"])}

    def set_show_thinking(
        self, *, user_id: str, persona_id: str, show: bool,
    ) -> dict[str, Any]:
        """写 show_thinking（upsert）。"""
        self.conn.execute(
            "INSERT INTO user_settings (user_id, persona_id, show_thinking, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id, persona_id) DO UPDATE SET "
            "  show_thinking = excluded.show_thinking, "
            "  updated_at = CURRENT_TIMESTAMP",
            (user_id, persona_id, 1 if show else 0),
        )
        return {"show_thinking": bool(show)}


__all__ = ["SettingRepo"]
