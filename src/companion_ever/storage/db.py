"""数据库连接与 schema 应用。"""
from __future__ import annotations

import sqlite3
import threading
from importlib import resources
from pathlib import Path


def _read_schema() -> str:
    """读取包内的 schema.sql。"""
    return (
        resources.files("companion_ever.storage")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )


def connect(
    db_path: Path | str,
    *,
    check_same_thread: bool = True,
    timeout: float = 30.0,
) -> sqlite3.Connection:
    """建立 SQLite 连接。

    自动设置：
    - row_factory = Row（dict-like 访问）
    - PRAGMA foreign_keys = ON（CHECK 约束必须开）
    - PRAGMA journal_mode = WAL（并发读友好）
    - timeout：写锁等待时间（多个连接同时写时排队而不是直接报错）
    """
    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,  # autocommit；Repo 自己控制事务
        check_same_thread=check_same_thread,
        timeout=timeout,
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


class ThreadLocalConnection:
    """每个线程一条自己的 SQLite 连接（代理对象）。

    为什么需要这个：
    **sqlite3 的连接对象不能被多个线程同时使用** —— 哪怕
    ``check_same_thread=False`` 也不行，同时用会随机报
    ``bad parameter or other API misuse``（连接内部状态被搅乱）。

    而我们这里有好几个线程会碰数据库：
    - FastAPI 的同步路由（跑在 anyio 线程池里）
    - WebSocket 的生产者线程
    - 后台"记录"线程（``close_session_background``）
    - 服务器的定时清扫任务

    所以改成「每个线程一条自己的连接」。
    SQLite 本身用 WAL 模式，多条连接并发读写是安全的
    （读不阻塞写；写与写之间靠 busy timeout 排队）。

    用法跟 sqlite3.Connection 一样（execute / executemany / commit …）。
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        check_same_thread: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self._path = str(db_path)
        self._check = check_same_thread
        self._timeout = timeout
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(
                self._path,
                check_same_thread=self._check,
                timeout=self._timeout,
            )
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    # --- sqlite3.Connection 的常用接口 ---
    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._conn.executescript(*args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._all.clear()

    def __getattr__(self, name: str):
        # 其余属性（row_factory 等）转发到当前线程的连接
        return getattr(self._conn, name)


def init_db(db_path: Path | str, *, check_same_thread: bool = True):
    """初始化数据库：建连接 + 应用 schema。

    Returns:
        一个**线程安全**的连接代理（每个线程一条自己的连接）。

    Raises:
        FileNotFoundError: schema.sql 找不到（包安装错误）
    """
    conn = connect(db_path, check_same_thread=check_same_thread)
    try:
        apply_schema(conn)
    finally:
        conn.close()
    # 之后每个线程各自建连接（schema 已经写进文件了）
    return ThreadLocalConnection(db_path, check_same_thread=check_same_thread)


__all__ = ["connect", "apply_schema", "init_db"]