"""日历回溯 + 定位跳转的回归测试

两件事锁死：

1. 「聊天记录」弹出的是**日历**（月历），不是一行行的日期列表；
   没聊过天的日期置灰且不可点。
2. 点某一天之后，是**定位到那天第一条消息**，而且视图仍然是普通聊天界面
   （能继续上下滑到全部历史），不再把范围锁在那一天里。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def main() -> int:
    from companion_ever.api.server import WEB_DIR

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")

    # --- 1. 日历弹层 ---
    assert 'id="calendar-panel"' in html, "缺少日历弹层"
    assert 'id="cal-scroll"' in html, "缺少日历滚动容器"
    assert 'id="cal-title"' in html, "缺少年份月份标题"
    assert 'id="btn-cal-today"' in html, "缺少回到本月"
    # 不再是"点按钮切月"，而是一路往上滑
    assert 'id="btn-cal-prev"' not in html and 'id="btn-cal-next"' not in html, \
        "上下月按钮应该去掉，改成一直往上滑"
    assert ".cal-day.has" in css, "缺少「有记录的日期」样式"
    assert ".cal-day.empty" in css, "缺少「没记录的日期（置灰）」样式"
    assert ".cal-day.today" in css, "缺少「今天」样式"
    # --- 2b. 无限上滑 ---
    assert "function loadOlderMonths" in js, "日历没有往上加载更早月份"
    assert "addEventListener('scroll'" in js, "日历没有滚动监听"
    assert "el.scrollTop < 140" in js, "滑到顶部没有触发加载更早的月份"
    assert "function monthHtml" in js, "日历没有按月份分块渲染"
    assert "cal-month" in js, "缺少月份分块"
    print("[OK] 日历弹层（可无限上滑 / 有记录 / 置灰 / 今天）")

    # --- 2. 有记录的日期可点，没记录的置灰不可点 ---
    assert "S.calDates[dk]" in js, "日历没按日期取记录"
    assert "has ? 'has' : 'empty'" in js, "没记录的日期没置灰"
    assert "querySelectorAll('.cal-day.has')" in js, "只有有记录的日期才能点"
    assert "data-date" in js, "日历格子没有带日期"
    print("[OK] 有记录的日期可点，没记录的置灰")

    # --- 3. 定位到那天第一条，而不是限制范围 ---
    assert "async function jumpToDate(date, firstId)" in js, "jumpToDate 签名不对"
    # before_id 是排他的（id < X），所以要 +1 才能把目标消息包进来
    assert "before_id=${firstId + 1}" in js, "没有正确取那天之前的一页（含目标消息）"
    assert "after_id=${after}" in js, "没有取那天之后的部分"
    assert "history?date=${date}" not in js, \
        "不该再用 date= 把视图限制在某一天里"
    assert "viewMode = 'date'" not in js, "'date' 视图模式已经不需要了"
    assert "loadAround" in js, "缺少定位加载逻辑"
    print("[OK] 跳转到那天第一条，且仍是普通聊天界面")

    # --- 4. 目标消息本身要在列表里（jumpToMessage 之前是个 bug）---
    assert "loadAround(Number(id))" in js, "jumpToMessage 没把目标消息本身加载进来"
    print("[OK] 搜索结果跳转也带上目标消息本身")

    # --- 5. 上下滑不被限制 ---
    assert "S.viewMode !== 'live'" not in js, \
        "loadOlder 不该再按视图模式拦截（定位后也要能往上翻）"
    print("[OK] 定位后仍能上下翻历史")

    # --- 6. 后端要给出当天第一条消息的 id ---
    from companion_ever.api.schemas import DateEntry
    assert "first_id" in DateEntry.model_fields, "DateEntry 缺 first_id"
    from companion_ever.infrastructure.persistence.conversation_repo import ConversationRepo
    import inspect
    src = inspect.getsource(ConversationRepo.list_dates)
    assert "MIN(id)" in src and "MAX(id)" in src, "list_dates 没有取当天的首/末消息 id"
    print("[OK] 后端提供每天的首/末消息 id")

    print("[ALL] 日历回溯测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())