"""静态网络模型与分层常量。

线路以"版本"发布（ROUTE_PUBLISHED），同一时刻可有多版本并存，预约锁定其确认时的
版本；边（索道/天梯/步道段/徒步段/极限段）与设施按 id 复用，新版本可覆盖元数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .timeutil import format_value, parse

# 容量核算的时间格子：索道、天梯、步道均按每 10 分钟一段统计在园与运载占用。
SLOT_MINUTES = 10

# 占位未确认的保留时长（分钟），超时由调度服务判定 TIMED_OUT 并释放容量。
HOLD_TTL_MINUTES = 15

# 分层线路
TIER_SCENIC = "scic"      # 精品观光
TIER_PHOTO = "photo"      # 摄影机位
TIER_HIKING = "hiking"    # 长线徒步
TIER_EXTREME = "extreme"  # 极限运动
TIERS = (TIER_SCENIC, TIER_PHOTO, TIER_HIKING, TIER_EXTREME)
TIER_LABELS = {
    TIER_SCENIC: "精品观光",
    TIER_PHOTO: "摄影机位",
    TIER_HIKING: "长线徒步",
    TIER_EXTREME: "极限运动",
}

# 各层允许的最高气象风险等级（0 无风险 / 1 蓝色 / 2 黄色 / 3 橙色 / 4 红色）。
# 极限层要求风险清零；橙色及以上触发紧急疏散判定。
WEATHER_LEVELS = (0, 1, 2, 3, 4)
WEATHER_MAX_LEVEL = {
    TIER_SCENIC: 2,
    TIER_PHOTO: 2,
    TIER_HIKING: 1,
    TIER_EXTREME: 0,
}
EVACUATION_WEATHER_LEVEL = 3

# 边类型
EDGE_CABLEWAY = "cableway"  # 索道
EDGE_STAIRWAY = "stairway"  # 天梯
EDGE_TRAIL = "trail"        # 普通步道
EDGE_HIKING = "hiking"      # 长线徒步段
EDGE_EXTREME = "extreme"    # 极限运动段
EDGE_KINDS = (EDGE_CABLEWAY, EDGE_STAIRWAY, EDGE_TRAIL, EDGE_HIKING, EDGE_EXTREME)
EDGE_KIND_LABELS = {
    EDGE_CABLEWAY: "索道",
    EDGE_STAIRWAY: "天梯",
    EDGE_TRAIL: "步道",
    EDGE_HIKING: "徒步道",
    EDGE_EXTREME: "极限段",
}

FACILITY_CABLEWAY = "cableway"
FACILITY_STAIRWAY = "stairway"
FACILITY_RESCUE = "rescue_station"  # 救援站点：决定分区救援覆盖


@dataclass
class Edge:
    edge_id: str
    kind: str
    from_node: str
    to_node: str
    duration_min: int
    zone: str
    facility_id: str | None = None   # 索道/天梯段归属的运载设施
    capacity_per_slot: int | None = None  # 每 10 分钟承载量；None 表示不做容量限制
    difficulty: int = 1              # 1-5，与游客能力等级对应
    required_qualification: str | None = None  # 如 "extreme"
    accessible: bool = True          # 是否满足无障碍通行
    bidirectional: bool = False      # 普通步道通常双向，索道/天梯单向
    maintenance: list[tuple[datetime, datetime]] = field(default_factory=list)

    def is_open_at(self, moment: datetime) -> bool:
        return not any(start <= moment < end for start, end in self.maintenance)

    def to_payload(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "kind": self.kind,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "duration_min": self.duration_min,
            "zone": self.zone,
            "facility_id": self.facility_id,
            "capacity_per_slot": self.capacity_per_slot,
            "difficulty": self.difficulty,
            "required_qualification": self.required_qualification,
            "accessible": self.accessible,
            "bidirectional": self.bidirectional,
            "maintenance": [[format_value(s), format_value(e)] for s, e in self.maintenance],
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "Edge":
        return cls(
            edge_id=data["edge_id"],
            kind=data["kind"],
            from_node=data["from_node"],
            to_node=data["to_node"],
            duration_min=int(data["duration_min"]),
            zone=data["zone"],
            facility_id=data.get("facility_id"),
            capacity_per_slot=data.get("capacity_per_slot"),
            difficulty=int(data.get("difficulty", 1)),
            required_qualification=data.get("required_qualification"),
            accessible=bool(data.get("accessible", True)),
            bidirectional=bool(data.get("bidirectional", False)),
            maintenance=[(parse(s), parse(e)) for s, e in data.get("maintenance", [])],
        )


@dataclass
class Facility:
    facility_id: str
    kind: str
    name: str
    capacity_per_slot: int
    zones_covered: tuple[str, ...] = ()  # 救援站点覆盖的分区

    def to_payload(self) -> dict[str, Any]:
        return {
            "facility_id": self.facility_id,
            "kind": self.kind,
            "name": self.name,
            "capacity_per_slot": self.capacity_per_slot,
            "zones_covered": list(self.zones_covered),
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "Facility":
        return cls(
            facility_id=data["facility_id"],
            kind=data["kind"],
            name=data.get("name", data["facility_id"]),
            capacity_per_slot=int(data["capacity_per_slot"]),
            zones_covered=tuple(data.get("zones_covered", [])),
        )


@dataclass
class RouteVersion:
    route_id: str
    tier: str
    version: int
    gate: str
    nodes: tuple[str, ...]            # 节点顺序
    edges: tuple[str, ...]            # 相邻节点间的边，长度 = len(nodes)-1
    valid_from: datetime
    valid_to: datetime | None = None  # None 表示当前有效
    min_capability: int = 1
    party_max: int = 30
    rendezvous: tuple[str, ...] = ()  # 团队拆分后的会合点
    requires_qualification: str | None = None  # 整条线统一资格（极限层）
    accessible_only: bool = False     # 是否为无障碍专线

    @property
    def high_risk(self) -> bool:
        return self.tier == TIER_EXTREME or self.requires_qualification is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "tier": self.tier,
            "version": self.version,
            "gate": self.gate,
            "nodes": list(self.nodes),
            "edges": list(self.edges),
            "valid_from": format_value(self.valid_from),
            "valid_to": format_value(self.valid_to) if self.valid_to else None,
            "min_capability": self.min_capability,
            "party_max": self.party_max,
            "rendezvous": list(self.rendezvous),
            "requires_qualification": self.requires_qualification,
            "accessible_only": self.accessible_only,
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "RouteVersion":
        return cls(
            route_id=data["route_id"],
            tier=data["tier"],
            version=int(data["version"]),
            gate=data["gate"],
            nodes=tuple(data["nodes"]),
            edges=tuple(data["edges"]),
            valid_from=parse(data["valid_from"]),
            valid_to=parse(data["valid_to"]) if data.get("valid_to") else None,
            min_capability=int(data.get("min_capability", 1)),
            party_max=int(data.get("party_max", 30)),
            rendezvous=tuple(data.get("rendezvous", [])),
            requires_qualification=data.get("requires_qualification"),
            accessible_only=bool(data.get("accessible_only", False)),
        )


def slot_start(moment: datetime) -> datetime:
    """把时刻向下取整到 10 分钟格子起点。"""
    minute = (moment.minute // SLOT_MINUTES) * SLOT_MINUTES
    return moment.replace(minute=minute, second=0, microsecond=0)
