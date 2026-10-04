"""分层线路规划引擎。

输入：事件投影（当前世界）、线路版本、分组与成员、期望入口时段、规划时刻。
输出：可解释的规划提案——是否可行、入场窗口、逐边时刻表、阻断原因码、
安全提示，以及高风险活动的资格/人工放行门状态。

约束按时刻表逐 10 分钟格子仿真：
入口名额 → 索道/天梯/步道承载 → 维护与停运 → 气象分层阈值 →
游客能力 → 无障碍 → 资格核验 → 救援覆盖 → 紧急疏散。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .network import (
    SLOT_MINUTES,
    TIER_LABELS,
    WEATHER_MAX_LEVEL,
    Edge,
    RouteVersion,
    slot_start,
)
from .projection import Projection
from .timeutil import parse

# 稳定阻断原因码（游客视图、现场视图、复盘归因共用同一套词汇）
EVACUATION_ACTIVE = "EVACUATION_ACTIVE"
WEATHER_TIER_EXCEEDED = "WEATHER_TIER_EXCEEDED"
FACILITY_CLOSED = "FACILITY_CLOSED"
EDGE_CLOSED = "EDGE_CLOSED"
MAINTENANCE = "MAINTENANCE"
CAPACITY_FULL = "CAPACITY_FULL"
ENTRY_SLOT_FULL = "ENTRY_SLOT_FULL"
ENTRY_SLOT_TIER_MISMATCH = "ENTRY_SLOT_TIER_MISMATCH"
CAPABILITY_INSUFFICIENT = "CAPABILITY_INSUFFICIENT"
ACCESSIBILITY_UNMET = "ACCESSIBILITY_UNMET"
QUALIFICATION_UNVERIFIED = "QUALIFICATION_UNVERIFIED"
QUALIFICATION_PENDING = "QUALIFICATION_PENDING"
QUALIFICATION_EXPIRED = "QUALIFICATION_EXPIRED"
RESCUE_UNCOVERED = "RESCUE_UNCOVERED"
PARTY_TOO_LARGE = "PARTY_TOO_LARGE"
MANUAL_APPROVAL_REQUIRED = "MANUAL_APPROVAL_REQUIRED"
NO_ENTRY_WINDOW = "NO_ENTRY_WINDOW"
NO_SAFE_CONTINUATION = "NO_SAFE_CONTINUATION"


class Planner:
    def __init__(self, proj: Projection, now: datetime | str, known_at: datetime | str | None = None):
        self.p = proj
        self.now = parse(now)
        # 决策时刻可知事实上界：晚于该时刻入库的数据一律不看
        self.known_at = parse(known_at) if known_at else self.now

    # ============================================================ 主入口
    def plan(
        self,
        route_id: str,
        groups: dict[str, list[str]],
        slot_id: str | None = None,
        earliest: datetime | str | None = None,
        latest: datetime | str | None = None,
        route_version: int | None = None,
        exclude_rid: str | None = None,
        need_accessible: bool | None = None,
        personalize: bool | None = None,
    ) -> dict[str, Any]:
        route = self.p.route(route_id, route_version)
        if route is None:
            return self._infeasible(route_id, route_version, groups,
                                    [{"code": NO_ENTRY_WINDOW, "message": "线路版本不存在或已下线"}])
        return self.plan_route(route, groups, slot_id=slot_id, earliest=earliest,
                               latest=latest, exclude_rid=exclude_rid,
                               need_accessible=need_accessible, personalize=personalize)

    def plan_route(
        self,
        route: RouteVersion,
        groups: dict[str, list[str]],
        slot_id: str | None = None,
        earliest: datetime | str | None = None,
        latest: datetime | str | None = None,
        exclude_rid: str | None = None,
        need_accessible: bool | None = None,
        personalize: bool | None = None,
        evacuation: bool = False,
    ) -> dict[str, Any]:
        members = [m for ms in groups.values() for m in ms]
        party_size = len(members)
        if need_accessible is None:
            need_accessible = any(self.p.visitors.get(m, {}).get("accessibility") for m in members)
        # 个性化推荐只在全体成员都同意时启用；任一人拒绝即关闭，按客观规则排期
        if personalize is None:
            personalize = all(self.p.visitors.get(m, {}).get("personalization_consent", True) for m in members)

        window_bounds = self._window_bounds(slot_id, earliest, latest)
        if window_bounds is None:
            return self._infeasible(route.route_id, route.version, groups,
                                    [{"code": NO_ENTRY_WINDOW, "message": "没有可用的入口时段"}])
        slot_obj, search_from, search_to = window_bounds

        if slot_obj and route.tier not in slot_obj["tiers"]:
            block = {"code": ENTRY_SLOT_TIER_MISMATCH,
                     "message": f"入口时段 {slot_id} 不接待{TIER_LABELS.get(route.tier, route.tier)}线路"}
            return self._infeasible(route.route_id, route.version, groups, [block], slot_id)
        if party_size > route.party_max:
            return self._infeasible(route.route_id, route.version, groups,
                                    [{"code": PARTY_TOO_LARGE,
                                      "message": f"团队 {party_size} 人超过该线路单团上限 {route.party_max} 人"}],
                                    slot_id)

        # 与具体入场时刻无关的静态/资格门（疏散撤离时一律让位）；
        # 这些门不随入场格子变化，一旦存在即无可行窗口
        static_blocks = [] if evacuation else self._static_blocks(route, members, need_accessible)
        if static_blocks:
            return self._infeasible(
                route.route_id, route.version, groups, static_blocks, slot_id,
            )
        first_blocks: list[dict[str, Any]] = []
        all_block_codes: set[str] = set()
        feasible_times: list[datetime] = []
        schedule_by_time: dict[datetime, list[dict[str, Any]]] = {}

        candidate = search_from
        step = timedelta(minutes=SLOT_MINUTES)
        while candidate <= search_to:
            schedule, blocks = self._simulate(route, groups, slot_obj, candidate,
                                              need_accessible, exclude_rid, route.tier,
                                              evacuation)
            for b in blocks:
                all_block_codes.add(b["code"])
            if not blocks:
                feasible_times.append(candidate)
                schedule_by_time[candidate] = schedule
            elif not first_blocks:
                first_blocks = blocks
            candidate += step

        if not feasible_times:
            return self._infeasible(
                route.route_id, route.version, groups,
                static_blocks + first_blocks or [{"code": NO_ENTRY_WINDOW,
                                                  "message": "该入口时段内所有入场格子均不满足实时约束"}],
                slot_id, all_codes=all_block_codes,
            )

        earliest_enter = feasible_times[0]
        latest_enter = feasible_times[-1]
        # 连续可行段即对外的入场窗口（首段）
        run_end = earliest_enter
        for t in feasible_times[1:]:
            if t == run_end + step:
                run_end = t
            else:
                break
        schedule = schedule_by_time[earliest_enter]
        proposal = {
            "feasible": True,
            "route_id": route.route_id,
            "route_version": route.version,
            "tier": route.tier,
            "slot_id": slot_id,
            "party_size": party_size,
            "groups": groups,
            "window": {
                "earliest": earliest_enter.isoformat(),
                "latest_contiguous": run_end.isoformat(),
                "last_feasible": latest_enter.isoformat(),
                "step_minutes": SLOT_MINUTES,
            },
            "schedule": schedule,
            "blocks": [],
            "gates": self._gates(route, members),
            "safety_tips": self._safety_tips(route, schedule, earliest_enter),
            "personalization_applied": personalize,
            "planned_at": self.now.isoformat(),
            "known_at": self.known_at.isoformat(),
        }
        return proposal

    # ============================================================ 仿真
    def _simulate(self, route: RouteVersion, groups: dict[str, list[str]],
                  slot_obj: dict | None, enter: datetime, need_accessible: bool,
                  exclude_rid: str | None, tier: str,
                  evacuation: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        blocks: list[dict[str, Any]] = []
        schedule: list[dict[str, Any]] = []
        moment = enter

        # 入口名额（按入场格子）——疏散续程不经过入口，跳过
        if slot_obj is not None and not evacuation:
            used = self._occupancy("entry_slot", slot_obj["slot_id"], enter, exclude_rid)
            quota = slot_obj["quota"]
            if used + len([m for ms in groups.values() for m in ms]) > quota:
                blocks.append({"code": ENTRY_SLOT_FULL,
                               "message": f"入口时段 {slot_obj['slot_id']} {enter:%H:%M} 名额剩余 {max(0, quota - used)}",
                               "subject": slot_obj["slot_id"], "at": enter.isoformat()})

        for edge_id in route.edges:
            edge = self.p.edges.get(edge_id)
            if edge is None:
                blocks.append({"code": EDGE_CLOSED, "message": f"边 {edge_id} 在当前版本中缺失",
                               "subject": edge_id, "at": moment.isoformat()})
                continue
            exit_moment = moment + timedelta(minutes=edge.duration_min)
            blocks.extend(self._edge_blocks(edge, moment, exit_moment, groups,
                                            need_accessible, exclude_rid, tier, evacuation))
            schedule.append({"edge_id": edge_id, "from_node": edge.from_node, "to_node": edge.to_node,
                             "enter_at": moment.isoformat(), "exit_at": exit_moment.isoformat(),
                             "zone": edge.zone, "kind": edge.kind})
            moment = exit_moment
        return schedule, _dedup_blocks(blocks)

    def _edge_blocks(self, edge: Edge, enter: datetime, exit_moment: datetime,
                     groups: dict[str, list[str]], need_accessible: bool,
                     exclude_rid: str | None, tier: str,
                     evacuation: bool = False) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        members = [m for ms in groups.values() for m in ms]
        zone = edge.zone

        # 紧急疏散优先：非疏散规划中，疏散分区内的边一律不可排；
        # 疏散规划中以撤离命令为准，允许穿越疏散分区赶往集合点
        if not evacuation:
            evac = self.p.evacuation_at(zone, enter, self.known_at)
            if evac:
                out.append({"code": EVACUATION_ACTIVE, "subject": zone, "at": enter.isoformat(),
                            "message": f"分区 {zone} 正在紧急疏散（{evac['reason']}），{edge.edge_id} 禁止通行"})

            # 气象分层阈值：取通行区间内的最高等级
            level = self._max_weather(zone, enter, exit_moment)
            max_allowed = WEATHER_MAX_LEVEL[tier]
            if level > max_allowed:
                out.append({"code": WEATHER_TIER_EXCEEDED, "subject": zone, "at": enter.isoformat(),
                            "level": level, "max_allowed": max_allowed,
                            "message": f"分区 {zone} 气象风险 {level} 级，超过该分层线路允许的 {max_allowed} 级"})

        # 停运 / 封闭 / 维护
        closed = self.p.edge_closed_at(edge.edge_id, enter, self.known_at)
        if closed:
            out.append({"code": EDGE_CLOSED, "subject": edge.edge_id, "at": enter.isoformat(),
                        "message": f"{edge.edge_id} 封闭：{closed}"})
        elif not edge.is_open_at(enter):
            out.append({"code": MAINTENANCE, "subject": edge.edge_id, "at": enter.isoformat(),
                        "message": f"{edge.edge_id} 处于维护计划时段"})
        if edge.facility_id and self.p.facility_status_at(edge.facility_id, enter, self.known_at) != "open":
            fac = self.p.facilities.get(edge.facility_id)
            out.append({"code": FACILITY_CLOSED, "subject": edge.facility_id, "at": enter.isoformat(),
                        "message": f"{fac.name if fac else edge.facility_id}停运"})

        # 承载量：跨越的每个 10 分钟格子都要留座
        if edge.capacity_per_slot is not None:
            for cell in _slots_between(enter, exit_moment):
                used = self._occupancy("edge", edge.edge_id, cell, exclude_rid)
                if used + len(members) > edge.capacity_per_slot:
                    out.append({"code": CAPACITY_FULL, "subject": edge.edge_id, "at": cell.isoformat(),
                                "message": f"{edge.edge_id} {cell:%H:%M} 承载不足（{edge.capacity_per_slot - used} 个余量）"})
                    break
        if edge.facility_id:
            fac = self.p.facilities.get(edge.facility_id)
            if fac is not None:
                for cell in _slots_between(enter, exit_moment):
                    used = self._occupancy("facility", fac.facility_id, cell, exclude_rid)
                    if used + len(members) > fac.capacity_per_slot:
                        out.append({"code": CAPACITY_FULL, "subject": fac.facility_id, "at": cell.isoformat(),
                                    "message": f"{fac.name} {cell:%H:%M} 运力不足（剩余 {max(0, fac.capacity_per_slot - used)}）"})
                        break

        # 游客能力、无障碍、资格
        for vid in members:
            v = self.p.visitors.get(vid, {"visitor_id": vid, "capability": 1, "accessibility": []})
            if v.get("capability", 1) < edge.difficulty:
                out.append({"code": CAPABILITY_INSUFFICIENT, "subject": vid, "edge": edge.edge_id,
                            "at": enter.isoformat(),
                            "message": f"游客 {vid} 能力等级 {v.get('capability', 1)} 低于 {edge.edge_id} 要求 {edge.difficulty}"})
            qual = edge.required_qualification
            if qual:
                self._qual_block(vid, qual, enter, out)
        if need_accessible and not edge.accessible:
            out.append({"code": ACCESSIBILITY_UNMET, "subject": edge.edge_id, "at": enter.isoformat(),
                        "message": f"{edge.edge_id} 不满足无障碍通行要求"})
        return out

    def _qual_block(self, vid: str, qual: str, at: datetime, out: list[dict[str, Any]]) -> None:
        v = self.p.visitors.get(vid, {})
        record = v.get("qualifications", {}).get(qual)
        if record is None:
            if qual in v.get("qual_claims", {}):
                out.append({"code": QUALIFICATION_PENDING, "subject": vid, "qualification": qual,
                            "at": at.isoformat(),
                            "message": f"游客 {vid} 的{qual}资格已申报，等待核验"})
            else:
                out.append({"code": QUALIFICATION_UNVERIFIED, "subject": vid, "qualification": qual,
                            "at": at.isoformat(),
                            "message": f"游客 {vid} 缺少{qual}资格核验记录"})
        elif record.get("result") != "verified":
            out.append({"code": QUALIFICATION_UNVERIFIED, "subject": vid, "qualification": qual,
                        "at": at.isoformat(), "message": f"游客 {vid} 的{qual}资格核验未通过"})
        elif record.get("expires_at") and record["expires_at"] < at:
            out.append({"code": QUALIFICATION_EXPIRED, "subject": vid, "qualification": qual,
                        "at": at.isoformat(), "message": f"游客 {vid} 的{qual}资格已过期"})

    # ============================================================ 静态门
    def _static_blocks(self, route: RouteVersion, members: list[str],
                       need_accessible: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for vid in members:
            v = self.p.visitors.get(vid, {"visitor_id": vid, "capability": 1})
            if v.get("capability", 1) < route.min_capability:
                out.append({"code": CAPABILITY_INSUFFICIENT, "subject": vid,
                            "message": f"游客 {vid} 能力等级 {v.get('capability', 1)} 低于线路准入 {route.min_capability}"})
            if route.requires_qualification:
                self._qual_block(vid, route.requires_qualification, self.now, out)
        if route.accessible_only is False and need_accessible:
            # 逐边检查在仿真里做；这里只在线路明确非无障碍且全程无替代时提示
            pass
        # 救援覆盖：线路涉及分区在相应时刻须有开放的救援站点
        zones = {self.p.edges[eid].zone for eid in route.edges if eid in self.p.edges}
        for zone in sorted(zones):
            if not self.p.rescue_covered(zone, self.now, self.known_at):
                out.append({"code": RESCUE_UNCOVERED, "subject": zone,
                            "message": f"分区 {zone} 当前无开放的救援站点覆盖"})
        return _dedup_blocks(out)

    def _gates(self, route: RouteVersion, members: list[str]) -> dict[str, Any]:
        qual_needed: list[dict[str, str]] = []
        quals = {route.requires_qualification} if route.requires_qualification else set()
        quals.update(self.p.edges[eid].required_qualification
                     for eid in route.edges
                     if eid in self.p.edges and self.p.edges[eid].required_qualification)
        for qual in sorted(q for q in quals if q):
            for vid in members:
                record = self.p.visitors.get(vid, {}).get("qualifications", {}).get(qual)
                if not record or record.get("result") != "verified" or (
                        record.get("expires_at") and record["expires_at"] < self.now):
                    qual_needed.append({"visitor_id": vid, "qualification": qual})
        return {
            "high_risk": route.high_risk,
            "qualification_needed": qual_needed,
            # 高风险线路即使资格齐备也必须人工放行
            "manual_approval_required": route.high_risk,
        }

    # ============================================================ 辅助
    def _window_bounds(self, slot_id: str | None, earliest: Any, latest: Any):
        def ceil_slot(m: datetime) -> datetime:
            start = slot_start(m)
            return start if start == m else start + timedelta(minutes=SLOT_MINUTES)

        if slot_id is not None:
            slot_obj = self.p.entry_slots.get(slot_id)
            if slot_obj is None:
                return None
            lower = max(self.now, slot_obj["start"], parse(earliest) if earliest else self.now)
            search_from = ceil_slot(lower)
            search_to = min(slot_obj["end"] - timedelta(minutes=SLOT_MINUTES),
                            parse(latest) if latest else slot_obj["end"])
            return slot_obj, search_from, search_to
        if earliest is None:
            return None
        slot_obj = None
        search_from = ceil_slot(max(self.now, parse(earliest)))
        search_to = slot_start(parse(latest)) if latest else search_from + timedelta(hours=1)
        return slot_obj, search_from, search_to

    def _occupancy(self, subject_type: str, subject_id: str, cell: datetime,
                   exclude_rid: str | None) -> int:
        used = self.p.capacity_at(subject_type, subject_id, cell)
        # 重排时剔除本团队旧占位（服务层也会先发释放事件，双保险避免重复计数）
        if exclude_rid:
            detail = self.p.ledger_detail.get(
                (subject_type, subject_id, slot_start(cell).isoformat()), {}
            ).get(exclude_rid)
            if detail:
                used -= detail["held"] + detail["confirmed"]
        return max(0, used)

    def _max_weather(self, zone: str, start: datetime, end: datetime) -> int:
        level = 0
        t = start
        step = timedelta(minutes=SLOT_MINUTES)
        while t < end:
            level = max(level, self.p.weather_level_at(zone, t, self.known_at))
            t += step
        return level

    def _safety_tips(self, route: RouteVersion, schedule: list[dict[str, Any]], enter: datetime) -> list[str]:
        tips = [f"本线路为{TIER_LABELS.get(route.tier, route.tier)}，全程约"
                f"{sum(self.p.edges[s['edge_id']].duration_min for s in schedule if s['edge_id'] in self.p.edges)} 分钟"]
        zones = {s["zone"] for s in schedule}
        for zone in sorted(zones):
            level = self.p.weather_level_at(zone, enter, self.known_at)
            if level > 0:
                tips.append(f"分区 {zone} 当前气象风险 {level} 级，请按提示穿戴装备、勿偏离步道")
        if route.min_capability >= 4:
            tips.append("线路含高强度路段，请评估体力，身体不适立即联系最近救援站")
        if route.high_risk:
            tips.append("高风险活动：须完成资格核验并经现场人员人工放行后方可入场")
        rescue_names = [f.name for f in self.p.facilities.values()
                        if f.kind == "rescue_station" and zones & set(f.zones_covered)]
        if rescue_names:
            tips.append("沿途救援覆盖：" + "、".join(rescue_names))
        return tips

    def _infeasible(self, route_id, version, groups, blocks, slot_id=None, all_codes=None) -> dict[str, Any]:
        return {
            "feasible": False,
            "route_id": route_id,
            "route_version": version,
            "slot_id": slot_id,
            "groups": groups,
            "window": None,
            "schedule": [],
            "blocks": _dedup_blocks(blocks),
            "all_block_codes": sorted(all_codes or {b["code"] for b in blocks}),
            "gates": {"high_risk": False, "qualification_needed": [], "manual_approval_required": False},
            "safety_tips": [],
            "personalization_applied": False,
            "planned_at": self.now.isoformat(),
            "known_at": self.known_at.isoformat(),
        }


def _slots_between(enter: datetime, exit_moment: datetime) -> list[datetime]:
    cells: list[datetime] = []
    cell = slot_start(enter)
    last = slot_start(exit_moment - timedelta(seconds=1))
    while cell <= last:
        cells.append(cell)
        cell += timedelta(minutes=SLOT_MINUTES)
    return cells


def _dedup_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for b in blocks:
        key = (b["code"], b.get("subject"), b.get("at", ""), b.get("edge", ""))
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out
