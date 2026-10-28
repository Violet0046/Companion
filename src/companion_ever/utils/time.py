"""utils/time.py — 时间格式化和时区处理。

设计：
- 默认 UTC+8（中国时区）
- 提供格式化当前时间、解析 naive/aware datetime 的工具
- 不依赖 pytz 等三方库（用 datetime 标准库）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def format_now(tz_offset_hours: int = 8, now: datetime | None = None) -> str:
    """格式化当前时间为可读字符串。

    Args:
        tz_offset_hours: 时区偏移小时数（默认 8 = UTC+8 中国）
        now: 要格式化的时间（默认当前时间）

    Returns:
        形如 "2026-09-28 周一 14:30 (UTC+8)"
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(timezone(timedelta(hours=tz_offset_hours)))
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    wd = weekdays[local.weekday()]
    return local.strftime(f"%Y-%m-%d {wd} %H:%M (UTC{tz_offset_hours:+d})")


def parse_sqlite_ts(ts: str | None) -> datetime | None:
    """解析 SQLite 的 UTC 时间字符串（"2026-10-28 00:08:47"）。

    返回带 UTC tzinfo 的 datetime；解析失败返回 None。
    """
    if not ts:
        return None
    try:
        return datetime.strptime(ts.split(".")[0], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, AttributeError):
        return None


def describe_gap(seconds: int) -> str:
    """把时间间隔说成中文。"""
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h} 小时" + (f" {m} 分钟" if m else "")
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d} 天" + (f" {h} 小时" if h else "")


def ensure_aware(dt: datetime | None) -> datetime | None:
    """把 naive datetime 加上 UTC tzinfo（如果是 aware 不变）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def relative_until_zh(target: datetime, now: datetime | None = None) -> str:
    """相对未来时间的中文描述（'3 天后' 等）。

    Args:
        target: 目标时间
        now: 当前时间（默认当前 UTC）

    Returns:
        形如 "3 天后" 或 "刚到" 或 "2 小时后"
    """
    if now is None:
        now = datetime.now(timezone.utc)
    target = ensure_aware(target)
    now = ensure_aware(now)

    delta = target - now
    seconds = int(delta.total_seconds())

    if seconds < 0:
        return relative_time_zh(target, now)
    if seconds < 60:
        return "马上"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟后"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时后"
    days = delta.days
    if days < 7:
        return f"{days} 天后"
    if days < 30:
        weeks = days // 7
        return f"{weeks} 周后"
    return "很久以后"


def relative_time_zh(then: datetime, now: datetime | None = None) -> str:
    """相对过去时间的中文描述（'3 天前' 等）。

    Args:
        then: 过去时间
        now: 当前时间（默认当前 UTC）

    Returns:
        形如 "3 天前" 或 "刚刚" 或 "2 小时前"
    """
    if now is None:
        now = datetime.now(timezone.utc)
    then = ensure_aware(then)
    now = ensure_aware(now)

    delta = now - then
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"
    days = delta.days
    if days < 7:
        return f"{days} 天前"
    if days < 30:
        weeks = days // 7
        return f"{weeks} 周前"
    if days < 365:
        months = days // 30
        return f"{months} 个月前"
    years = days // 365
    return f"{years} 年前"


__all__ = [
    "parse_sqlite_ts",
    "describe_gap",
    "format_now",
    "ensure_aware",
    "relative_time_zh",
    "relative_until_zh",
]