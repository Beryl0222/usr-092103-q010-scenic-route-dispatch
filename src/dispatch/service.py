"""调度应用服务：命令 -> 事件。

所有命令只产出事件并追加到 EventStore，再由 Projection 重建状态；
服务自身不保存可变状态，崩溃后用事件日志即可恢复。

关键一致性规则：
- 预约占位 hold 有 15 分钟 TTL；确认转 confirmed，超时转 timed_out，
  取消/释放转 released；三种结局都成对释放容量，任何格子不重不漏。
- 高风险线路：资格未核验不可占位；资格齐备仍须人工放行才可确认。
- 停运/封闭/气象/疏散只重排尚未经过的节点：已使用票段（SEGMENT_USED）原样保留，
  旧未来格子容量释放、新格子容量按 confirmed 重新预留。
- 紧急疏散优先于普通偏好：不做个性化，优先最早可行的撤往集合点线路。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from .events import EventError, make_event
from .network import (
    HOLD_TTL_MINUTES,
    SLOT_MINUTES,
    WEATHER_MAX_LEVEL,
    RouteVersion,
    slot_start,
)
from .planner import (
    EVACUATION_ACTIVE,
    MANUAL_APPROVAL_REQUIRED,
    NO_SAFE_CONTINUATION,
    Planner,
)
from .projection import Projection
from .store import EventStore
from .timeutil import format_value, parse


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class DispatchService:
    def __init__(self, store: EventStore, projection: Projection | None = None) -> None:
        self.store = store
        self.proj = projection or Projection()
        self.proj.build(store.all)
        # 疏散分区 -> 撤往安全线路（发布过的 route_id）
        self.evacuation_routes: dict[str, str] = {}

    # ============================================================== 内部工具
    def _rebuild(self) -> None:
        self.proj = Projection().build(self.store.all)

    def _planner(self, now: datetime | str) -> Planner:
        return Planner(self.proj, now)

    def _append(self, event: dict[str, Any], recorded_at: datetime | str | None = None) -> dict[str, Any]:
        stored = self.store.append(event, recorded_at=recorded_at)
        self.proj.apply(stored)
        return stored

    def _reserve_cells(self, rid: str, schedule: list[dict[str, Any]], slot_id: str | None,
                       enter: datetime, party_size: int, state: str,
                       recorded_at: datetime, from_state: str | None = None) -> list[dict[str, Any]]:
        """为入口与时刻表覆盖的边/设施格子登记容量事件。"""
        events: list[dict[str, Any]] = []
        targets: list[tuple[str, str, datetime]] = []
        if slot_id:
            targets.append(("entry_slot", slot_id, slot_start(enter)))
        for item in schedule:
            edge = self.proj.edges.get(item["edge_id"])
            if edge is None:
                continue
            enter_at = parse(item["enter_at"])
            exit_at = parse(item["exit_at"])
            if edge.capacity_per_slot is not None or edge.facility_id:
                cell = slot_start(enter_at)
                last = slot_start(exit_at - timedelta(seconds=1))
                while cell <= last:
                    if edge.capacity_per_slot is not None:
                        targets.append(("edge", edge.edge_id, cell))
                    if edge.facility_id:
                        targets.append(("facility", edge.facility_id, cell))
                    cell += timedelta(minutes=SLOT_MINUTES)
        for subject_type, subject_id, cell in sorted(set(targets)):
            payload = {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "slot_start": cell.isoformat(),
                "qty": party_size,
                "state": state,
                "reservation_id": rid,
            }
            if from_state:
                payload["from_state"] = from_state
            agg = f"{subject_type}:{subject_id}"
            ver = self._next_version("capacity_window", agg)
            events.append(self._append(make_event(
                "CAPACITY_RESERVED", "capacity_window", agg, recorded_at, ver,
                f"容量预留 {subject_type} {subject_id} {cell:%H:%M} {state}", payload,
                event_id=_uid(f"cap-{agg.replace(':', '-')}-{cell.strftime('%H%M')}"),
            ), recorded_at=recorded_at))
        return events

    def _next_version(self, agg_type: str, agg_id: str) -> int:
        return self.store._agg_versions.get((agg_type, agg_id), 0) + 1

    # ============================================================== 线路与设施
    def publish_route(self, route: RouteVersion, edges: list, facilities: list,
                      occurred_at: datetime | str) -> dict[str, Any]:
        payload = {
            "route": route.to_payload(),
            "edges": [e.to_payload() for e in edges],
            "facilities": [f.to_payload() for f in facilities],
        }
        return self._append(make_event(
            "ROUTE_PUBLISHED", "route_revision", route.route_id, occurred_at,
            self._next_version("route_revision", route.route_id),
            f"发布{route.tier}线路 v{route.version}", payload,
        ), recorded_at=occurred_at)

    def open_entry_slot(self, slot_id: str, gate: str, start: datetime | str,
                        end: datetime | str, quota: int, tiers: list[str],
                        occurred_at: datetime | str) -> dict[str, Any]:
        return self._append(make_event(
            "ENTRY_SLOT_OPENED", "entry_slot", slot_id, occurred_at,
            self._next_version("entry_slot", slot_id),
            f"开放入口时段 {slot_id}（{gate}，名额 {quota}）",
            {"slot_id": slot_id, "gate": gate, "start": format_value(parse(start)),
             "end": format_value(parse(end)), "quota": quota, "tiers": tiers},
        ), recorded_at=occurred_at)

    def change_facility(self, occurred_at: datetime | str, status: str, reason: str,
                        facility_id: str | None = None, edge_id: str | None = None,
                        recorded_at: datetime | str | None = None) -> dict[str, Any]:
        if not facility_id and not edge_id:
            raise EventError("facility_id 与 edge_id 至少给一个")
        agg = facility_id or edge_id
        event = self._append(make_event(
            "FACILITY_STATUS_CHANGED", "facility", agg, occurred_at,
            self._next_version("facility", agg),
            f"设施状态变更：{reason}",
            {"status": status, "reason": reason,
             **({"facility_id": facility_id} if facility_id else {}),
             **({"edge_id": edge_id} if edge_id else {})},
        ), recorded_at=recorded_at or occurred_at)
        # 停运/封闭立即影响在场团队的未完成路段
        self.handle_disruption(parse(recorded_at or occurred_at))
        return event

    # ============================================================== 气象与疏散
    def issue_weather(self, zone: str, level: int, occurred_at: datetime | str,
                      title: str = "", source: str = "sensor",
                      recorded_at: datetime | str | None = None) -> dict[str, Any]:
        event = self._append(make_event(
            "WEATHER_RISK_ISSUED", "weather_advisory", zone, occurred_at,
            self._next_version("weather_advisory", zone),
            f"{zone} 气象风险 {level} 级 {title}",
            {"zone": zone, "level": level, "title": title, "source": source},
        ), recorded_at=recorded_at or occurred_at)
        self.handle_disruption(parse(recorded_at or occurred_at))
        return event

    def clear_weather(self, zone: str, occurred_at: datetime | str,
                      recorded_at: datetime | str | None = None) -> dict[str, Any]:
        return self._append(make_event(
            "WEATHER_RISK_CLEARED", "weather_advisory", zone, occurred_at,
            self._next_version("weather_advisory", zone), f"{zone} 气象风险解除",
            {"zone": zone},
        ), recorded_at=recorded_at or occurred_at)

    def order_evacuation(self, zone: str, reason: str, occurred_at: datetime | str,
                         must_clear_by: datetime | str | None = None,
                         assembly_nodes: list[str] | None = None,
                         safe_route_id: str | None = None) -> dict[str, Any]:
        if safe_route_id:
            self.evacuation_routes[zone] = safe_route_id
        event = self._append(make_event(
            "EVACUATION_ORDERED", "evacuation", zone, occurred_at,
            self._next_version("evacuation", zone),
            f"{zone} 紧急疏散：{reason}",
            {"zone": zone, "reason": reason,
             "must_clear_by": format_value(parse(must_clear_by)) if must_clear_by else None,
             "assembly_nodes": assembly_nodes or []},
        ), recorded_at=occurred_at)
        # 疏散优先：立刻对受影响在场团队执行撤离重排，不等普通批处理
        self.handle_disruption(parse(occurred_at), evacuation_zone=zone)
        return event

    def stand_down_evacuation(self, zone: str, occurred_at: datetime | str) -> dict[str, Any]:
        return self._append(make_event(
            "EVACUATION_STOOD_DOWN", "evacuation", zone, occurred_at,
            self._next_version("evacuation", zone), f"{zone} 疏散解除", {"zone": zone},
        ), recorded_at=occurred_at)

    # ============================================================== 游客与团队
    def register_party(self, party_id: str, members: list[str], occurred_at: datetime | str,
                       name: str = "") -> dict[str, Any]:
        return self._append(make_event(
            "PARTY_REGISTERED", "visitor_party", party_id, occurred_at,
            self._next_version("visitor_party", party_id),
            f"登记团队 {party_id}（{len(members)} 人）",
            {"party_id": party_id, "name": name, "members": members},
        ), recorded_at=occurred_at)

    def declare_capability(self, visitor_id: str, capability: int,
                           occurred_at: datetime | str, accessibility: list[str] | None = None,
                           qualification_claims: dict[str, str] | None = None,
                           party_id: str | None = None) -> dict[str, Any]:
        return self._append(make_event(
            "VISITOR_CAPABILITY_DECLARED", "visitor", visitor_id, occurred_at,
            self._next_version("visitor", visitor_id),
            f"游客 {visitor_id} 能力声明：等级 {capability}",
            {"visitor_id": visitor_id, "capability": capability,
             "accessibility": accessibility or [],
             "qualification_claims": qualification_claims or {},
             **({"party_id": party_id} if party_id else {})},
        ), recorded_at=occurred_at)

    def update_consent(self, visitor_id: str, personalization: bool,
                       occurred_at: datetime | str) -> dict[str, Any]:
        return self._append(make_event(
            "VISITOR_CONSENT_UPDATED", "visitor", visitor_id, occurred_at,
            self._next_version("visitor", visitor_id),
            f"游客 {visitor_id} 个性化推荐：{'同意' if personalization else '拒绝'}",
            {"visitor_id": visitor_id, "personalization": personalization},
        ), recorded_at=occurred_at)

    def verify_qualification(self, visitor_id: str, qualification: str, result: str,
                             verifier: str, occurred_at: datetime | str,
                             expires_at: datetime | str | None = None) -> dict[str, Any]:
        return self._append(make_event(
            "QUALIFICATION_VERIFIED", "qualification", f"{visitor_id}:{qualification}",
            occurred_at, self._next_version("qualification", f"{visitor_id}:{qualification}"),
            f"{visitor_id} 的{qualification}资格核验：{result}",
            {"visitor_id": visitor_id, "qualification": qualification, "result": result,
             "verifier": verifier,
             "expires_at": format_value(parse(expires_at)) if expires_at else None},
        ), recorded_at=occurred_at)

    def split_party(self, party_id: str, members: list[str], rendezvous_node: str,
                    occurred_at: datetime | str, group_id: str | None = None,
                    rendezvous_time: datetime | str | None = None,
                    reason: str = "") -> dict[str, Any]:
        gid = group_id or "split1"
        event = self._append(make_event(
            "PARTY_SPLIT", "visitor_party", party_id, occurred_at,
            self._next_version("visitor_party", party_id),
            f"团队 {party_id} 拆分 {len(members)} 人，会合点 {rendezvous_node}",
            {"party_id": party_id, "members": members, "group_id": gid,
             "rendezvous_node": rendezvous_node,
             "rendezvous_time": format_value(parse(rendezvous_time)) if rendezvous_time else None,
             "reason": reason},
        ), recorded_at=occurred_at)
        # 拆分后为各组重排剩余路段：明确各自路线、票段继承与会合点
        split_info = {"group_id": gid, "members": list(members),
                      "rendezvous_node": rendezvous_node,
                      "rendezvous_time": rendezvous_time}
        reroute_results = []
        for rid in self._active_reservations_for_party(party_id):
            reroute_results.append(self.reroute(rid, parse(occurred_at), split=split_info))
        return {"event": event, "reroutes": reroute_results}

    # ============================================================== 预约
    def place_reservation(self, route_id: str, party_id: str, slot_id: str,
                          occurred_at: datetime | str,
                          groups: dict[str, list[str]] | None = None,
                          route_version: int | None = None,
                          need_accessible: bool | None = None) -> dict[str, Any]:
        now = parse(occurred_at)
        if groups is None:
            party = self.proj.parties[party_id]
            groups = {g["group_id"]: list(g["members"]) for g in party["groups"]}
        planner = self._planner(now)
        proposal = planner.plan(route_id, groups, slot_id=slot_id,
                                route_version=route_version,
                                need_accessible=need_accessible)
        if not proposal["feasible"]:
            return {"accepted": False, "proposal": proposal, "events": []}

        rid = _uid("res")
        members = [m for ms in groups.values() for m in ms]
        expires = now + timedelta(minutes=HOLD_TTL_MINUTES)
        schedule = proposal["schedule"]
        placed = self._append(make_event(
            "RESERVATION_PLACED", "reservation", rid, now,
            self._next_version("reservation", rid),
            f"占位 {route_id} v{proposal['route_version']}，{len(members)} 人，"
            f"入场 {proposal['window']['earliest']}",
            {"reservation_id": rid, "party_id": party_id, "route_id": route_id,
             "route_version": proposal["route_version"], "slot_id": slot_id,
             "party_size": len(members), "need_accessible": bool(need_accessible),
             "expires_at": expires.isoformat(),
             "groups": [{"group_id": gid, "members": gmembers, "schedule": schedule}
                       for gid, gmembers in groups.items()]},
        ), recorded_at=now)
        self._reserve_cells(rid, schedule, slot_id, parse(proposal["window"]["earliest"]),
                            len(members), "held", now)
        # 高风险线路：占位即提示等待人工放行
        if proposal["gates"]["manual_approval_required"]:
            self._note(rid, now, "staff", "manual_gate",
                       f"高风险线路 {route_id} 已占位，等待资格复核与人工放行；占位 {HOLD_TTL_MINUTES} 分钟超时",
                       [MANUAL_APPROVAL_REQUIRED])
        return {"accepted": True, "reservation_id": rid, "proposal": proposal,
                "expires_at": expires.isoformat(), "events": [placed]}

    def decide_manual_approval(self, rid: str, decision: str, approver: str,
                               occurred_at: datetime | str, reason: str = "") -> dict[str, Any]:
        if decision not in ("approved", "rejected"):
            raise EventError("decision 必须是 approved / rejected")
        res = self.proj.reservations.get(rid)
        if res is None:
            raise EventError(f"预约不存在：{rid}")
        event = self._append(make_event(
            "MANUAL_APPROVAL_DECIDED", "qualification", rid, occurred_at,
            self._next_version("qualification", rid),
            f"人工放行 {rid}：{decision}（{approver}）",
            {"reservation_id": rid, "decision": decision,
             "approver": approver, "reason": reason},
        ), recorded_at=occurred_at)
        if decision == "rejected":
            self._release(rid, parse(occurred_at), f"人工放行驳回：{reason or '无'}")
        return event

    def confirm_reservation(self, rid: str, occurred_at: datetime | str,
                            approver: str = "system") -> dict[str, Any]:
        now = parse(occurred_at)
        res = self.proj.reservations.get(rid)
        if res is None:
            raise EventError(f"预约不存在：{rid}")
        if res["status"] != "hold":
            raise EventError(f"预约 {rid} 当前状态 {res['status']}，不可确认")
        if res["expires_at"] < now:
            self.expire_holds(now)
            raise EventError(f"预约 {rid} 已超时")

        route = self.proj.route(res["route_id"], res["route_version"])
        if route and route.high_risk:
            decisions = self.proj.approvals.get(rid, [])
            if not decisions or decisions[-1]["decision"] != "approved":
                raise EventError(f"高风险线路 {res['route_id']} 必须先经人工放行 approved")

        event = self._append(make_event(
            "RESERVATION_CONFIRMED", "reservation", rid, now,
            self._next_version("reservation", rid),
            f"确认预约 {rid}", {"reservation_id": rid, "approver": approver},
        ), recorded_at=now)
        for group in res["groups"].values():
            self._reserve_cells(rid, group["schedule"], res["slot_id"],
                                parse(group["schedule"][0]["enter_at"]),
                                len(group["members"]), "converted", now, from_state="held")
        return event

    def expire_holds(self, now: datetime | str) -> list[dict[str, Any]]:
        now = parse(now)
        out = []
        for rid, res in list(self.proj.reservations.items()):
            if res["status"] == "hold" and res["expires_at"] < now:
                event = self._append(make_event(
                    "RESERVATION_TIMED_OUT", "reservation", rid, now,
                    self._next_version("reservation", rid),
                    f"占位超时释放 {rid}", {"reservation_id": rid},
                ), recorded_at=now)
                self._release_cells(rid, now, "held")
                out.append(event)
        return out

    def release_reservation(self, rid: str, occurred_at: datetime | str,
                            reason: str = "游客主动取消") -> dict[str, Any]:
        return self._release(rid, parse(occurred_at), reason)

    def _release(self, rid: str, now: datetime, reason: str) -> dict[str, Any]:
        res = self.proj.reservations.get(rid)
        if res is None or res["status"] not in ("hold", "confirmed"):
            raise EventError(f"预约 {rid} 无可释放的占位（{res['status'] if res else '不存在'}）")
        from_state = res["status"]
        event = self._append(make_event(
            "RESERVATION_RELEASED", "reservation", rid, now,
            self._next_version("reservation", rid),
            f"释放预约 {rid}：{reason}",
            {"reservation_id": rid, "reason": reason},
        ), recorded_at=now)
        self._release_cells(rid, now, from_state)
        return event

    def _release_cells(self, rid: str, now: datetime, from_state: str) -> None:
        # 预约状态名 hold 对应容量台账桶名 held
        bucket_state = "held" if from_state == "hold" else from_state
        # 从台账明细反查该预约占着的格子，逐格释放
        for (subject_type, subject_id, slot_iso), details in list(self.proj.ledger_detail.items()):
            d = details.get(rid)
            if not d:
                continue
            qty = d[bucket_state]
            if qty <= 0:
                continue
            agg = f"{subject_type}:{subject_id}"
            self._append(make_event(
                "CAPACITY_RESERVED", "capacity_window", agg, now,
                self._next_version("capacity_window", agg),
                f"释放 {subject_type} {subject_id} {slot_iso}（{qty} 人）",
                {"subject_type": subject_type, "subject_id": subject_id,
                 "slot_start": slot_iso, "qty": qty, "state": "released",
                 "from_state": from_state, "reservation_id": rid},
                event_id=_uid(f"rel-{agg.replace(':', '-')}"),
            ), recorded_at=now)

    def mark_segment_used(self, rid: str, edge_id: str, occurred_at: datetime | str,
                          group_id: str = "main") -> dict[str, Any]:
        now = parse(occurred_at)
        res = self.proj.reservations[rid]
        group = res["groups"][group_id]
        used_edges = {u["edge_id"] for u in group["used"]}
        if edge_id in used_edges:
            raise EventError(f"票段 {edge_id} 已使用，不可重复核销")
        item = next((s for s in group["schedule"] if s["edge_id"] == edge_id), None)
        if item is None:
            raise EventError(f"票段 {edge_id} 不在 {rid}/{group_id} 当前行程中")
        return self._append(make_event(
            "SEGMENT_USED", "reservation", rid, now,
            self._next_version("reservation", rid),
            f"核销票段 {edge_id}（{rid}/{group_id}）",
            {"reservation_id": rid, "group_id": group_id, "edge_id": edge_id,
             "from_node": item["from_node"], "to_node": item["to_node"]},
        ), recorded_at=now)

    # ============================================================== 重排
    def _continuation_route(self, res: dict[str, Any], used_edges: set[str],
                            alt_route_id: str | None, now: datetime) -> RouteVersion | None:
        """构造续程：已使用票段一律保留，从最后已核销边的终点开始。

        默认在已发布网络中搜索一条避开当前阻断/疏散分区的安全路径回到原终点；
        疏散时图搜索到撤往线路的集合点终点。
        """
        from .network import WEATHER_MAX_LEVEL
        original = self.proj.route(res["route_id"], res["route_version"])
        if original is None:
            return None
        last_used = max((i for i, e in enumerate(original.edges) if e in used_edges), default=-1)
        start_node = original.nodes[last_used + 1]
        end_node = original.nodes[-1]
        remaining_original = tuple(original.edges[last_used + 1:])
        need_accessible = res.get("need_accessible", False)
        evac_zones = {z for z in self.proj.evacuations if self.proj.evacuation_at(z, now)}

        def weather_ok(edge: Any) -> bool:
            return self.proj.weather_level_at(edge.zone, now) <= WEATHER_MAX_LEVEL[original.tier]

        evacuating = bool(alt_route_id) or bool(evac_zones)
        if alt_route_id:
            alt = self.proj.route(alt_route_id)
            if alt is None:
                return None
            target_node = alt.nodes[-1]
            allowed = set(alt.edges)

            def edge_ok(edge: Any) -> bool:
                # 疏散时以撤离命令为准：允许穿过疏散分区赶往集合点，
                # 气象阈值让位于撤离；封闭/停运仍由路径搜索与后续仿真拦截
                return True

            found = self._find_path(start_node, target_node, allowed, need_accessible, edge_ok)
            if found is None:
                return None
            alt_edges, alt_nodes = found
            return RouteVersion(
                route_id=alt.route_id, tier=alt.tier, version=alt.version,
                gate=start_node, nodes=tuple(alt_nodes), edges=tuple(alt_edges),
                valid_from=alt.valid_from, min_capability=1, party_max=alt.party_max,
                rendezvous=alt.rendezvous, requires_qualification=None,
            )

        if not remaining_original:
            return None

        def edge_ok_normal(edge: Any) -> bool:
            if edge.zone in evac_zones:
                return False
            return weather_ok(edge)

        detour = self._find_path(start_node, end_node, set(remaining_original),
                                 need_accessible, edge_ok_normal)
        if detour is None:
            return None  # 无可达替代路径，交由人工介入
        alt_edges, alt_nodes = detour
        return RouteVersion(
            route_id=original.route_id, tier=original.tier, version=original.version,
            gate=start_node, nodes=tuple(alt_nodes), edges=tuple(alt_edges),
            valid_from=original.valid_from, min_capability=1, party_max=original.party_max,
            rendezvous=original.rendezvous, requires_qualification=None,
        )

    def _find_path(self, start: str, end: str, preferred: set[str],
                   need_accessible: bool,
                   edge_ok: Any) -> tuple[list[str], list[str]] | None:
        """最短路：优先走 preferred 边（代价 1），借道其他边代价 100+时长。

        edge_ok(edge) 决定该边当前能否通行（封闭/停运/疏散/气象由调用方规定）。
        """
        import heapq
        adj: dict[str, list[tuple[str, str]]] = {}
        for eid, edge in self.proj.edges.items():
            if need_accessible and not edge.accessible:
                continue
            adj.setdefault(edge.from_node, []).append((edge.to_node, eid))
            if edge.bidirectional:
                adj.setdefault(edge.to_node, []).append((edge.from_node, eid))
        dist = {start: 0}
        prev: dict[str, tuple[str, str]] = {}
        heap = [(0, start)]
        while heap:
            cost, node = heapq.heappop(heap)
            if cost > dist.get(node, 1 << 30):
                continue
            if node == end:
                break
            for nxt, eid in adj.get(node, []):
                edge = self.proj.edges[eid]
                hist = self.proj.edge_history.get(eid)
                blocked = bool(hist and hist[-1]["closed"])
                fhist = self.proj.facility_history.get(edge.facility_id) if edge.facility_id else None
                fac_down = bool(fhist and fhist[-1]["status"] != "open")
                if blocked or fac_down or not edge_ok(edge):
                    continue
                step = cost + (1 if eid in preferred else 100 + edge.duration_min)
                if step < dist.get(nxt, 1 << 30):
                    dist[nxt] = step
                    prev[nxt] = (node, eid)
                    heapq.heappush(heap, (step, nxt))
        if end not in dist:
            return None
        edge_ids: list[str] = []
        node = end
        while node != start:
            prev_node, eid = prev[node]
            edge_ids.append(eid)
            node = prev_node
        edge_ids.reverse()
        nodes = [start]
        for eid in edge_ids:
            edge = self.proj.edges[eid]
            nodes.append(edge.to_node if nodes[-1] == edge.from_node else edge.from_node)
        return edge_ids, nodes

    def reroute(self, rid: str, occurred_at: datetime | str,
                evacuation: bool = False, split: dict[str, Any] | None = None) -> dict[str, Any]:
        now = parse(occurred_at)
        res = self.proj.reservations.get(rid)
        if res is None or res["status"] not in ("hold", "confirmed"):
            return {"rerouted": False, "reason": "reservation_not_active"}

        planner = self._planner(now)
        # 在工作副本上处理：拆分时 main 组减员，新组继承已用票段单独排续程
        main_gid = next(iter(res["groups"]))
        working: dict[str, dict[str, Any]] = {
            gid: {"members": list(g["members"]), "schedule": list(g["schedule"]),
                  "used": list(g["used"])}
            for gid, g in res["groups"].items()
        }
        if split:
            off = set(split["members"])
            working[main_gid]["members"] = [m for m in working[main_gid]["members"] if m not in off]
            working[split["group_id"]] = {
                "members": list(off),
                "schedule": list(working[main_gid]["schedule"]),
                "used": list(working[main_gid]["used"]),
            }
        new_groups: dict[str, dict[str, Any]] = {}
        reason_codes: set[str] = set()
        reason_texts: list[str] = []
        feasible = True
        zones = {self.proj.edges[e].zone for wg in working.values()
                 for s in wg["schedule"] for e in [s["edge_id"]] if e in self.proj.edges}
        evac_zone = next((z for z in zones if self.proj.evacuation_at(z, now)), None)
        alt_route = self.evacuation_routes.get(evac_zone) if evac_zone else None

        for gid, wg in working.items():
            used_edges = {u["edge_id"] for u in wg["used"]}
            remaining_members = wg["members"]
            cont = self._continuation_route(res, used_edges, alt_route, now)
            if not remaining_members:
                new_groups[gid] = {"members": remaining_members, "schedule": []}
                continue
            if cont is None or not cont.edges:
                feasible = False
                reason_codes.add(NO_SAFE_CONTINUATION)
                reason_texts.append(f"分组 {gid} 当前位置无可达的安全续程，需要现场引导撤离")
                new_groups[gid] = {"members": remaining_members, "schedule": []}
                continue
            proposal = planner.plan_route(
                cont, {gid: remaining_members},
                earliest=now + timedelta(minutes=SLOT_MINUTES),
                exclude_rid=rid,
                need_accessible=res.get("need_accessible", False),
                personalize=False if evacuation else None,
                evacuation=evacuation or bool(evac_zone),
            )
            if not proposal["feasible"]:
                feasible = False
                reason_codes.update(proposal["all_block_codes"])
                reason_texts.extend(b["message"] for b in proposal["blocks"])
                new_groups[gid] = {"members": remaining_members, "schedule": []}
                continue
            reason_codes.update(self._infer_reroute_reasons(res, now, wg["schedule"],
                                                            proposal["schedule"], used_edges))
            new_groups[gid] = {
                "members": remaining_members,
                "schedule": proposal["schedule"],
                "window": proposal["window"],
            }

        if not feasible and not evacuation:
            # 暂不改动既有行程，等待现场处理；把阻断原因推给现场视图
            self._note(rid, now, "staff", "reroute_failed",
                       "自动改线失败，需要人工介入：" + "；".join(sorted(set(reason_texts)))[:400],
                       sorted(reason_codes))
            return {"rerouted": False, "reason_codes": sorted(reason_codes)}

        # 释放旧未来格子，预留新格子（confirmed 状态不变；hold 维持 held）
        self._release_future_cells(rid, now)
        groups_payload: dict[str, Any] = {}
        for gid, g in new_groups.items():
            if g["schedule"]:
                self._reserve_cells(rid, g["schedule"], None,
                                    parse(g["schedule"][0]["enter_at"]),
                                    len(g["members"]),
                                    "confirmed" if res["status"] == "confirmed" else "held", now)
            groups_payload[gid] = {"schedule": g["schedule"], "members": g["members"]}
            if split and gid == split["group_id"]:
                groups_payload[gid]["copy_used_from"] = main_gid

        reasons = self._reason_messages(sorted(reason_codes), evac_zone, alt_route)
        rendezvous = None
        party = self.proj.parties.get(res["party_id"])
        if split:
            rendezvous = {"node": split["rendezvous_node"],
                          "time": format_value(parse(split["rendezvous_time"]))
                          if split.get("rendezvous_time") else None}
        elif party and party.get("rendezvous"):
            rendezvous = {"node": party["rendezvous"]["node"],
                          "time": party["rendezvous"]["time"].isoformat()
                          if party["rendezvous"].get("time") else None}
        event = self._append(make_event(
            "ROUTE_REROUTED", "dispatch_decision", rid, now,
            self._next_version("dispatch_decision", rid),
            ("紧急疏散改线" if evacuation or evac_zone else "线路调整") + f" {rid}",
            {"reservation_id": rid, "groups": groups_payload,
             "reason_codes": sorted(reason_codes) or ([EVACUATION_ACTIVE] if evac_zone else []),
             "reasons": reasons, "rendezvous": rendezvous,
             "evacuation": bool(evacuation or evac_zone)},
        ), recorded_at=now)
        self._note(rid, now, "visitor", "reroute",
                   self._visitor_text(reasons, rendezvous, evacuation or bool(evac_zone)),
                   sorted(reason_codes))
        self._note(rid, now, "staff", "reroute",
                   f"{'疏散' if evacuation or evac_zone else '改线'}调度 {rid}：" + "；".join(reasons),
                   sorted(reason_codes))
        return {"rerouted": True, "event": event, "reasons": reasons,
                "reason_codes": sorted(reason_codes), "groups": new_groups,
                "rendezvous": rendezvous}

    def _release_future_cells(self, rid: str, now: datetime) -> None:
        # 续程只覆盖未使用票段：按当前 schedule 中未核销的格子释放（已用票段格子不动）
        res = self.proj.reservations[rid]
        used_by_subject: set[tuple[str, str, str]] = set()
        future_by_subject: set[tuple[str, str, str]] = set()
        for gid, group in res["groups"].items():
            used_edges = {u["edge_id"] for u in group["used"]}
            for item in group["schedule"]:
                eid = item["edge_id"]
                bucket = used_by_subject if eid in used_edges else future_by_subject
                edge = self.proj.edges.get(eid)
                if edge is None:
                    continue
                cell = slot_start(parse(item["enter_at"])).isoformat()
                if edge.capacity_per_slot is not None:
                    bucket.add(("edge", eid, cell))
                if edge.facility_id:
                    bucket.add(("facility", edge.facility_id, cell))
        for key in sorted(future_by_subject):
            subject_type, subject_id, slot_iso = key
            d = self.proj.ledger_detail.get(key, {}).get(rid)
            if not d:
                continue
            state = "confirmed" if res["status"] == "confirmed" else "held"
            qty = d[state]
            if qty <= 0:
                continue
            agg = f"{subject_type}:{subject_id}"
            self._append(make_event(
                "CAPACITY_RESERVED", "capacity_window", agg, now,
                self._next_version("capacity_window", agg),
                f"改线释放 {subject_type} {subject_id} {slot_iso}",
                {"subject_type": subject_type, "subject_id": subject_id,
                 "slot_start": slot_iso, "qty": qty, "state": "released",
                 "from_state": state, "reservation_id": rid},
                event_id=_uid(f"rel-{agg.replace(':', '-')}"),
            ), recorded_at=now)

    def handle_disruption(self, now: datetime, evacuation_zone: str | None = None) -> list[dict[str, Any]]:
        """扫描在场有效预约，凡续程在当前事实上不可行者全部重排（疏散优先）。"""
        results = []
        for rid, res in list(self.proj.reservations.items()):
            if res["status"] not in ("confirmed", "hold"):
                continue
            affected = False
            for group in res["groups"].values():
                used = {u["edge_id"] for u in group["used"]}
                for item in group["schedule"]:
                    if item["edge_id"] in used:
                        continue
                    edge = self.proj.edges.get(item["edge_id"])
                    if edge is None:
                        continue
                    # 未核销票段的实际通行不会早于"现在"（延误只可能更晚），
                    # 故按 max(now, 排定时刻) 查当前事实
                    enter = max(now, parse(item["enter_at"]))
                    zone = edge.zone
                    if self.proj.evacuation_at(zone, now):
                        affected = True
                        break
                    if self.proj.edge_closed_at(edge.edge_id, enter):
                        affected = True
                        break
                    if edge.facility_id and self.proj.facility_status_at(edge.facility_id, enter) != "open":
                        affected = True
                        break
                    if not edge.is_open_at(enter):
                        affected = True
                        break
                    route_obj = self.proj.route(res["route_id"], res["route_version"])
                    if route_obj and self.proj.weather_level_at(zone, enter) > WEATHER_MAX_LEVEL[route_obj.tier]:
                        affected = True
                        break
                    if not self.proj.rescue_covered(zone, enter):
                        affected = True
                        break
                if affected:
                    break
            if affected:
                results.append(self.reroute(rid, now, evacuation=evacuation_zone is not None))
        return results

    def _active_reservations_for_party(self, party_id: str) -> list[str]:
        return [rid for rid, res in self.proj.reservations.items()
                if res["party_id"] == party_id and res["status"] in ("hold", "confirmed")]

    def _infer_reroute_reasons(self, res: dict, now: datetime, old_schedule: list[dict],
                               new_schedule: list[dict], used_edges: set[str]) -> set[str]:
        """对比新旧行程，结合当前事实推断改线原因码。"""
        from .planner import (EDGE_CLOSED, FACILITY_CLOSED, MAINTENANCE,
                              WEATHER_TIER_EXCEEDED, CAPACITY_FULL)
        from .network import WEATHER_MAX_LEVEL, slot_start
        route = self.proj.route(res["route_id"], res["route_version"])
        tier = route.tier if route else "scic"
        codes: set[str] = set()
        old_future = [s for s in old_schedule if s["edge_id"] not in used_edges]
        new_edges = {s["edge_id"] for s in new_schedule}
        for s in old_future:
            edge = self.proj.edges.get(s["edge_id"])
            if edge is None:
                continue
            # 未核销票段实际通行不早于现在，用 max(now, 排定时刻) 判定当前事实
            enter = max(now, parse(s["enter_at"]))
            changed = s["edge_id"] not in new_edges
            if changed:
                if self.proj.edge_closed_at(edge.edge_id, enter):
                    codes.add(EDGE_CLOSED)
                elif not edge.is_open_at(enter):
                    codes.add(MAINTENANCE)
                if edge.facility_id and self.proj.facility_status_at(edge.facility_id, enter) != "open":
                    codes.add(FACILITY_CLOSED)
            if self.proj.weather_level_at(edge.zone, enter) > WEATHER_MAX_LEVEL[tier]:
                codes.add(WEATHER_TIER_EXCEEDED)
            if changed and edge.capacity_per_slot is not None:
                used_n = self.proj.capacity_at("edge", edge.edge_id, slot_start(enter))
                if used_n >= edge.capacity_per_slot:
                    codes.add(CAPACITY_FULL)
        return codes

    # ============================================================== 通知
    def _note(self, rid: str | None, now: datetime, audience: str, note_type: str,
              text: str, reason_codes: list[str]) -> dict[str, Any]:
        agg = rid or "global"
        return self._append(make_event(
            "DISPATCH_NOTE_ISSUED", "dispatch_decision", agg, now,
            self._next_version("dispatch_decision", agg),
            f"调度通知（{audience}）：{note_type}",
            {"reservation_id": rid, "audience": audience, "note_type": note_type,
             "text": text, "reason_codes": reason_codes},
        ), recorded_at=now)

    @staticmethod
    def _reason_messages(codes: list[str], evac_zone: str | None, alt_route: str | None) -> list[str]:
        labels = {
            "FACILITY_CLOSED": "索道/天梯等运载设施停运",
            "EDGE_CLOSED": "途经步道封闭",
            "MAINTENANCE": "途经设施进入维护时段",
            "WEATHER_TIER_EXCEEDED": "气象风险超过该分层线路安全阈值",
            "CAPACITY_FULL": "替代路段当前承载已满",
            "EVACUATION_ACTIVE": "分区启动紧急疏散",
            "RESCUE_UNCOVERED": "沿途救援覆盖中断",
            "NO_SAFE_CONTINUATION": "当前位置无可达安全续程，需现场引导撤离",
        }
        msgs = [labels[c] for c in codes if c in labels]
        if evac_zone:
            head = f"分区 {evac_zone} 紧急疏散"
            msgs = [head + (f"，请沿撤往线路 {alt_route} 前往集合点" if alt_route else "，请听从现场人员指挥前往集合点")] + msgs
        return msgs or ["实时约束变化，系统为您调整后续路线"]

    @staticmethod
    def _visitor_text(reasons: list[str], rendezvous: dict | None, evacuation: bool) -> str:
        text = ("【安全提示】" if evacuation else "【改线通知】") + "；".join(reasons)
        text += "。已使用的票段继续有效，仅调整后续节点，给您带来不便敬请谅解。"
        if rendezvous:
            text += f" 团队会合点：{rendezvous['node']}"
            if rendezvous.get("time"):
                text += f"（{rendezvous['time'][11:16]}）"
            text += "。"
        if evacuation:
            text += " 请立即停止原计划项目，优先撤离，勿返回取物。"
        return text
