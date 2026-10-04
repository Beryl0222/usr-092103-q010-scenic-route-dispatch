"""时间工具：统一带时区的 ISO8601 字符串与 datetime 互换。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def format_value(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.isoformat()


def at(date: str, hour: int, minute: int = 0) -> datetime:
    """便捷构造：'2026-10-04', 9, 30 -> 当日 CST 09:30。"""
    y, m, d = (int(x) for x in date.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=CST)
