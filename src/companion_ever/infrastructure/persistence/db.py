"""数据库连接与 schema 应用。

设计要点：
- schema.sql 放在 persistence/ 包内（importlib.resources 读取）
- 连接默认开启 PRAGMA foreign_keys（CHECK 约束必须）
- 用 WAL 模式（允许并发读）
- check_same_thread 默认 False
  → 需要跨线程用同一条连接（Web 层、会话自动关闭的后台线程）
  → SQLite 本身是 serialized 模式（threadsafety=3），跨线程安全
"""
from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


def _read_schema() -> str:
    """读取包内的 schema.sql。"""
    return (
        resources.files("companion_ever.infrastructure.persistence")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )


def connect(db_path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """建立 SQLite 连接。

    自动设置：
    - row_factory = Row（dict-like 访问）
    - PRAGMA foreign_keys = ON（CHECK 约束必须开）
    - PRAGMA journal_mode = WAL（并发读友好）
    """
    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,  # autocommit；Repo 自己控制事务
        check_same_thread=check_same_thread,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """应用 schema.sql（执行所有 CREATE TABLE/INDEX/TRIGGER）。

    多次执行是幂等的（所有 DDL 都用 IF NOT EXISTS）。
    """
    conn.executescript(_read_schema())


def init_db(db_path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """初始化数据库：连接 + 应用 schema。

    Returns:
        一个配置好的 sqlite3.Connection

    Raises:
        FileNotFoundError: schema.sql 找不到（包安装错误）
    """
    conn = connect(db_path, check_same_thread=check_same_thread)
    apply_schema(conn)
    return conn


__all__ = ["connect", "apply_schema", "init_db"]