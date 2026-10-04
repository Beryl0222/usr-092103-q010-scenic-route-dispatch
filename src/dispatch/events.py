"""领域事件工厂。

事件是本系统唯一的事实来源。所有状态变更都先落为事件，再由投影重建。
每个事件携带：
- occurred_at：业务发生时刻（气象观测时刻、人工点击时刻……）
- recorded_at 由 EventStore 在入库时盖上：事件真正进入日志的时刻。

迟到传感器数据 = occurred_at 早于已处理事件、但 recorded_at 更晚；
投影按 recorded_at 顺序生效，因此迟到数据只能影响之后，不能回溯制造违规。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.validator import validate_event

from .timeutil import format_value, parse


class EventError(ValueError):
    pass


def make_event(
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str | datetime,
    version: int,
    summary: str,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    event = {
        "event_id": event_id or f"{event_type.lower()}-{aggregate_id}-v{version}",
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": format_value(parse(occurred_at)),
        "version": version,
        "summary": summary,
        "payload": payload or {},
    }
    errors = validate_event(event)
    if errors:
        raise EventError("；".join(errors))
    return event
