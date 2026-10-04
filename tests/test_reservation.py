"""预约生命周期：占位/确认/超时/释放一致性，与高风险人工放行门。"""

import unittest

from src.dispatch.events import EventError
from tests.fixtures import at, build_world


class ReservationLifecycleTest(unittest.TestCase):
    def _booked_hike(self, visitor="v1", capability=4):
        svc, _ = build_world()
        svc.register_party("p1", [visitor], at(8))
        svc.declare_capability(visitor, capability, at(8))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        return svc, res

    def test_hold_then_confirm_converts_capacity(self) -> None:
        svc, res = self._booked_hike()
        rid = res["reservation_id"]
        self.assertEqual(svc.proj.reservations[rid]["status"], "hold")
        self.assertGreater(svc.proj.capacity_at("facility", "cab1", at(8, 20)), 0)
        svc.confirm_reservation(rid, at(8, 6))
        self.assertEqual(svc.proj.reservations[rid]["status"], "confirmed")
        # 占位转确认后仍占用同一格子（held+confirmed 恒等，不重不漏）
        self.assertGreater(svc.proj.capacity_at("facility", "cab1", at(8, 20)), 0)

    def test_timeout_releases_hold_and_frees_capacity(self) -> None:
        svc, res = self._booked_hike()
        rid = res["reservation_id"]
        occupied = svc.proj.capacity_at("facility", "cab1", at(8, 20))
        svc.expire_holds(at(8, 21))  # TTL 15 分钟：08:05 + 15 = 08:20
        self.assertEqual(svc.proj.reservations[rid]["status"], "timed_out")
        self.assertEqual(svc.proj.capacity_at("facility", "cab1", at(8, 20)), 0)
        self.assertGreater(occupied, 0)

    def test_cannot_confirm_after_timeout(self) -> None:
        svc, res = self._booked_hike()
        rid = res["reservation_id"]
        svc.expire_holds(at(8, 21))
        with self.assertRaises(EventError):
            svc.confirm_reservation(rid, at(8, 22))

    def test_active_release_frees_capacity(self) -> None:
        svc, res = self._booked_hike()
        rid = res["reservation_id"]
        svc.release_reservation(rid, at(8, 7), reason="行程变更")
        self.assertEqual(svc.proj.reservations[rid]["status"], "released")
        self.assertEqual(svc.proj.capacity_at("facility", "cab1", at(8, 20)), 0)

    def test_double_release_rejected(self) -> None:
        svc, res = self._booked_hike()
        rid = res["reservation_id"]
        svc.release_reservation(rid, at(8, 7))
        with self.assertRaises(EventError):
            svc.release_reservation(rid, at(8, 8))

    def test_extreme_hold_requires_manual_approval_to_confirm(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 5, at(8), qualification_claims={"extreme": "cert#1"})
        svc.verify_qualification("v1", "extreme", "verified", "ranger01",
                                 at(8, 2), expires_at=at(20))
        res = svc.place_reservation("r_ext", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        self.assertTrue(res["accepted"])
        # 资格齐备但高风险，仍必须人工放行
        with self.assertRaises(EventError):
            svc.confirm_reservation(rid, at(8, 6))
        svc.decide_manual_approval(rid, "approved", "ranger01", at(8, 7),
                                   reason="装备与体能核验通过")
        svc.confirm_reservation(rid, at(8, 8))
        self.assertEqual(svc.proj.reservations[rid]["status"], "confirmed")

    def test_manual_rejection_releases_hold(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 5, at(8), qualification_claims={"extreme": "c"})
        svc.verify_qualification("v1", "extreme", "verified", "r1", at(8, 2), expires_at=at(20))
        res = svc.place_reservation("r_ext", "p1", "slot_am", at(8, 5))
        rid = res["reservation_id"]
        svc.decide_manual_approval(rid, "rejected", "ranger01", at(8, 7),
                                   reason="未携带安全绳")
        self.assertEqual(svc.proj.reservations[rid]["status"], "released")

    def test_expired_qualification_blocks_booking(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 5, at(8))
        svc.verify_qualification("v1", "extreme", "verified", "r1",
                                 at(7), expires_at=at(7, 30))  # 已过期
        res = svc.place_reservation("r_ext", "p1", "slot_am", at(8, 5))
        self.assertFalse(res["accepted"])
        self.assertTrue(any(b["code"] == "QUALIFICATION_EXPIRED"
                            for b in res["proposal"]["blocks"]))

    def test_personalization_refused_is_recorded_and_not_applied(self) -> None:
        svc, _ = build_world()
        svc.register_party("p1", ["v1"], at(8))
        svc.declare_capability("v1", 4, at(8))
        svc.update_consent("v1", False, at(8, 1))
        res = svc.place_reservation("r_hike", "p1", "slot_am", at(8, 5))
        self.assertFalse(res["proposal"]["personalization_applied"])


if __name__ == "__main__":
    unittest.main()
