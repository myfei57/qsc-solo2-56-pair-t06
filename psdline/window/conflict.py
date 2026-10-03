"""冲突检测：窗口之间（硬冲突）与窗口对排班（接车冲突）。

两类冲突性质不同，分开返回：

* :class:`WindowConflict` —— 和别的申请/已批窗口抢同一樘门。两个已承诺占用不能
  共存，恒为硬冲突；唯一例外是本站策略允许 ``force`` 时顶掉对方**尚未到点**的
  窗口（已 active 的门绝不能顶，现场有人）。
* :class:`ScheduleConflict` —— 时段内有接车。是否致命由站策略决定：保运营的站
  视为致命，保检修的站不致命（让路方案是改排班）。

挂账门（``expired_unconfirmed``）不走冲突表，而是申请入口单独闸住——它不占时刻，
但现场收工没人确认，新申请没有资格进来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .occupancy import Interval, overlaps
from .timetable import Timetable
from .window import APPROVED, MaintenanceWindow


@dataclass(frozen=True)
class Conflict:
    """冲突基类，描述"哪樘门、在什么时段、因为什么"。"""

    gate_id: str
    interval: Interval
    kind: str

    def base(self) -> dict:
        return {"gate_id": self.gate_id, "kind": self.kind, **self.interval.as_dict()}


@dataclass(frozen=True)
class WindowConflict(Conflict):
    """和另一扇检修窗口抢门。"""

    window_id: str = ""
    window_status: str = ""
    requested_by: str = ""
    reason: str = ""
    forceable: bool = False

    def as_dict(self) -> dict:
        return {
            **self.base(),
            "window_id": self.window_id,
            "window_status": self.window_status,
            "requested_by": self.requested_by,
            "reason": self.reason,
            "forceable": self.forceable,
        }


@dataclass(frozen=True)
class ScheduleConflict(Conflict):
    """时段内有接车。"""

    train_id: str = ""
    arrive_at: float = 0.0
    depart_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            **self.base(),
            "train_id": self.train_id,
            "arrive_at": self.arrive_at,
            "depart_at": self.depart_at,
        }


@dataclass
class ConflictReport:
    """一次申请的全部冲突，以及由此得到的判定。"""

    window_conflicts: list[WindowConflict] = field(default_factory=list)
    schedule_conflicts: list[ScheduleConflict] = field(default_factory=list)

    @property
    def hard(self) -> bool:
        """有门窗冲突即硬冲突（force 只能消解可顶让的部分）。"""

        return bool(self.window_conflicts)

    @property
    def forceable(self) -> bool:
        return bool(self.window_conflicts) and all(item.forceable for item in self.window_conflicts)

    def gate_conflicts(self) -> dict[str, list[str]]:
        summary: dict[str, list[str]] = {}
        for item in self.window_conflicts:
            summary.setdefault(item.gate_id, []).append(f"window:{item.window_id}")
        for item in self.schedule_conflicts:
            summary.setdefault(item.gate_id, []).append(f"train:{item.train_id}")
        return summary

    def as_dict(self) -> dict:
        return {
            "hard": self.hard,
            "forceable": self.forceable,
            "window_conflicts": [item.as_dict() for item in self.window_conflicts],
            "schedule_conflicts": [item.as_dict() for item in self.schedule_conflicts],
        }


class ConflictFinder:
    """无状态冲突判定：给窗口集合与时刻表，返回任意申请区间的冲突报告。"""

    def find(
        self,
        gate_ids: list[str],
        wanted: Interval,
        live_windows: list[MaintenanceWindow],
        timetables: dict[str, Timetable] | None = None,
        *,
        allow_force: bool = False,
        ignore_window_id: str = "",
    ) -> ConflictReport:
        report = ConflictReport()
        report.window_conflicts = self._window_conflicts(
            gate_ids, wanted, live_windows, allow_force, ignore_window_id
        )
        report.schedule_conflicts = self._schedule_conflicts(
            gate_ids, wanted, timetables or {}
        )
        return report

    def _window_conflicts(
        self,
        gate_ids: list[str],
        wanted: Interval,
        live_windows: list[MaintenanceWindow],
        allow_force: bool,
        ignore_window_id: str,
    ) -> list[WindowConflict]:
        found: list[WindowConflict] = []
        for other in live_windows:
            if other.id == ignore_window_id or other.status == "expired_unconfirmed":
                continue
            shared = [gate_id for gate_id in gate_ids if gate_id in other.gate_ids]
            if not shared:
                continue
            other_interval = Interval(other.start_at, other.end_at)
            if not overlaps(wanted, other_interval):
                continue
            for gate_id in shared:
                found.append(
                    WindowConflict(
                        gate_id=gate_id,
                        interval=Interval(
                            max(wanted.start, other.start_at),
                            min(wanted.end, other.end_at),
                        ),
                        kind="window",
                        window_id=other.id,
                        window_status=other.status,
                        requested_by=other.requested_by,
                        reason=other.reason,
                        # 只有未到点的已批窗口可被顶；active 门现场有人，绝不顶。
                        forceable=allow_force and other.status == APPROVED,
                    )
                )
        return sorted(found, key=lambda item: (item.gate_id, item.window_id))

    def _schedule_conflicts(
        self,
        gate_ids: list[str],
        wanted: Interval,
        timetables: dict[str, Timetable],
    ) -> list[ScheduleConflict]:
        found: list[ScheduleConflict] = []
        for timetable in timetables.values():
            for visit in timetable.visits:
                shared = [gate_id for gate_id in gate_ids if visit.affects(gate_id)]
                if not shared or not overlaps(wanted, visit.interval):
                    continue
                for gate_id in shared:
                    found.append(
                        ScheduleConflict(
                            gate_id=gate_id,
                            interval=Interval(
                                max(wanted.start, visit.arrive_at),
                                min(wanted.end, visit.depart_at),
                            ),
                            kind="schedule",
                            train_id=visit.train_id,
                            arrive_at=visit.arrive_at,
                            depart_at=visit.depart_at,
                        )
                    )
        return sorted(found, key=lambda item: (item.gate_id, item.arrive_at))

    def conflicts_for_gate(
        self,
        gate_id: str,
        wanted: Interval,
        live_windows: list[MaintenanceWindow],
        timetable: Timetable | None,
    ) -> tuple[list[WindowConflict], list[ScheduleConflict]]:
        """单门版本，让路规划逐门试排时复用。"""

        report = self.find(
            [gate_id], wanted, live_windows, {gate_id: timetable} if timetable else None
        )
        return report.window_conflicts, report.schedule_conflicts
