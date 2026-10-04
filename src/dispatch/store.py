"""仅追加事件日志。

两条时间线：
- occurred_at：事件在业务世界发生的时间（写入事件时自带）
- recorded_at：事件进入本系统日志的时间（append 时加盖，测试可显式传入）

重放严格按入库顺序（recorded_at, seq）。因此一条"观测时刻很早、到达很晚"
的传感器事件只能改变它被记录之后的判断，系统不会回头重判过去的决策，
也就不会用迟到数据反向制造违规。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .events import EventError
from .timeutil import format_value, parse


class EventStore:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self._seq = 0
        self._agg_versions: dict[tuple[str, str], int] = {}

    def append(self, event: dict[str, Any], recorded_at: str | datetime | None = None) -> dict[str, Any]:
        eid = event["event_id"]
        if eid in self._ids:
            raise EventError(f"事件幂等冲突：{eid}")
        key = (event["aggregate_type"], event["aggregate_id"])
        expected = self._agg_versions.get(key, 0) + 1
        if event["version"] != expected:
            raise EventError(
                f"聚合 {key} 版本冲突：收到 v{event['version']}，期望 v{expected}"
            )
        stored = dict(event)
        stored["recorded_at"] = format_value(parse(recorded_at) if recorded_at else datetime.now())
        stored["_seq"] = self._seq
        self._seq += 1
        self._events.append(stored)
        self._ids.add(eid)
        self._agg_versions[key] = expected
        return stored

    def append_many(self, events: Iterable[dict[str, Any]], recorded_at: str | datetime | None = None) -> None:
        for event in events:
            self.append(event, recorded_at=recorded_at)

    def replay(self) -> list[dict[str, Any]]:
        return sorted(self._events, key=lambda e: (parse(e["recorded_at"]), e["_seq"]))

    @property
    def all(self) -> list[dict[str, Any]]:
        return self.replay()

    def recorded_after(self, moment: str | datetime) -> list[dict[str, Any]]:
        """返回入库时间晚于某时刻的事件（复盘时用于识别迟到数据）。"""
        m = parse(moment)
        return [e for e in self.replay() if parse(e["recorded_at"]) > m]
