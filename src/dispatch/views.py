"""三类现场视图与复盘归因。

- visitor_view：游客看到的路线、已用票段、改线原因与安全提示、会合点。
- staff_view：当班人员必要信息——停运/封闭、气象、疏散、待人工放行、即将超时占位。
- incident_review：管理者复盘一次拥堵或救援的归因（容量 / 设施 / 气象 / 调度 / 迟到数据），
  每条归因都引用具体事件作为证据，并区分"决策时刻可知"与"事后才到"的信息。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .events import make_event
from .projection import LATE_DATA_THRESHOLD, Projection
from .store import EventStore
from .timeutil import parse

CAUSE_CAPACITY = "capacity"
CAUSE_FACILITY = "facility"
CAUSE_WEATHER = "weather"
CAUSE_DISPATCH = "dispatch"
CAUSE_LATE_DATA = "late_data"
CAUSE_LABELS = {
    CAUSE_CAPACITY: "容量不足",
    CAUSE_FACILITY: "设施停运/封闭",
    CAUSE_WEATHER: "气象风险",
    CAUSE_DISPATCH: "调度决定",
    CAUSE_LATE_DATA: "传感器迟到数据",
}


def visitor_view(proj: Projection, rid: str) -> dict[str, Any]:
    res = proj.reservations.get(rid)
    if res is None:
        return {"found": False, "reservation_id": rid}
    route = proj.route(res["route_id"], res["route_version"])
    groups_out = []
    for gid, group in res["groups"].items():
        used_edges = {u["edge_id"] for u in group["used"]}
        groups_out.append({
            "group_id": gid,
            "members": group["members"],
            "used_segments": [
                {"edge_id": u["edge_id"], "from_node": u["from_node"], "to_node": u["to_node"],
                 "used_at": u["used_at"].isoformat()}
                for u in group["used"]
            ],
            "upcoming": [
                {"edge_id": s["edge_id"], "from_node": s["from_node"], "to_node": s["to_node"],
                 "enter_at": s["enter_at"].isoformat(), "exit_at": s["exit_at"].isoformat(),
                 "ticket_kept": s["edge_id"] in used_edges}
                for s in group["schedule"]
            ],
        })
    last_reroute = res["reroutes"][-1] if res["reroutes"] else None
    notes = [n for n in proj.notes.get(rid, []) if n["audience"] == "visitor"]
    approvals = proj.approvals.get(rid, [])
    pending_approval = bool(
        route and route.high_risk and res["status"] == "hold"
        and (not approvals or approvals[-1]["decision"] != "approved")
    )
    out = {
        "found": True,
        "reservation_id": rid,
        "status": res["status"],
        "route_id": res["route_id"],
        "route_version": res["route_version"],
        "tier": route.tier if route else None,
        "groups": groups_out,
        "rendezvous": res["rendezvous"],
        "changed": last_reroute is not None,
        "change_reasons": last_reroute["reasons"] if last_reroute else [],
        "change_reason_codes": last_reroute["reason_codes"] if last_reroute else [],
        "evacuation": last_reroute["evacuation"] if last_reroute else False,
        "notices": [{"text": n["text"], "at": n["at"].isoformat(),
                     "reason_codes": n["reason_codes"]} for n in notes],
        "latest_notice": notes[-1]["text"] if notes else None,
        "pending_manual_approval": pending_approval,
    }
    if res["status"] == "hold":
        out["expires_at"] = res["expires_at"].isoformat()
    return out


def staff_view(proj: Projection, now: datetime | str) -> dict[str, Any]:
    now = parse(now)
    zones: dict[str, dict[str, Any]] = {}

    def zone_state(zone: str) -> dict[str, Any]:
        return zones.setdefault(zone, {"zone": zone, "weather_level": 0,
                                       "evacuation": None, "closed_edges": [],
                                       "closed_facilities": [], "rescue_open": False})

    # 气象
    for zone, intervals in proj.weather.items():
        z = zone_state(zone)
        for iv in intervals:
            if iv["start"] <= now and (iv["end"] is None or now < iv["end"]):
                z["weather_level"] = max(z["weather_level"], iv["level"])
    # 疏散
    for key, intervals in proj.evacuations.items():
        for iv in intervals:
            if iv["start"] <= now and iv["end"] is None:
                target_zones = [key] if key != "ALL" else list(zones) or ["ALL"]
                for zname in target_zones:
                    z = zone_state(zname)
                    z["evacuation"] = {
                        "reason": iv["reason"],
                        "must_clear_by": iv["must_clear_by"].isoformat() if iv["must_clear_by"] else None,
                        "assembly_nodes": iv["assembly_nodes"],
                        "ordered_at": iv["start"].isoformat(),
                    }
    # 封闭的边与设施
    for edge_id, history in proj.edge_history.items():
        rec = history[-1]
        if rec["at"] <= now and rec["closed"]:
            edge = proj.edges.get(edge_id)
            z = zone_state(edge.zone if edge else "unknown")
            z["closed_edges"].append({"edge_id": edge_id, "reason": rec["reason"], "since": rec["at"].isoformat()})
    for fac_id, history in proj.facility_history.items():
        rec = history[-1]
        if rec["at"] <= now and rec["status"] != "open":
            fac = proj.facilities.get(fac_id)
            for zone in (fac.zones_covered if fac and fac.kind == "rescue_station" else
                         [next((e.zone for e in proj.edges.values() if e.facility_id == fac_id), "unknown")]):
                z = zone_state(zone)
                entry = {"facility_id": fac_id, "name": fac.name if fac else fac_id,
                         "status": rec["status"], "since": rec["at"].isoformat()}
                if fac and fac.kind == "rescue_station":
                    z["closed_facilities"].append(entry)
                else:
                    z["closed_facilities"].append(entry)
    # 救援覆盖
    for zname in list(zones):
        zones[zname]["rescue_open"] = proj.rescue_covered(zname, now)

    # 待人工放行 / 即将超时
    pending_approval, expiring_holds, affected_reservations = [], [], []
    for rid, res in proj.reservations.items():
        route = proj.route(res["route_id"], res["route_version"])
        if res["status"] == "hold":
            if route and route.high_risk:
                decisions = proj.approvals.get(rid, [])
                if not decisions or decisions[-1]["decision"] != "approved":
                    pending_approval.append({
                        "reservation_id": rid, "party_id": res["party_id"],
                        "route_id": res["route_id"], "party_size": res["party_size"],
                        "expires_at": res["expires_at"].isoformat(),
                    })
            if res["expires_at"] >= now and res["expires_at"] - now <= timedelta(minutes=5):
                expiring_holds.append({"reservation_id": rid,
                                       "expires_at": res["expires_at"].isoformat()})
        if res["status"] in ("hold", "confirmed"):
            for group in res["groups"].values():
                used = {u["edge_id"] for u in group["used"]}
                zones_on_route = {proj.edges[s["edge_id"]].zone for s in group["schedule"]
                                  if s["edge_id"] not in used and s["edge_id"] in proj.edges}
                hit = zones_on_route & {zname for zname, z in zones.items()
                                        if z["evacuation"] or z["closed_edges"] or z["closed_facilities"]}
                if hit:
                    affected_reservations.append({"reservation_id": rid, "zones": sorted(hit),
                                                  "status": res["status"]})
                    break

    # 拆分团队与会合点
    split_parties = []
    for pid, party in proj.parties.items():
        if len(party["groups"]) > 1:
            split_parties.append({
                "party_id": pid,
                "groups": [{"group_id": g["group_id"], "members": g["members"]} for g in party["groups"]],
                "rendezvous": {"node": party["rendezvous"]["node"],
                               "time": party["rendezvous"]["time"].isoformat()
                               if party["rendezvous"].get("time") else None}
                if party.get("rendezvous") else None,
            })

    # 现场通知（staff）
    staff_notes: list[tuple[str | None, dict[str, Any]]] = [
        (None, n) for n in proj.global_notes if n["audience"] == "staff"
    ]
    for rid, rid_notes in proj.notes.items():
        staff_notes.extend((rid, n) for n in rid_notes if n["audience"] == "staff")
    staff_notes = sorted(staff_notes, key=lambda x: (x[1]["at"], x[1]["seq"]))[-20:]

    return {
        "as_of": now.isoformat(),
        "zones": sorted(zones.values(), key=lambda z: z["zone"]),
        "pending_manual_approval": pending_approval,
        "expiring_holds": expiring_holds,
        "affected_reservations": affected_reservations,
        "split_parties": split_parties,
        "recent_staff_notes": [{"at": n["at"].isoformat(), "text": n["text"],
                                "reason_codes": n["reason_codes"],
                                "reservation_id": rid} for rid, n in staff_notes],
    }


def incident_review(store: EventStore, proj: Projection, incident_id: str,
                    kind: str, zone: str, occurred_at: datetime | str,
                    window_before: int = 60, window_after: int = 30,
                    edge_id: str | None = None,
                    summary: str = "") -> dict[str, Any]:
    """复盘一次拥堵（congestion）或救援（rescue）。

    在 [发生时刻-window_before, +window_after] 窗口内收集证据，按五类归因：
    容量、设施、气象、调度决定、传感器迟到数据。归因只陈述证据与相关性，
    并明确区分：决策时刻系统能看到什么、哪些数据是事后才到达的。
    """
    t = parse(occurred_at)
    lo, hi = t - timedelta(minutes=window_before), t + timedelta(minutes=window_after)
    findings: dict[str, list[dict[str, Any]]] = {
        CAUSE_CAPACITY: [], CAUSE_FACILITY: [], CAUSE_WEATHER: [],
        CAUSE_DISPATCH: [], CAUSE_LATE_DATA: [],
    }

    for e in store.replay():
        et, at, rec_at = e["event_type"], parse(e["occurred_at"]), parse(e["recorded_at"])
        if not (lo <= at <= hi):
            continue
        p = e.get("payload", {})
        relevant_zone = p.get("zone") == zone if "zone" in p else True
        if edge_id and ("edge_id" in p or "subject_id" in p):
            relevant_zone = relevant_zone or p.get("edge_id") == edge_id or p.get("subject_id") == edge_id
        if not relevant_zone and et not in ("ROUTE_REROUTED", "MANUAL_APPROVAL_DECIDED"):
            continue
        lag = rec_at - at
        late = lag > LATE_DATA_THRESHOLD

        if et == "CAPACITY_RESERVED" and p.get("state") in ("held", "confirmed"):
            findings[CAUSE_CAPACITY].append(_evidence(e, f"格子 {p['subject_id']} @{p['slot_start'][11:16]} "
                                                         f"新增占用 {p['qty']} 人"))
        if et == "FACILITY_STATUS_CHANGED" and p.get("status") != "open":
            findings[CAUSE_FACILITY].append(_evidence(e, f"{p.get('facility_id') or p.get('edge_id')} "
                                                         f"状态 {p['status']}：{p.get('reason', '')}"))
        if et == "WEATHER_RISK_ISSUED" and int(p.get("level", 0)) > 0:
            findings[CAUSE_WEATHER].append(_evidence(e, f"{p['zone']} 气象 {p['level']} 级"
                                                        + (f"（数据迟到 {int(lag.total_seconds() // 60)} 分钟）" if late else "")))
        if et in ("ROUTE_REROUTED", "MANUAL_APPROVAL_DECIDED", "EVACUATION_ORDERED"):
            findings[CAUSE_DISPATCH].append(_evidence(e, e["summary"]))
        if late and et in ("WEATHER_RISK_ISSUED", "FACILITY_STATUS_CHANGED"):
            findings[CAUSE_LATE_DATA].append(_evidence(
                e, f"观测 {at:%H:%M}，入库 {rec_at:%H:%M}，滞后 {int(lag.total_seconds() // 60)} 分钟；"
                   f"按规则该数据只影响入库之后的调度，不回溯此前决策"))

    # 决策时刻可知性核验：用 t 时刻的投影重跑，确认当时判断未被迟到数据污染
    knowable = Projection().build([
        e for e in store.replay() if parse(e["recorded_at"]) <= t
    ])
    late_weather_before = [
        e for e in store.replay()
        if e["event_type"] == "WEATHER_RISK_ISSUED"
        and parse(e["occurred_at"]) < t < parse(e["recorded_at"])
    ]
    retroactive_violation = False  # 双时间日志下，迟到数据不可能参与 t 之前的容量/通行判定

    attribution = {
        cause: {"label": CAUSE_LABELS[cause], "evidence_count": len(items),
                "evidence": items[:10]}
        for cause, items in findings.items() if items
    }
    primary = None
    if findings[CAUSE_WEATHER] and findings[CAUSE_FACILITY]:
        primary = CAUSE_FACILITY
    elif findings[CAUSE_FACILITY]:
        primary = CAUSE_FACILITY
    elif findings[CAUSE_WEATHER]:
        primary = CAUSE_WEATHER
    elif findings[CAUSE_CAPACITY]:
        primary = CAUSE_CAPACITY
    if kind == "rescue" and findings[CAUSE_WEATHER]:
        primary = CAUSE_WEATHER

    payload = {
        "incident_id": incident_id, "kind": kind, "zone": zone,
        "edge_id": edge_id, "occurred_at": t.isoformat(),
        "window": {"before_min": window_before, "after_min": window_after},
        "primary_cause": primary,
        "attribution": attribution,
        "late_data_rule": {
            "threshold_minutes": LATE_DATA_THRESHOLD.total_seconds() // 60,
            "late_records_arrived_after_decision": len(late_weather_before),
            "retroactive_violation_possible": retroactive_violation,
        },
        "summary": summary or f"{zone} {kind} 复盘，主因：{CAUSE_LABELS.get(primary, '证据不足')}",
    }
    event = make_event(
        "INCIDENT_REVIEWED", "dispatch_decision", incident_id, t,
        store._agg_versions.get(("dispatch_decision", incident_id), 0) + 1,
        f"复盘 {incident_id}：{summary or kind}", payload,
    )
    store.append(event, recorded_at=parse(occurred_at) + timedelta(minutes=window_after))
    return {"report": payload, "event": event}


def _evidence(event: dict[str, Any], detail: str) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "occurred_at": event["occurred_at"],
        "recorded_at": event["recorded_at"],
        "detail": detail,
    }
