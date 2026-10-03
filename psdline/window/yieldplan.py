"""让路方案：冲突拒绝时给出"怎么让"，而不是只说不行。

规划器产出三类可执行建议，按现场代价排序：

``shift``              门不换、时间让：在原申请门组上找一段够长的空档（同时避开
                       其他窗口与接车），整体平移；
``relocate``           时间不换、门让：同站换一组没被占的门，乘客导流由车站组织；
``displace_schedule``  保检修站专用：窗口照批，列出被挤的接车，建议排班改点。

多门申请按"整组一起让"处理：平移要求所有门在同一时段都空；换门要求替部门在整个
区间都空。宁可不给方案，也不返回"修一半"的半截建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .conflict import ConflictFinder
from .occupancy import Interval, complement_intervals, intervals_for_gate, merge_intervals
from .timetable import Timetable
from .window import MAINTENANCE_FIRST, MaintenanceWindow

SHIFT = "shift"
RELOCATE = "relocate"
DISPLACE_SCHEDULE = "displace_schedule"


@dataclass
class YieldOption:
    """一条让路建议。"""

    kind: str
    gate_ids: list[str]
    start_at: float
    end_at: float
    note: str = ""
    trains: list[dict] = field(default_factory=list)
    score: float = 0.0

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "gate_ids": list(self.gate_ids),
            "start_at": self.start_at,
            "end_at": self.end_at,
            "duration_s": self.end_at - self.start_at,
            "note": self.note,
            "trains": list(self.trains),
            "score": self.score,
        }


@dataclass
class YieldProposal:
    """一次让路求解的完整结果。"""

    requested_gates: list[str]
    wanted: Interval
    feasible: bool
    headline: str
    options: list[YieldOption] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "requested_gates": list(self.requested_gates),
            "wanted": self.wanted.as_dict(),
            "feasible": self.feasible,
            "headline": self.headline,
            "options": [option.as_dict() for option in self.options],
            "conflicts": list(self.conflicts),
        }


class YieldPlanner:
    """让路方案求解。

    ``search_horizon_s`` 限定平移建议最多向后找多久；找太远没有现场意义。
    """

    def __init__(self, finder: ConflictFinder | None = None, search_horizon_s: float = 3600.0) -> None:
        self._finder = finder or ConflictFinder()
        self._horizon = search_horizon_s

    def plan(
        self,
        gate_ids: list[str],
        wanted: Interval,
        live_windows: list[MaintenanceWindow],
        station_gates: list[str],
        timetable: Timetable | None,
        conflict_policy: str,
        conflicts: list[dict] | None = None,
    ) -> YieldProposal:
        options: list[YieldOption] = []
        shift = self._shift_option(gate_ids, wanted, live_windows, timetable)
        if shift is not None:
            options.append(shift)
        relocation = self._relocate_option(
            gate_ids, wanted, live_windows, station_gates, timetable, conflict_policy
        )
        if relocation is not None:
            options.append(relocation)
        if conflict_policy == MAINTENANCE_FIRST:
            displacement = self._displace_option(gate_ids, wanted, timetable)
            if displacement is not None:
                options.append(displacement)
        feasible = bool(options)
        if feasible:
            headline = f"找到 {len(options)} 个让路方案，按现场代价排序"
        else:
            horizon_end = wanted.end + self._horizon
            headline = f"{wanted.start:.0f} 之后 {self._horizon:.0f}s 内整组无空档、同站无替部门"
            del horizon_end
        return YieldProposal(
            requested_gates=list(gate_ids),
            wanted=wanted,
            feasible=feasible,
            headline=headline,
            options=options,
            conflicts=list(conflicts or []),
        )

    # ---- 平移 ---------------------------------------------------------

    def _shift_option(
        self,
        gate_ids: list[str],
        wanted: Interval,
        live_windows: list[MaintenanceWindow],
        timetable: Timetable | None,
    ) -> YieldOption | None:
        search = Interval(wanted.start, wanted.end + self._horizon)
        per_gate_slots: list[list[Interval]] = []
        for gate_id in gate_ids:
            busy = intervals_for_gate(live_windows, gate_id)
            if timetable is not None:
                busy = [
                    *busy,
                    *(
                        visit.interval
                        for visit in timetable.visits
                        if visit.affects(gate_id)
                    ),
                ]
            free = [
                slot
                for slot in complement_intervals(search, merge_intervals(busy))
                if slot.duration_s >= wanted.duration_s
            ]
            if not free:
                return None
            per_gate_slots.append(free)
        common = _common_slots(per_gate_slots, wanted.duration_s)
        if not common:
            return None
        chosen = min(common, key=lambda slot: abs(slot.start - wanted.start))
        return YieldOption(
            kind=SHIFT,
            gate_ids=list(gate_ids),
            start_at=chosen.start,
            end_at=chosen.start + wanted.duration_s,
            note="原门组整体平移到最近空档"
            + ("" if chosen.start == wanted.start else "（起点已挪动）"),
            score=abs(chosen.start - wanted.start),
        )

    # ---- 换门 ---------------------------------------------------------

    def _relocate_option(
        self,
        gate_ids: list[str],
        wanted: Interval,
        live_windows: list[MaintenanceWindow],
        station_gates: list[str],
        timetable: Timetable | None,
        conflict_policy: str,
    ) -> YieldOption | None:
        candidates = [gate_id for gate_id in station_gates if gate_id not in gate_ids]
        free: list[str] = []
        for gate_id in candidates:
            window_hits, schedule_hits = self._finder.conflicts_for_gate(
                gate_id, wanted, live_windows, timetable
            )
            if window_hits:
                continue
            # 保运营站换门也要躲开接车；保检修站可接受换门后仍改排班（那是下策，
            # 排在 displace 之前由 score 控制）。
            if schedule_hits and conflict_policy != MAINTENANCE_FIRST:
                continue
            free.append(gate_id)
            if len(free) >= len(gate_ids):
                break
        if len(free) < len(gate_ids):
            return None
        chosen = free[: len(gate_ids)]
        return YieldOption(
            kind=RELOCATE,
            gate_ids=chosen,
            start_at=wanted.start,
            end_at=wanted.end,
            note="同站替部门，时段不变，需同步客运导流",
            score=1.0,
        )

    # ---- 改排班 -------------------------------------------------------

    def _displace_option(
        self,
        gate_ids: list[str],
        wanted: Interval,
        timetable: Timetable | None,
    ) -> YieldOption | None:
        if timetable is None:
            return None
        hit_visits = [
            visit
            for visit in timetable.visits
            if any(visit.affects(gate_id) for gate_id in gate_ids)
            and visit.interval.overlaps(wanted)
        ]
        if not hit_visits:
            return None
        trains: list[dict] = []
        for visit in hit_visits:
            dwell = visit.depart_at - visit.arrive_at
            suggested_arrive = wanted.end
            trains.append(
                {
                    "train_id": visit.train_id,
                    "from_arrive_at": visit.arrive_at,
                    "from_depart_at": visit.depart_at,
                    "suggested_arrive_at": suggested_arrive,
                    "suggested_depart_at": suggested_arrive + dwell,
                    "note": "建议顺延至窗口结束后，需行车调度确认",
                }
            )
        return YieldOption(
            kind=DISPLACE_SCHEDULE,
            gate_ids=list(gate_ids),
            start_at=wanted.start,
            end_at=wanted.end,
            note="窗口照批，以下接车需排班改点",
            trains=trains,
            score=2.0,
        )


def _common_slots(per_gate_slots: list[list[Interval]], duration_s: float) -> list[Interval]:
    """多扇门都有足够长度的公共空档：逐对求交。"""

    common = list(per_gate_slots[0])
    for slots in per_gate_slots[1:]:
        advanced: list[Interval] = []
        for first in common:
            for second in slots:
                start = max(first.start, second.start)
                end = min(first.end, second.end)
                if end - start >= duration_s:
                    advanced.append(Interval(start, end))
        common = merge_intervals(advanced)
        if not common:
            return []
    return common
