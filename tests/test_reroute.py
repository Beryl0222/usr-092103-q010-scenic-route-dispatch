"""重排与疏散：停运/封闭/气象/疏散只动未走节点，已用票段保留；
团队拆分会合明确；迟到数据不回溯制造违规。"""

import unittest

from src.dispatch.planner import FACILITY_CLOSED
from src.dispatch.projection import Projection
from src.dispatch.views import visitor_view
from tests.fixtures import at, build_world


class RerouteTest(unittest.TestCase):
    def _enroute_scenic(self):
        """已确认并走过 tr0、cw1，人在 B。"""
        svc, _ = build_world()
        svc.register_party("p1", ["v1", "v2"], at(8))
        svc.declare_capability("v1", 2, at(8))
        svc.declare_capability("v2", 2, at(8))
        res = svc.place_reservation("r_scenic", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.confirm_reservation(rid, at(8, 6))
        svc.mark_segment_used(rid, "tr0", at(8, 20))
        svc.mark_segment_used(rid, "cw1", at(8, 40))
        return svc, rid

    def test_stairway_closure_reroutes_to_trail_and_keeps_used(self) -> None:
        svc, rid = self._enroute_scenic()
        svc.change_facility(at(9), "closed", "百龙天梯临时检修", facility_id="stair1")
        view = visitor_view(svc.proj, rid)
        self.assertTrue(view["changed"])
        self.assertEqual([s["edge_id"] for s in view["groups"][0]["used_segments"]],
                         ["tr0", "cw1"])
        upcoming = [s["edge_id"] for s in view["groups"][0]["upcoming"]]
        self.assertEqual(upcoming, ["tr8"])  # B -> summit 改走步行道
        self.assertIn(FACILITY_CLOSED, view["change_reason_codes"])
        self.assertTrue(view["latest_notice"])

    def test_no_safe_continuation_escalates_to_staff(self) -> None:
        svc, rid = self._enroute_scenic()
        # 先封天梯：自动改走 tr8
        svc.change_facility(at(9), "closed", "天梯检修", facility_id="stair1")
        self.assertTrue(visitor_view(svc.proj, rid)["changed"])
        reroutes_before = len(svc.proj.reservations[rid]["reroutes"])
        # 再封唯一绕行步行道 tr8：无安全续程，转现场人工，不再产生新改线事件
        svc.change_facility(at(9, 1), "closed", "步道落石", edge_id="tr8")
        staff = [n for n in svc.proj.notes.get(rid, []) if n["note_type"] == "reroute_failed"]
        self.assertTrue(staff)
        self.assertIn("NO_SAFE_CONTINUATION", staff[-1]["reason_codes"])
        self.assertEqual(len(svc.proj.reservations[rid]["reroutes"]), reroutes_before)

    def test_weather_upgrade_on_scenic_reroutes_to_parallel_zone(self) -> None:
        # 观光线：中区（索道/天梯）橙色预警超过观光阈值 2，
        # 平行分区 z_mid2 的步行道 tr8 仍安全，自动绕行
        svc, rid = self._enroute_scenic()
        svc.issue_weather("z_mid", 3, at(8, 45), title="强对流橙色")
        view = visitor_view(svc.proj, rid)
        self.assertTrue(view["changed"])
        self.assertEqual([s["edge_id"] for s in view["groups"][0]["upcoming"]], ["tr8"])
        self.assertIn("WEATHER_TIER_EXCEEDED", view["change_reason_codes"])

    def test_weather_upgrade_on_hike_with_no_detour_escalates_safely(self) -> None:
        # 高山徒步线进入高山区后遇黄色预警（徒步限 1 级），高山区无安全替代，
        # 系统不得把人排进风险区，而应保持原确认状态并转现场人工处置
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 4, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.confirm_reservation(rid, at(8, 6))
        svc.mark_segment_used(rid, "tr0", at(8, 20))
        svc.mark_segment_used(rid, "cw1", at(8, 40))  # 人在 B，下一步进高山区
        svc.issue_weather("z_high", 2, at(8, 42), title="大风黄色")
        staff = [n for n in svc.proj.notes.get(rid, []) if n["note_type"] == "reroute_failed"]
        self.assertTrue(staff)
        self.assertIn("NO_SAFE_CONTINUATION", staff[-1]["reason_codes"])
        # 没有产生把游客送入风险区的改线，预约仍确认（由现场人员引导就近避险）
        self.assertEqual(svc.proj.reservations[rid]["status"], "confirmed")

    def test_evacuation_takes_precedence_and_routes_to_assembly(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 4, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.confirm_reservation(rid, at(8, 6))
        svc.mark_segment_used(rid, "tr0", at(8, 20))
        svc.mark_segment_used(rid, "cw1", at(8, 30))  # 人在 B
        svc.order_evacuation("z_high", "山火", at(8, 35),
                             must_clear_by=at(9, 5),
                             assembly_nodes=["safe_plaza"],
                             safe_route_id="r_evac")
        view = visitor_view(svc.proj, rid)
        self.assertTrue(view["evacuation"])
        # B -> hk1 -> hut -> evac1 -> safe_plaza：穿越疏散分区撤离，已用段保留
        self.assertEqual([s["edge_id"] for s in view["groups"][0]["upcoming"]],
                         ["hk1", "evac1"])
        self.assertEqual([s["edge_id"] for s in view["groups"][0]["used_segments"]],
                         ["tr0", "cw1"])
        self.assertIn("优先撤离", view["latest_notice"])

    def test_party_split_records_groups_and_rendezvous(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1", "v2", "v3"], at(8))
        for v in ("v1", "v2", "v3"):
            svc.declare_capability(v, 4, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.confirm_reservation(rid, at(8, 6))
        svc.mark_segment_used(rid, "tr0", at(8, 20))
        out = svc.split_party("p1", ["v3"], "hut", at(8, 25),
                              group_id="photo_group",
                              rendezvous_time=at(11), reason="拍摄需要")
        self.assertTrue(out["reroutes"][0]["rerouted"])
        view = visitor_view(svc.proj, rid)
        groups = {g["group_id"]: g for g in view["groups"]}
        self.assertEqual(set(groups), {"main", "photo_group"})
        self.assertEqual(groups["main"]["members"], ["v1", "v2"])
        self.assertEqual(groups["photo_group"]["members"], ["v3"])
        self.assertEqual(view["rendezvous"]["node"], "hut")

    def test_late_sensor_data_does_not_retroactively_violate(self) -> None:
        from src.dispatch.timeutil import parse
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 4, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.confirm_reservation(rid, at(8, 6))
        confirm_seq = len(svc.store.all)
        # 08:00 观测的橙色风险，09:30 才入库（迟到 90 分钟）
        svc.issue_weather("z_high", 3, at(8), title="迟到数据",
                          recorded_at=at(9, 30))
        # 决策时刻（08:06）可知的事实里没有该预警
        self.assertEqual(
            svc.proj.weather_level_at("z_high", at(8, 40), known_at=at(8, 6)), 0
        )
        # 用 08:06 前入库的事件重建，确认动作与预约状态不被回溯推翻
        decision_view = Projection().build([
            e for e in svc.store.all if parse(e["recorded_at"]) <= at(8, 6)
        ])
        self.assertEqual(decision_view.reservations[rid]["status"], "confirmed")
        # 迟到数据到达之后产生的事件不能早于它入库：日志中确认事件之后、
        # 迟到预警之前，没有任何改线/违规事件
        mid_events = [
            e for e in svc.store.all[confirm_seq:]
            if parse(e["recorded_at"]) < at(9, 30)
        ]
        self.assertEqual(mid_events, [])
        # 到达后只影响未来：当前无气象安全的替代路径，转现场人工而非回溯判违规
        staff = [n for n in svc.proj.notes.get(rid, []) if n["audience"] == "staff"]
        self.assertTrue(staff)
        self.assertEqual(svc.proj.reservations[rid]["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
