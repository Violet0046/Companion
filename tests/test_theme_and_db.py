"""亮色主题 + 多线程数据库 的回归测试

1. **主题**：从深色改成明亮（小红书那挂的：白卡片 + 一点红 + 粉彩色块）。
   底色**不能是纯白**（刺眼），要柔和浅灰。

2. **数据库**：sqlite3 的连接对象不能被多个线程同时用。
   我们有 4 个线程会碰库（FastAPI 线程池 / WS 生产者 / 后台记录 / 定时清扫），
   共用一条连接会随机报 "bad parameter or other API misuse"，
   表现为「聊天记录加载不出来」「角色汇总 500」。
   现在改成**每个线程一条自己的连接**。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_light_theme():
    from companion_ever.api.server import WEB_DIR

    css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
    root = css.split(":root {")[1].split("}")[0]

    import re

    def var(name: str) -> str:
        m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{3,8}})", root)
        assert m, f"缺少变量 --{name}"
        return m.group(1).lower()

    bg = var("bg")
    assert bg != "#ffffff", "底色别用纯白（刺眼）"
    assert bg.lower() in ("#f2f3f6", "#f3f4f6", "#f4f5f7"), f"底色应该柔和浅灰，现在是 {bg}"
    assert var("bg-soft").lower() == "#ffffff", "卡片应该是纯白（浮在浅灰底上）"
    assert var("accent").lower() == "#ff2442", "强调色应该是小红书红"
    assert var("bubble-me").lower() == "#3a7dff", "我的气泡应该是蓝底白字（红底看久了刺眼）"
    assert var("bubble-me-tx").lower() == "#ffffff", "我的气泡文字应该是白色"
    assert "color-scheme: light" in css, "没告诉浏览器用浅色渲染控件和滚动条"
    print("[OK] 亮色主题（柔和浅灰底 + 纯白卡片 + 红色强调）")


def test_thread_safe_db():
    """多线程同时用数据库不能报错。"""
    from companion_ever.storage.db import init_db

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = init_db(db_path)
        conn.execute("INSERT INTO user (id) VALUES ('u1')")

        errors: list[str] = []
        barrier = threading.Barrier(8)

        def worker(idx: int) -> None:
            try:
                barrier.wait()          # 让所有线程同时开跑，最大化撞车概率
                for j in range(40):
                    conn.execute("SELECT 1").fetchone()
                    conn.execute(
                        "INSERT INTO conversation_log "
                        "(user_id, persona_id, turn_index, role, content) "
                        "VALUES ('u1', 'p1', ?, 'user', 'x')",
                        (idx * 1000 + j,),
                    )
            except Exception as e:  # noqa: BLE001
                errors.append(f"线程{idx}: {e!r}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, "并发访问数据库出错：\n" + "\n".join(errors[:5])
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM conversation_log"
        ).fetchone()["c"]
        assert n == 8 * 40, f"写丢了数据：{n} != 320"
        print("[OK] 多线程并发访问数据库没问题（每线程一条连接）")
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_thread_local_conn_is_per_thread():
    from companion_ever.storage.db import ThreadLocalConnection, init_db

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = init_db(db_path)
        assert isinstance(conn, ThreadLocalConnection), "init_db 应该返回线程安全的连接代理"

        seen: list[int] = []

        def grab() -> None:
            seen.append(id(conn._conn))   # noqa: SLF001

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(seen)) == 4, f"每个线程应该拿到自己的连接，实际 {len(set(seen))} 条"
        print("[OK] 每个线程一条独立连接")
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def main() -> int:
    test_light_theme()
    test_thread_safe_db()
    test_thread_local_conn_is_per_thread()
    print("[ALL] 主题 + 并发测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())