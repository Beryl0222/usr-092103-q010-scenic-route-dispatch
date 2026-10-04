"""三视图与复盘归因。"""

import unittest

from src.dispatch.views import (
    CAUSE_FACILITY,
    CAUSE_LATE_DATA,
    CAUSE_WEATHER,
    incident_review,
    staff_view,
    visitor_view,
)
from tests.fixtures import at, build_world


class ViewsTest(unittest.TestCase):
    def test_visitor_view_shows_reasons_and_kept_tickets(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 2, at(8))
        res = svc.place_reservation("r_scenic", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.confirm_reservation(rid, at(8, 6))
        svc.mark_segment_used(rid, "tr0", at(8, 20))
        svc.mark_segment_used(rid, "cw1", at(8, 40))
        svc.change_facility(at(9), "closed", "天梯检修", facility_id="stair1")
        view = visitor_view(svc.proj, rid)
        self.assertTrue(view["changed"])
        self.assertTrue(view["latest_notice"].startswith("【改线通知】"))
        self.assertIn("已使用的票段继续有效", view["latest_notice"])

    def test_staff_view_lists_closures_evacuation_and_pending_gate(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 5, at(8), qualification_claims={"extreme": "c"})
        svc.verify_qualification("v1", "extreme", "verified", "r1", at(8, 2), expires_at=at(20))
        svc.place_reservation("r_ext", "p1", "slot_am", at(8, 5))
        svc.change_facility(at(8, 7), "closed", "索道检修", facility_id="cab1")
        svc.order_evacuation("z_mid", "设备故障", at(8, 8),
                             assembly_nodes=["safe_plaza"])
        view = staff_view(svc.proj, at(8, 9))
        zones = {z["zone"]: z for z in view["zones"]}
        self.assertTrue(zones["z_mid"]["evacuation"])
        self.assertTrue(any(f["facility_id"] == "cab1"
                            for f in zones["z_mid"]["closed_facilities"]))
        self.assertEqual(len(view["pending_manual_approval"]), 1)

    def test_staff_view_shows_split_party_rendezvous(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1", "v2"], at(8))
        for v in ("v1", "v2"):
            svc.declare_capability(v, 4, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.confirm_reservation(rid, at(8, 6))
        svc.split_party("p1", ["v2"], "hut", at(8, 20), group_id="g2",
                        rendezvous_time=at(11))
        view = staff_view(svc.proj, at(8, 21))
        self.assertEqual(len(view["split_parties"]), 1)
        sp = view["split_parties"][0]
        self.assertEqual(sp["rendezvous"]["node"], "hut")
        self.assertEqual({g["group_id"] for g in sp["groups"]}, {"main", "g2"})

    def test_incident_review_attributes_facility_closure(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1", "v2"], at(8))
        for v in ("v1", "v2"):
            svc.declare_capability(v, 2, at(8))
        res = svc.place_reservation("r_scenic", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.confirm_reservation(rid, at(8, 6))
        svc.mark_segment_used(rid, "tr0", at(8, 20))
        svc.mark_segment_used(rid, "cw1", at(8, 40))
        svc.change_facility(at(8, 50), "closed", "天梯机械故障", facility_id="stair1")
        report = incident_review(svc.store, svc.proj, "inc-1", "congestion",
                                 "z_mid", at(8, 55), edge_id="st1",
                                 summary="天梯口拥堵复盘")["report"]
        self.assertEqual(report["primary_cause"], CAUSE_FACILITY)
        self.assertIn(CAUSE_FACILITY, report["attribution"])
        # 每条归因都带事件证据
        for finding in report["attribution"][CAUSE_FACILITY]["evidence"]:
            self.assertTrue(finding["event_id"])
            self.assertTrue(finding["detail"])

    def test_incident_review_attributes_weather_rescue_and_flags_late_data(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 4, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.confirm_reservation(rid, at(8, 6))
        # 观测 08:00、入库 09:10 的橙色预警，救援发生在 08:40
        svc.issue_weather("z_high", 3, at(8), title="强对流橙色",
                          recorded_at=at(9, 10))
        report = incident_review(svc.store, svc.proj, "inc-2", "rescue",
                                 "z_high", at(8, 40),
                                 summary="高山区救援复盘")["report"]
        self.assertEqual(report["primary_cause"], CAUSE_WEATHER)
        self.assertIn(CAUSE_LATE_DATA, report["attribution"])
        # 双时间日志保证迟到数据不可能回溯制造违规
        self.assertFalse(report["late_data_rule"]["retroactive_violation_possible"])
        self.assertGreaterEqual(
            report["late_data_rule"]["late_records_arrived_after_decision"], 1
        )


if __name__ == "__main__":
    unittest.main()
