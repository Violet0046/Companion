"""时区 + 异步记录 的回归测试

1. **时区**：DB 里存的是 UTC（SQLite CURRENT_TIMESTAMP 的行为），
   但出库必须统一转本地时区。
   以前只有提示词那边转了，API 层漏了 —— 前端把 UTC 当本地显示，
   东八区所有时间都慢 8 小时（"上次记录 8 小时前"就是这么来的）。
2. **异步记录**：抽取是一次完整 LLM 调用（几十秒），
   接口必须立刻返回，不能让用户干等着没法继续聊天。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_timeutil():
    from companion_ever.utils.timeutil import utc_ts_to_local, tz_modifier

    # 东八区：UTC 14:47 -> 本地 22:47
    assert utc_ts_to_local("2026-10-28 14:47:29", 8) == "2026-10-28 22:47:29"
    # 跨天
    assert utc_ts_to_local("2026-10-28 20:00:00", 8) == "2026-10-29 04:00:00"
    # 零偏移 = 原样
    assert utc_ts_to_local("2026-10-28 14:47:29", 0) == "2026-10-28 14:47:29"
    # 负偏移
    assert utc_ts_to_local("2026-10-28 02:00:00", -5) == "2026-10-27 21:00:00"
    # 空值和脏数据不崩
    assert utc_ts_to_local(None, 8) == ""
    assert utc_ts_to_local("乱七八糟", 8) == "乱七八糟"

    assert tz_modifier(8) == "+8 hours"
    assert tz_modifier(0) == "+0 hours"
    assert tz_modifier(-5) == "-5 hours"
    print("[OK] utils/timeutil: UTC -> 本地时区")


def test_repo_groups_by_local_date():
    """list_dates 必须按**本地日期**分组。

    UTC 20:00 在东八区是第二天凌晨 0 点 —— 按 UTC 分组会算错一天。
    """
    from companion_ever.conversations.repository import ConversationRepo
    from companion_ever.infrastructure.database.connection import init_db

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = init_db(db_path)
        repo = ConversationRepo(conn)
        conn.execute(
            "INSERT INTO user (id) VALUES ('u1')"
        )
        repo.ensure_conversation("u1", "p1")
        # 这条消息 UTC 是 10-28 20:00 -> 东八区是 10-29 04:00
        conn.execute(
            "INSERT INTO conversation_log "
            "(user_id, persona_id, turn_index, role, content, created_at,conversation_id,sender_kind,sender_id) "
            "VALUES ('u1', 'p1', 1, 'user', 'hi', '2026-10-28 20:00:00',1,'user','u1')"
        )
        conn.execute(
            "INSERT INTO conversation_log "
            "(user_id, persona_id, turn_index, role, content, created_at,conversation_id,sender_kind,sender_id) "
            "VALUES ('u1', 'p1', 1, 'assistant', 'yo', '2026-10-28 20:00:01',1,'persona','p1')"
        )

        utc_days = repo.list_dates(user_id="u1", persona_id="p1", tz_offset_hours=0)
        local_days = repo.list_dates(user_id="u1", persona_id="p1", tz_offset_hours=8)
        assert utc_days[0]["date"] == "2026-10-28", utc_days
        assert local_days[0]["date"] == "2026-10-29", local_days
        assert local_days[0]["count"] == 2
        assert local_days[0]["first_id"] is not None and local_days[0]["last_id"] is not None
        print("[OK] list_dates 按本地日期分组")
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_api_converts_timestamps():
    from companion_ever.entrypoints.web.routes.memory import _s

    assert _s("2026-10-28 14:47:29", 8) == "2026-10-28 22:47:29"
    assert _s("2026-10-28 14:47:29", 0) == "2026-10-28 14:47:29"
    assert _s(None, 8) is None
    print("[OK] API 层出库统一转本地时区")


def test_close_session_is_async():
    """/api/session/close 必须后台执行、立刻返回。"""
    import inspect
    from companion_ever.entrypoints.web.routes import chat as chat_routes

    # 只看 HTTP 路由（WS 那条 close_session 消息仍然是同步的，
    # 因为它要把抽取结果回报给客户端）
    src = inspect.getsource(chat_routes.post_close_session)
    assert "close_session_background" in src, (
        "/api/session/close 还在同步等待抽取（用户会干等着没法聊天）"
    )
    assert "close_session," not in src, "/api/session/close 还在同步调用 close_session"

    from companion_ever.memory.extraction.sessions import SessionCloseService
    svc_src = inspect.getsource(SessionCloseService)
    assert "def close_session_background" in svc_src
    assert "_closing" in svc_src, "缺少「正在记录」的守卫（会重复触发抽取）"
    print("[OK] 记录是异步的，且有重复触发守卫")


def test_frontend_uses_async_record():
    from companion_ever.entrypoints.web.server import WEB_DIR

    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "pollRecordDone" in js, "前端没有轮询等待记录完成"
    assert "await api('/api/session/close'" in js, "前端还在同步等记录完成"
    assert "Promise.allSettled" in js, "会话加载不该用 Promise.all（一个失败全挂）"
    assert "m.role === 'assistant'" in js, "未读数应该只数 TA 的回复"
    assert "tab-badge-msg" in js, "底部 Tab 没有未读角标"
    print("[OK] 前端：异步记录 + 错误不再被吞 + 未读只数回复")


def main() -> int:
    test_timeutil()
    test_repo_groups_by_local_date()
    test_api_converts_timestamps()
    test_close_session_is_async()
    test_frontend_uses_async_record()
    print("[ALL] 时区 + 异步记录测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
