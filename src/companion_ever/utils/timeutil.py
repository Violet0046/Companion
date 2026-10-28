"""时间工具：DB 里存的是 UTC，展示时统一转成本地时区。

为什么需要这个：
- SQLite 的 ``CURRENT_TIMESTAMP`` 返回的是 **UTC**
- 但用户看到的时间、模型的 prompt 里说的时间，都应该是**本地时区**
- 提示词那边早就转了（prompt_service._utc_ts_to_local），
  但 API 层漏了 —— 结果前端把 UTC 当本地显示，所有时间都差一个时区。

约定：**DB 存 UTC，出库一律转本地**。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_ts_to_local(ts_str: str | None, tz_offset_hours: int) -> str:
    """把 SQLite 的 UTC 时间字符串（"YYYY-MM-DD HH:MM:SS"）转成本地时区。

    Args:
        ts_str: DB 里的 UTC 时间字符串
        tz_offset_hours: 用户的时区偏移（东八区 = 8）

    Returns:
        本地时区的时间字符串；解析不了就原样返回（不猜、不吞错）。
    """
    if not ts_str:
        return ""
    try:
        ts_clean = str(ts_str).split(".")[0]
        dt_utc = datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc,
        )
        local_dt = dt_utc.astimezone(timezone(timedelta(hours=tz_offset_hours)))
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError, TypeError):
        return str(ts_str)


def tz_modifier(tz_offset_hours: int) -> str:
    """给 SQLite ``datetime(col, ?)`` 用的时区修饰符。

    例：东八区 -> "+8 hours"；零偏移 -> "+0 hours"。
    """
    return f"{tz_offset_hours:+d} hours"


__all__ = ["utc_ts_to_local", "tz_modifier"]
