"""测试共用：构建张家界分层线路的小型网络世界。"""

from __future__ import annotations

from src.dispatch.network import (
    EDGE_CABLEWAY,
    EDGE_EXTREME,
    EDGE_HIKING,
    EDGE_STAIRWAY,
    EDGE_TRAIL,
    FACILITY_CABLEWAY,
    FACILITY_RESCUE,
    FACILITY_STAIRWAY,
    TIER_EXTREME,
    TIER_HIKING,
    TIER_PHOTO,
    TIER_SCENIC,
    Edge,
    Facility,
    RouteVersion,
)
from src.dispatch.service import DispatchService
from src.dispatch.store import EventStore
from src.dispatch import timeutil as tu

DAY = "2026-10-04"


def build_world(cable_cap: int = 60, stair_cap: int = 80):
    """返回 (service, t0)。

    网络：
      east_gate --tr0--> A --cw1(索道)--> B --st1(天梯)--> summit
      A --tr8(步行,绕行)--> summit
      B --hk1(徒步)--> hut --ex1(极限)--> peak
      hut --evac1--> safe_plaza
      east_gate --acc1(无障碍)--> B
    """
    store = EventStore()
    svc = DispatchService(store)
    t0 = tu.at(DAY, 8, 0)
    edges = [
        Edge("tr0", EDGE_TRAIL, "east_gate", "A", 10, "z_low",
             capacity_per_slot=200, bidirectional=True, accessible=True),
        Edge("cw1", EDGE_CABLEWAY, "A", "B", 8, "z_mid",
             facility_id="cab1", capacity_per_slot=cable_cap, accessible=False),
        Edge("st1", EDGE_STAIRWAY, "B", "summit", 15, "z_mid",
             facility_id="stair1", capacity_per_slot=stair_cap, accessible=False),
        Edge("tr8", EDGE_TRAIL, "B", "summit", 25, "z_mid2",
             capacity_per_slot=100, bidirectional=True, difficulty=1, accessible=True),
        Edge("hk1", EDGE_HIKING, "B", "hut", 30, "z_high",
             capacity_per_slot=40, difficulty=4, bidirectional=True),
        Edge("ex1", EDGE_EXTREME, "hut", "peak", 40, "z_top",
             capacity_per_slot=20, difficulty=5,
             required_qualification="extreme", bidirectional=True),
        Edge("evac1", EDGE_TRAIL, "hut", "safe_plaza", 20, "z_safe",
             capacity_per_slot=200, bidirectional=True, accessible=True),
        Edge("acc1", EDGE_TRAIL, "east_gate", "B", 30, "z_low",
             capacity_per_slot=50, bidirectional=True, accessible=True),
        Edge("photo1", EDGE_TRAIL, "B", "photo_terrace", 12, "z_mid",
             capacity_per_slot=30, bidirectional=True),
    ]
    facilities = [
        Facility("cab1", FACILITY_CABLEWAY, "天子山索道", max(cable_cap, 70)),
        Facility("stair1", FACILITY_STAIRWAY, "百龙天梯", max(stair_cap, 90)),
        Facility("rs1", FACILITY_RESCUE, "低山救援站", 10,
                 zones_covered=("z_low", "z_mid", "z_mid2")),
        Facility("rs2", FACILITY_RESCUE, "高山救援站", 10,
                 zones_covered=("z_high", "z_top", "z_safe")),
    ]
    routes = [
        RouteVersion("r_scenic", TIER_SCENIC, 1, "east_gate",
                     ("east_gate", "A", "B", "summit"),
                     ("tr0", "cw1", "st1"), valid_from=t0, min_capability=1,
                     rendezvous=("summit",)),
        RouteVersion("r_scenic_acc", TIER_SCENIC, 1, "east_gate",
                     ("east_gate", "B", "summit"),
                     ("acc1", "tr8"), valid_from=t0, min_capability=1,
                     accessible_only=True),
        RouteVersion("r_photo", TIER_PHOTO, 1, "east_gate",
                     ("east_gate", "A", "B", "photo_terrace"),
                     ("tr0", "cw1", "photo1"), valid_from=t0, min_capability=2),
        RouteVersion("r_hike", TIER_HIKING, 1, "east_gate",
                     ("east_gate", "A", "B", "hut"),
                     ("tr0", "cw1", "hk1"), valid_from=t0, min_capability=4,
                     rendezvous=("hut",)),
        RouteVersion("r_ext", TIER_EXTREME, 1, "east_gate",
                     ("east_gate", "A", "B", "hut", "peak"),
                     ("tr0", "cw1", "hk1", "ex1"), valid_from=t0,
                     min_capability=5, requires_qualification="extreme"),
        RouteVersion("r_evac", TIER_SCENIC, 1, "hut",
                     ("hut", "safe_plaza"), ("evac1",), valid_from=t0,
                     min_capability=1),
    ]
    for route in routes:
        svc.publish_route(route, edges if route is routes[0] else [],
                          facilities if route is routes[0] else [], t0)
    svc.open_entry_slot("slot_am", "east_gate",
                        tu.at(DAY, 8, 0), tu.at(DAY, 11, 0), 150,
                        [TIER_SCENIC, TIER_PHOTO, TIER_HIKING, TIER_EXTREME], t0)
    svc.evacuation_routes["z_high"] = "r_evac"
    svc.evacuation_routes["z_top"] = "r_evac"
    return svc, t0


def at(hour: int, minute: int = 0):
    return tu.at(DAY, hour, minute)
