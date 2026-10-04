"""读模型投影：从仅追加事件日志重建当前状态与历史事实。

所有 *_at 查询都接受可选的 known_at（入库时刻上界），实现"决策时刻可知事实"语义：
只有 recorded_at <= known_at 的事件才参与判断。因此一条迟到的气象/传感器事件
不会改变它到达之前已经做出的决策，只可能触发对未完成路段的重新调度。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from .network import (
    Facility,
    RouteVersion,
    Edge,
    slot_start,
)
from .timeutil import parse

# 传感器/气象数据超过此时延视为"迟到数据"，复盘时单列归因。
LATE_DATA_THRESHOLD = timedelta(minutes=5)


def _latest_record(records: list[dict[str, Any]], t: datetime, known_at: datetime | None) -> dict[str, Any] | None:
    pick = None
    for rec in records:
        if rec["at"] > t:
            continue
        if known_at is not None and rec["recorded_at"] > known_at:
            continue
        if pick is None or (rec["at"], rec["seq"]) > (pick["at"], pick["seq"]):
            pick = rec
    return pick


class Projection:
    def __init__(self) -> None:
        self.edges: dict[str, Edge] = {}
        self.facilities: dict[str, Facility] = {}
        self.routes: dict[str, dict[int, RouteVersion]] = {}
        # 设施 / 边的状态变迁史
        self.facility_history: dict[str, list[dict[str, Any]]] = {}
        self.edge_history: dict[str, list[dict[str, Any]]] = {}
        # 气象风险区间（按分区）
        self.weather: dict[str, list[dict[str, Any]]] = {}
        # 疏散区间：key 为分区或 'ALL'
        self.evacuations: dict[str, list[dict[str, Any]]] = {}
        # 入口时段
        self.entry_slots: dict[str, dict[str, Any]] = {}
        # 游客
        self.visitors: dict[str, dict[str, Any]] = {}
        # 团队
        self.parties: dict[str, dict[str, Any]] = {}
        # 预约
        self.reservations: dict[str, dict[str, Any]] = {}
        # 容量台账：(subject_type, subject_id, slot_iso) -> {"held":n,"confirmed":n}
        self.ledger: dict[tuple[str, str, str], dict[str, int]] = {}
        # 同一格子按预约的明细：key -> {rid: {"held":n,"confirmed":n}}，重排时剔除自身旧占位
        self.ledger_detail: dict[tuple[str, str, str], dict[str, dict[str, int]]] = {}
        # 决策票据（审批、规划说明）
        self.approvals: dict[str, list[dict[str, Any]]] = {}   # reservation_id -> decisions
        self.notes: dict[str, list[dict[str, Any]]] = {}       # reservation_id -> notes
        self.global_notes: list[dict[str, Any]] = []
        # 事件索引（复盘证据用）
        self.events: list[dict[str, Any]] = []
        self._seq = 0

    # ------------------------------------------------------------------ 重建
    def build(self, events: Iterable[dict[str, Any]]) -> "Projection":
        for event in sorted(events, key=lambda e: (parse(e["recorded_at"]), e.get("_seq", 0))):
            self.apply(event)
        return self

    def apply(self, event: dict[str, Any]) -> None:
        seq = event.get("_seq", self._seq)
        self._seq = max(self._seq, seq) + 1
        self.events.append(event)
        p = event.get("payload", {})
        t = parse(event["occurred_at"])
        recorded = parse(event["recorded_at"])
        et = event["event_type"]
        handler = getattr(self, f"_on_{et.lower()}", None)
        if handler is not None:
            handler(p, t, recorded, seq, event)

    def _on_route_published(self, p: dict, t: datetime, recorded: datetime, seq: int, e: dict) -> None:
        route = RouteVersion.from_payload(p["route"])
        self.routes.setdefault(route.route_id, {})[route.version] = route
        # 同 id 的旧版本失效
        for ver, rv in self.routes[route.route_id].items():
            if ver != route.version and rv.valid_to is None:
                rv.valid_to = route.valid_from
        for edge_data in p.get("edges", []):
            self.edges[edge_data["edge_id"]] = Edge.from_payload(edge_data)
        for fac_data in p.get("facilities", []):
            self.facilities[fac_data["facility_id"]] = Facility.from_payload(fac_data)

    def _on_facility_status_changed(self, p: dict, t: datetime, recorded: datetime, seq: int, e: dict) -> None:
        rec = {"at": t, "recorded_at": recorded, "seq": seq, "status": p["status"],
               "reason": p.get("reason", ""), "event_id": e["event_id"]}
        if p.get("facility_id"):
            self.facility_history.setdefault(p["facility_id"], []).append(rec)
        if p.get("edge_id"):
            self.edge_history.setdefault(p["edge_id"], []).append(
                {**rec, "closed": p["status"] == "closed"})

    def _open_interval(self, store: dict, key: str, rec: dict) -> None:
        store.setdefault(key, []).append(rec)

    def _close_intervals(self, store: dict, key: str, t: datetime) -> None:
        for interval in store.get(key, []):
            if interval["end"] is None or interval["end"] > t:
                interval["end"] = t

    def _on_weather_risk_issued(self, p: dict, t: datetime, recorded: datetime, seq: int, e: dict) -> None:
        self._open_interval(self.weather, p["zone"], {
            "level": int(p["level"]), "start": t, "end": None,
            "recorded_at": recorded, "seq": seq,
            "source": p.get("source", "sensor"), "title": p.get("title", ""),
            "event_id": e["event_id"],
        })

    def _on_weather_risk_cleared(self, p: dict, t: datetime, *args: Any) -> None:
        self._close_intervals(self.weather, p["zone"], t)

    def _on_risk_cleared(self, p: dict, t: datetime, recorded: datetime, seq: int, e: dict) -> None:
        # 人工综合解除（气象或现场风险）
        if p.get("zone"):
            self._close_intervals(self.weather, p["zone"], t)

    def _on_entry_slot_opened(self, p: dict, t: datetime, *args: Any) -> None:
        self.entry_slots[p["slot_id"]] = {
            "slot_id": p["slot_id"], "gate": p["gate"],
            "start": parse(p["start"]), "end": parse(p["end"]),
            "quota": int(p["quota"]), "tiers": tuple(p.get("tiers", ())),
        }

    def _visitor_base(self, vid: str) -> dict[str, Any]:
        return self.visitors.setdefault(vid, {
            "visitor_id": vid, "capability": 1, "accessibility": [],
            "qual_claims": {}, "qualifications": {},
            "personalization_consent": True, "party_id": None,
        })

    def _on_visitor_capability_declared(self, p: dict, *args: Any) -> None:
        v = self._visitor_base(p["visitor_id"])
        v["capability"] = int(p.get("capability", v["capability"]))
        v["accessibility"] = list(p.get("accessibility", v["accessibility"]))
        v["qual_claims"] = dict(p.get("qualification_claims", {}))
        if p.get("party_id"):
            v["party_id"] = p["party_id"]

    def _on_visitor_consent_updated(self, p: dict, *args: Any) -> None:
        v = self._visitor_base(p["visitor_id"])
        v["personalization_consent"] = bool(p.get("personalization", True))

    def _on_qualification_verified(self, p: dict, t: datetime, *args: Any) -> None:
        v = self._visitor_base(p["visitor_id"])
        v["qualifications"][p["qualification"]] = {
            "result": p.get("result", "verified"),
            "verified_at": t,
            "expires_at": parse(p["expires_at"]) if p.get("expires_at") else None,
            "verifier": p.get("verifier", ""),
        }

    def _on_party_registered(self, p: dict, t: datetime, *args: Any) -> None:
        members = list(p["members"])
        self.parties[p["party_id"]] = {
            "party_id": p["party_id"], "name": p.get("name", p["party_id"]),
            "groups": [{"group_id": "main", "members": members}],
            "rendezvous": None, "splits": [], "created_at": t,
        }
        for vid in members:
            self._visitor_base(vid)["party_id"] = p["party_id"]

    def _on_party_split(self, p: dict, t: datetime, *args: Any) -> None:
        party = self.parties.get(p["party_id"])
        if party is None:
            return
        off = list(p["members"])
        for group in party["groups"]:
            if set(off) <= set(group["members"]):
                group["members"] = [m for m in group["members"] if m not in off]
                break
        new_gid = p.get("group_id") or f"g{len(party['groups']) + 1}"
        party["groups"].append({"group_id": new_gid, "members": off})
        party["rendezvous"] = {
            "node": p["rendezvous_node"],
            "time": parse(p["rendezvous_time"]) if p.get("rendezvous_time") else None,
        }
        party["splits"].append({"group_id": new_gid, "members": off,
                                "rendezvous_node": p["rendezvous_node"], "at": t,
                                "reason": p.get("reason", "")})

    # ------------------------------------------------------------- 预约生命周期
    def _on_reservation_placed(self, p: dict, t: datetime, recorded: datetime, seq: int, e: dict) -> None:
        groups = {}
        for g in p["groups"]:
            groups[g["group_id"]] = {
                "group_id": g["group_id"], "members": list(g["members"]),
                "schedule": [self._schedule_item(x) for x in g.get("schedule", [])],
                "used": [],
            }
        self.reservations[p["reservation_id"]] = {
            "reservation_id": p["reservation_id"],
            "party_id": p["party_id"],
            "route_id": p["route_id"],
            "route_version": int(p["route_version"]),
            "slot_id": p["slot_id"],
            "party_size": int(p["party_size"]),
            "need_accessible": bool(p.get("need_accessible", False)),
            "status": "hold",
            "placed_at": t, "recorded_at": recorded,
            "expires_at": parse(p["expires_at"]),
            "confirmed_at": None, "ended_at": None,
            "groups": groups,
            "rendezvous": None,
            "reroutes": [],
        }

    @staticmethod
    def _schedule_item(x: dict) -> dict:
        return {
            "edge_id": x["edge_id"], "from_node": x["from_node"], "to_node": x["to_node"],
            "enter_at": parse(x["enter_at"]), "exit_at": parse(x["exit_at"]),
        }

    def _set_status(self, rid: str, status: str, t: datetime) -> dict[str, Any] | None:
        res = self.reservations.get(rid)
        if res:
            res["status"] = status
            if status == "confirmed":
                res["confirmed_at"] = t
            if status in ("timed_out", "released"):
                res["ended_at"] = t
        return res

    def _on_reservation_confirmed(self, p: dict, t: datetime, *args: Any) -> None:
        self._set_status(p["reservation_id"], "confirmed", t)

    def _on_reservation_timed_out(self, p: dict, t: datetime, *args: Any) -> None:
        self._set_status(p["reservation_id"], "timed_out", t)

    def _on_reservation_released(self, p: dict, t: datetime, *args: Any) -> None:
        res = self._set_status(p["reservation_id"], "released", t)
        if res is not None:
            res["release_reason"] = p.get("reason", "")

    def _on_segment_used(self, p: dict, t: datetime, *args: Any) -> None:
        res = self.reservations.get(p["reservation_id"])
        if res is None:
            return
        gid = p.get("group_id", "main")
        group = res["groups"].setdefault(gid, {"group_id": gid, "members": [], "schedule": [], "used": []})
        group["used"].append({
            "edge_id": p["edge_id"], "from_node": p["from_node"], "to_node": p["to_node"],
            "used_at": t,
        })

    def _on_capacity_reserved(self, p: dict, t: datetime, *args: Any) -> None:
        key = (p["subject_type"], p["subject_id"], p["slot_start"])
        cell = self.ledger.setdefault(key, {"held": 0, "confirmed": 0})
        rid = p.get("reservation_id", "_")
        detail = self.ledger_detail.setdefault(key, {}).setdefault(rid, {"held": 0, "confirmed": 0})
        state = p.get("state", "held")
        qty = int(p["qty"])
        if state in ("held", "confirmed"):
            cell[state] += qty
            detail[state] += qty
        elif state == "released":
            bucket = "confirmed" if p.get("from_state") == "confirmed" else "held"
            cell[bucket] = max(0, cell[bucket] - qty)
            detail[bucket] = max(0, detail[bucket] - qty)
        elif state == "converted":  # 占位转确认：held -= qty, confirmed += qty
            cell["held"] = max(0, cell["held"] - qty)
            cell["confirmed"] += qty
            detail["held"] = max(0, detail["held"] - qty)
            detail["confirmed"] += qty

    def _on_manual_approval_decided(self, p: dict, t: datetime, recorded: datetime, seq: int, e: dict) -> None:
        self.approvals.setdefault(p["reservation_id"], []).append({
            "decision": p["decision"], "approver": p.get("approver", ""),
            "reason": p.get("reason", ""), "at": t, "recorded_at": recorded,
            "event_id": e["event_id"],
        })

    def _on_route_rerouted(self, p: dict, t: datetime, recorded: datetime, seq: int, e: dict) -> None:
        res = self.reservations.get(p["reservation_id"])
        if res is None:
            return
        for gid, g in p.get("groups", {}).items():
            existed = gid in res["groups"]
            group = res["groups"].setdefault(gid, {"group_id": gid, "members": [], "schedule": [], "used": []})
            # 已使用票段原样保留，仅替换尚未经过的节点
            group["schedule"] = [self._schedule_item(x) for x in g.get("schedule", [])]
            if "members" in g:
                group["members"] = list(g["members"])
            src_gid = g.get("copy_used_from")
            if not existed and src_gid and src_gid in res["groups"]:
                # 团队拆分产生的新组：继承指定父组共同走过的已核销票段
                group["used"] = [dict(u) for u in res["groups"][src_gid]["used"]]
        if p.get("rendezvous"):
            rv = p["rendezvous"]
            res["rendezvous"] = {"node": rv["node"],
                                 "time": parse(rv["time"]) if rv.get("time") else None}
        res["reroutes"].append({
            "at": t, "recorded_at": recorded, "seq": seq,
            "reason_codes": list(p.get("reason_codes", [])),
            "reasons": list(p.get("reasons", [])),
            "event_id": e["event_id"],
            "evacuation": bool(p.get("evacuation", False)),
        })

    def _on_evacuation_ordered(self, p: dict, t: datetime, recorded: datetime, seq: int, e: dict) -> None:
        key = p.get("zone", "ALL")
        self._open_interval(self.evacuations, key, {
            "start": t, "end": None, "recorded_at": recorded, "seq": seq,
            "reason": p.get("reason", ""), "must_clear_by": parse(p["must_clear_by"]) if p.get("must_clear_by") else None,
            "assembly_nodes": list(p.get("assembly_nodes", [])),
            "event_id": e["event_id"],
        })

    def _on_evacuation_stood_down(self, p: dict, t: datetime, *args: Any) -> None:
        self._close_intervals(self.evacuations, p.get("zone", "ALL"), t)

    def _on_dispatch_note_issued(self, p: dict, t: datetime, recorded: datetime, seq: int, e: dict) -> None:
        note = {"at": t, "recorded_at": recorded, "seq": seq,
                "audience": p.get("audience", "visitor"),
                "note_type": p.get("note_type", ""), "text": p.get("text", ""),
                "reason_codes": list(p.get("reason_codes", [])),
                "event_id": e["event_id"]}
        if p.get("reservation_id"):
            self.notes.setdefault(p["reservation_id"], []).append(note)
        else:
            self.global_notes.append(note)

    def _on_incident_reviewed(self, p: dict, t: datetime, *args: Any) -> None:
        # 复盘事件本身不再投影出业务状态，仅留存于事件日志
        pass

    # ------------------------------------------------------------------ 查询
    def route(self, route_id: str, version: int | None = None) -> RouteVersion | None:
        versions = self.routes.get(route_id)
        if not versions:
            return None
        if version is not None:
            return versions.get(version)
        return versions[max(versions)]

    def group_members(self, party_id: str, group_id: str) -> list[str]:
        party = self.parties.get(party_id)
        if not party:
            return []
        for g in party["groups"]:
            if g["group_id"] == group_id:
                return list(g["members"])
        return []

    def edge_closed_at(self, edge_id: str, t: datetime, known_at: datetime | None = None) -> str | None:
        rec = _latest_record(self.edge_history.get(edge_id, []), t, known_at)
        if rec and rec["closed"]:
            return rec["reason"] or "封闭"
        return None

    def facility_status_at(self, facility_id: str, t: datetime, known_at: datetime | None = None) -> str:
        rec = _latest_record(self.facility_history.get(facility_id, []), t, known_at)
        return rec["status"] if rec else "open"

    def weather_level_at(self, zone: str, t: datetime, known_at: datetime | None = None) -> int:
        best = None
        for iv in self.weather.get(zone, []):
            if iv["start"] > t or (iv["end"] is not None and t >= iv["end"]):
                continue
            if known_at is not None and iv["recorded_at"] > known_at:
                continue
            if best is None or (iv["start"], iv["seq"]) > (best["start"], best["seq"]):
                best = iv
        return best["level"] if best else 0

    def evacuation_at(self, zone: str, t: datetime, known_at: datetime | None = None) -> dict[str, Any] | None:
        for key in ("ALL", zone):
            for iv in self.evacuations.get(key, []):
                if iv["start"] <= t and iv["end"] is None:
                    if known_at is None or iv["recorded_at"] <= known_at:
                        return iv
        return None

    def rescue_covered(self, zone: str, t: datetime, known_at: datetime | None = None) -> bool:
        for fac in self.facilities.values():
            if fac.kind != "rescue_station" or zone not in fac.zones_covered:
                continue
            if self.facility_status_at(fac.facility_id, t, known_at) == "open":
                return True
        return False

    def capacity_at(self, subject_type: str, subject_id: str, moment: datetime) -> int:
        """某设施/边/入口在 10 分钟格子上的占用（held 与 confirmed 都计入容量）。"""
        key = (subject_type, subject_id, slot_start(moment).isoformat())
        cell = self.ledger.get(key)
        if not cell:
            return 0
        return cell["held"] + cell["confirmed"]
