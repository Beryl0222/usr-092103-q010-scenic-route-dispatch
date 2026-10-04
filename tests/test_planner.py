"""规划约束：容量窗口、气象分层、能力、无障碍、资格、救援覆盖。"""

import unittest

from src.dispatch.planner import (
    ACCESSIBILITY_UNMET,
    CAPABILITY_INSUFFICIENT,
    QUALIFICATION_UNVERIFIED,
    WEATHER_TIER_EXCEEDED,
    Planner,
)
from tests.fixtures import at, build_world


class PlannerConstraintTest(unittest.TestCase):
    def test_scenic_feasible_and_window_after_now(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 1, at(8))
        res = svc.place_reservation("r_scenic", "p1", "slot_am", at(8, 5))
        self.assertTrue(res["accepted"])
        # 08:05 下单，最早入场格必须是 08:10（向上取整，不能给过去格子）
        self.assertEqual(res["proposal"]["window"]["earliest"], "2026-10-04T08:10:00+08:00")

    def test_capacity_full_pushes_to_later_slot(self) -> None:
        svc, _ = build_world(cable_cap=2)  # 索道每格仅 2
        svc.register_party("p1", ["v1", "v2"], at(8))
        svc.register_party("p2", ["v3", "v4"], at(8, 1))
        for v in ("v1", "v2", "v3", "v4"):
            svc.declare_capability(v, 1, at(8))
        r1 = svc.place_reservation("r_scenic", "p1", "slot_am", at(8, 5))
        r2 = svc.place_reservation("r_scenic", "p2", "slot_am", at(8, 6))
        self.assertTrue(r1["accepted"] and r2["accepted"])
        self.assertLess(r1["proposal"]["window"]["earliest"],
                        r2["proposal"]["window"]["earliest"])
        self.assertEqual(r2["proposal"]["window"]["earliest"], "2026-10-04T08:20:00+08:00")

    def test_weather_blocks_hiking_at_yellow_but_scenic_ok(self) -> None:
        svc, _ = build_world()
        svc.issue_weather("z_high", 2, at(8), title="黄色大风")  # 徒步限 1 级
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 4, at(8))
        hike = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        self.assertFalse(hike["accepted"])
        self.assertIn(WEATHER_TIER_EXCEEDED,
                      [b["code"] for b in hike["proposal"]["blocks"]])
        # 同一黄色预警下精品观光（限 2 级）仍可在中低山区活动
        svc.register_party("p2", ["v2"], at(8))
        svc.declare_capability("v2", 1, at(8))
        scenic = svc.place_reservation("r_scenic", "p2", "slot_am", at(8, 5))
        self.assertTrue(scenic["accepted"])

    def test_extreme_requires_risk_clearance(self) -> None:
        svc, _ = build_world()
        svc.issue_weather("z_top", 1, at(8), title="蓝色")  # 极限限 0 级
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 5, at(8), qualification_claims={"extreme": "c"})
        # 资格先核验通过，才能观察到下一道门——气象必须清零
        svc.verify_qualification("v1", "extreme", "verified", "r1",
                                 at(8, 1), expires_at=at(20))
        res = svc.place_reservation("r_ext", "p1", "slot_am", at(8, 5))
        self.assertFalse(res["accepted"])
        self.assertIn(WEATHER_TIER_EXCEEDED,
                      [b["code"] for b in res["proposal"]["blocks"]])

    def test_capability_gate(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 1, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        self.assertFalse(res["accepted"])
        self.assertIn(CAPABILITY_INSUFFICIENT,
                      [b["code"] for b in res["proposal"]["blocks"]])

    def test_accessibility_routed_to_accessible_version(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 1, at(8), accessibility=["wheelchair"])
        blocked = svc.place_reservation("r_scenic", "p1", "slot_am", at(8, 5))
        self.assertIn(ACCESSIBILITY_UNMET,
                      [b["code"] for b in blocked["proposal"]["blocks"]])
        ok = svc.place_reservation("r_scenic_acc", "p1", "slot_am", at(8, 5),
                                   need_accessible=True)
        self.assertTrue(ok["accepted"])
        self.assertEqual([s["edge_id"] for s in ok["proposal"]["schedule"]], ["acc1", "tr8"])

    def test_qualification_required_for_extreme(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 5, at(8))
        res = svc.place_reservation("r_ext", "p1", "slot_am", at(8, 5))
        self.assertFalse(res["accepted"])
        self.assertIn(QUALIFICATION_UNVERIFIED,
                      [b["code"] for b in res["proposal"]["blocks"]])

    def test_rescue_uncovered_blocks_route(self) -> None:
        svc, _ = build_world()
        # 关掉高山区唯一救援站
        svc.change_facility(at(8, 2), "closed", "高山救援站搬迁", facility_id="rs2")
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 4, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        self.assertFalse(res["accepted"])
        self.assertTrue(any("z_high" in b.get("subject", "") for b in res["proposal"]["blocks"]))

    def test_explainability_every_block_has_code_and_message(self) -> None:
        svc, _ = build_world()
        svc.issue_weather("z_high", 3, at(8))
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 1, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        for block in res["proposal"]["blocks"]:
            self.assertTrue(block["code"])
            self.assertTrue(block["message"])

    def test_maintenance_window_blocks_edge(self) -> None:
        from src.dispatch.network import Edge as NetEdge, RouteVersion
        from src.dispatch.timeutil import parse
        svc, _ = build_world()
        # 发布观光线路 v2：步道 tr0 在整个上午时段维护，无可入场窗口
        rv = svc.proj.route("r_scenic", 1)
        tr0 = NetEdge.from_payload(svc.proj.edges["tr0"].to_payload())
        tr0.maintenance = [(at(8), at(12))]
        new_version = RouteVersion(
            rv.route_id, rv.tier, rv.version + 1, rv.gate, rv.nodes, rv.edges,
            valid_from=parse("2026-10-04T08:02:00+08:00"),
            min_capability=rv.min_capability, party_max=rv.party_max,
            rendezvous=rv.rendezvous)
        svc.publish_route(new_version, [tr0], [], at(8, 2))
        svc.register_party("p1", ["v1"], at(8, 3))
        svc.declare_capability("v1", 1, at(8, 3))
        res = svc.place_reservation("r_scenic", "p1", "slot_am", at(8, 5))
        self.assertFalse(res["accepted"])
        self.assertIn("MAINTENANCE", res["proposal"]["all_block_codes"])

    def test_entry_slot_tier_mismatch(self) -> None:
        svc, _ = build_world()
        svc.open_entry_slot("slot_scenic_only", "east_gate",
                            at(8), at(11), 100, ["scic"], at(8))
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 4, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_scenic_only", at(8, 5))
        self.assertFalse(res["accepted"])
        self.assertIn("ENTRY_SLOT_TIER_MISMATCH",
                      res["proposal"]["all_block_codes"])

    def test_party_over_route_limit_rejected(self) -> None:
        svc, _ = build_world()
        members = [f"v{i}" for i in range(40)]
        svc.register_party("pbig", members, at(8))
        for m in members:
            svc.declare_capability(m, 1, at(8))
        res = svc.place_reservation("r_scenic", "pbig", "slot_am", at(8, 5))
        self.assertFalse(res["accepted"])
        self.assertIn("PARTY_TOO_LARGE", res["proposal"]["all_block_codes"])


if __name__ == "__main__":
    unittest.main()
