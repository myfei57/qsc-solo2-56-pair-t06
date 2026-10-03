"""检修窗口领域服务：申请、冲突裁决、生命周期与到期释放。

服务是唯一的写入口，规则集中在这里：

1. **申请** (:meth:`apply_window`)：校验门 → 挂账闸 → 冲突检测 → 按站策略裁决。
   一次申请产生的"批准窗口 / 顶让旧窗口 / 拒绝事件 / 审计"打成**一个事件批**提交，
   任何一步失败整批回滚，不会出现"旧窗口取消了、新窗口没落上"的半完成现场。
2. **到点扫描** (:meth:`tick`)：控制周期调用。到点窗口转 ``active`` 并对门落锁；
   到点 + 宽限结束的窗口自动释放门体，转 ``expired_unconfirmed`` 挂账。
3. **人工销账** (:meth:`confirm_close`)：值班人确认现场收工，挂账转不可变历史。
   未销账的门不能再申请。

门控联动不直接 import 门控包，走装配期注入的 ``lock_applier`` 回调（门体上锁/
解锁、自动开门屏蔽都在平台装配处接线），避免领域层与设备层互相耦合。
"""

from __future__ import annotations

import functools
import threading
from typing import Callable

from ..clock import Clock
from ..errors import RailError
from ..ids import IdFactory
from .conflict import ConflictFinder
from .occupancy import Interval, Occupancy, occupied_gates
from .store import ChainBroken, WindowStore, event
from .timetable import Timetable, TimetableRegistry, TrainVisit
from .window import (
    ACTIVE,
    APPROVED,
    CANCELLED,
    CLOSED,
    EXPIRED_UNCONFIRMED,
    MAINTENANCE_FIRST,
    SERVICE_FIRST,
    MaintenanceWindow,
    StationWindowPolicy,
)
from .yieldplan import YieldPlanner


#: lock_applier 回调签名：(门清单, 是否锁定, 窗口) -> 受影响门标识
LockApplier = Callable[[list[str], bool, MaintenanceWindow], list[str]]


def _decision_locked(method):
    """把整个服务方法收进决策锁临界区（可重入）。"""

    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        with self._decision:
            return method(self, *args, **kwargs)

    return guarded


class WindowService:
    """检修窗口领域服务。"""

    def __init__(
        self,
        store: WindowStore,
        timetables: TimetableRegistry,
        clock: Clock,
        *,
        finder: ConflictFinder | None = None,
        planner: YieldPlanner | None = None,
    ) -> None:
        self._store = store
        self._timetables = timetables
        self._clock = clock
        self._finder = finder or ConflictFinder()
        self._planner = planner or YieldPlanner(self._finder)
        self._policies: dict[str, StationWindowPolicy] = {}
        self._default_policy = StationWindowPolicy()
        self._ids = IdFactory("win")
        # 装配期注入：审计、事件总线、门体联动、门清单解析。
        self.audit: Callable[..., object] | None = None
        self.publish: Callable[[str, str, str], None] | None = None
        self.lock_applier: LockApplier | None = None
        self.gate_lister: Callable[[str], list[str]] | None = None
        # 故障注入：提交前抛出，验证整批回滚。
        self.fault_on_commit: bool = False
        # 决策锁：把"查冲突 → 改生命周期 → 提交"收成临界区，杜绝两笔申请都通过
        # 检测后双双落盘的 TOCTOU。可重入，允许服务内方法互相调用。
        self._decision = threading.RLock()

    # ---- 策略 ---------------------------------------------------------

    @_decision_locked
    def set_default_policy(self, policy: StationWindowPolicy) -> None:
        self._default_policy = policy.validate()

    @_decision_locked
    def set_station_policy(self, station_id: str, policy: StationWindowPolicy) -> None:
        if not station_id:
            raise RailError("站台标识为空", component="window")
        self._policies[station_id] = policy.validate()

    def policy_of(self, station_id: str) -> StationWindowPolicy:
        return self._policies.get(station_id, self._default_policy)

    def policies(self) -> dict:
        return {
            "default": self._default_policy.as_dict(),
            "stations": {station_id: policy.as_dict() for station_id, policy in sorted(self._policies.items())},
        }

    # ---- 时刻表 -------------------------------------------------------

    @_decision_locked
    def seed_timetable(
        self, station_id: str, visits: list[TrainVisit], note: str = "初始排班"
    ) -> dict:
        revision = self._timetables.next_revision(station_id)
        self._timetables.save(
            Timetable(
                station_id=station_id,
                revision=revision,
                visits=visits,
                created_at=self._clock.now(),
                note=note,
            )
        )
        self._audit("window_timetable_seeded", station_id, revision=revision, visits=len(visits))
        return {"station_id": station_id, "revision": revision, "visits": len(visits)}

    @_decision_locked
    def displace_timetable(
        self, station_id: str, displacements: list[dict], note: str
    ) -> dict:
        """按让路方案把接车顺延，写新版本时刻表（旧版本保留）。"""

        latest = self._timetables.require_latest(station_id)
        moves = {
            str(item["train_id"]): (
                float(item["suggested_arrive_at"]),
                float(item["suggested_depart_at"]),
            )
            for item in displacements
        }
        visits: list[TrainVisit] = []
        for visit in latest.visits:
            if visit.train_id in moves:
                arrive_at, depart_at = moves[visit.train_id]
                visit = TrainVisit(
                    train_id=visit.train_id,
                    station_id=visit.station_id,
                    arrive_at=arrive_at,
                    depart_at=depart_at,
                    gate_ids=visit.gate_ids,
                    track=visit.track,
                )
            visits.append(visit)
        revision = self._timetables.next_revision(station_id)
        self._timetables.save(
            Timetable(
                station_id=station_id,
                revision=revision,
                visits=visits,
                created_at=self._clock.now(),
                note=note,
            )
        )
        self._audit(
            "window_timetable_displaced",
            station_id,
            revision=revision,
            trains=sorted(moves),
        )
        return {"station_id": station_id, "revision": revision, "displaced": sorted(moves)}

    # ---- 申请 ---------------------------------------------------------

    @_decision_locked
    def apply_window(
        self,
        station_id: str,
        gate_ids: list[str],
        start_at: float,
        end_at: float,
        requested_by: str,
        reason: str,
        *,
        force: bool = False,
    ) -> dict:
        """申请一樘或一组门的检修占用。

        返回结构固定带 ``ok``：拒绝时 ``ok=False`` 且携带 ``yield_plan``，不落任何
        占用；批准时 ``ok=True``，窗口与连带顶让一个事务落盘。
        """

        moment = self._clock.now()
        policy = self.policy_of(station_id)
        gate_ids = self._validate_gates(station_id, gate_ids)
        if end_at <= start_at:
            raise RailError("窗口止点必须晚于起点", component="window")
        wanted = Interval(start_at, end_at)

        unconfirmed = self._unconfirmed_gates(gate_ids)
        if unconfirmed:
            raise RailError(
                f"门尚未完成收工销账，不能再申请: {','.join(sorted(unconfirmed))}",
                component="window",
            )

        live = self._store.live()
        timetable = self._timetables.latest(station_id)
        report = self._finder.find(
            gate_ids,
            wanted,
            live,
            {station_id: timetable} if timetable else None,
            allow_force=policy.allow_force,
        )

        # 硬冲突：可顶让且持 force 时走顶让；否则拒绝。
        forceable_windows: list[MaintenanceWindow] = []
        if report.hard:
            if force and policy.allow_force and report.forceable:
                forceable_windows = self._force_targets(report)
            else:
                return self._reject(
                    station_id, gate_ids, wanted, requested_by, reason, policy,
                    report.as_dict(), live, timetable,
                    why="window",
                )

        # 接车冲突：保运营拒绝并给方案；保检修批准并改排班。
        displacements: list[dict] = []
        if report.schedule_conflicts and policy.conflict_policy == SERVICE_FIRST:
            return self._reject(
                station_id, gate_ids, wanted, requested_by, reason, policy,
                report.as_dict(), live, timetable,
                why="schedule",
            )
        if report.schedule_conflicts and policy.conflict_policy == MAINTENANCE_FIRST:
            proposal = self._planner.plan(
                gate_ids, wanted, live, self._station_gates(station_id), timetable,
                MAINTENANCE_FIRST, report.as_dict()["schedule_conflicts"],
            )
            displace_option = next(
                (option for option in proposal.options if option.kind == "displace_schedule"),
                None,
            )
            if displace_option is None:
                raise RailError("保检修站未能生成排班改点方案", component="window")
            displacements = displace_option.trains

        window = MaintenanceWindow(
            id=self._ids.labeled("mw"),
            station_id=station_id,
            gate_ids=gate_ids,
            start_at=start_at,
            end_at=end_at,
            requested_by=requested_by,
            reason=reason,
            status=APPROVED,
            conflict_policy=policy.conflict_policy,
            created_at=moment,
            conflicts=report.as_dict()["schedule_conflicts"],
            schedule_displacements=displacements,
        ).validate()

        events = []
        # 用副本承载顶让后的终态：事务失败时 live 表里的原窗口不被污染。
        for victim in forceable_windows:
            cancelled_copy = MaintenanceWindow.from_dict(victim.as_dict())
            cancelled_copy.status = CANCELLED
            cancelled_copy.cancelled_at = moment
            cancelled_copy.superseded_by = window.id
            cancelled_copy.revision += 1
            events.append(event("window_cancelled", cancelled_copy, by=requested_by, forced=True))
        events.append(event("window_approved", window, by=requested_by, force=bool(forceable_windows)))

        locked: list[str] = []
        def _hook(_events: list[dict]) -> None:
            if self.fault_on_commit:
                self.fault_on_commit = False
                raise RuntimeError("注入故障：提交前夕存储中断")

        # 排班改点先于窗口提交：两个存储不是一个文件，无法同事务。若改点失败，窗口
        # 尚未落盘，现场无痕迹；若之后窗口提交失败，最坏结果是"车顺延了但没修成"，
        # 安全侧无误，且孤立时刻表版本仍可审计追溯——取舍偏向保运营安全。
        if displacements:
            self.displace_timetable(
                station_id,
                displacements,
                note=f"检修窗口 {window.id} 保检修策略改点",
            )
        self._store.commit(events, moment, before_hook=_hook)
        if wanted.contains(moment):
            locked = self._apply_lock(gate_ids, True, window)
        self._audit(
            "window_apply",
            station_id,
            ok=True,
            window_id=window.id,
            gates=gate_ids,
            start_at=start_at,
            end_at=end_at,
            forced=[victim.id for victim in forceable_windows],
            displaced=sorted(item["train_id"] for item in displacements),
        )
        self._publish("window", "window", "approved")
        return {
            "ok": True,
            "window": window.as_dict(),
            "conflicts": report.as_dict(),
            "gate_conflicts": report.gate_conflicts(),
            "forced_windows": [victim.id for victim in forceable_windows],
            "schedule_displacements": displacements,
            "locked_now": locked,
            "yield_plan": None,
            "violations": [],
        }

    def _reject(
        self, station_id, gate_ids, wanted, requested_by, reason, policy,
        conflicts, live, timetable, *, why: str,
    ) -> dict:
        proposal = self._planner.plan(
            gate_ids, wanted, live, self._station_gates(station_id), timetable,
            policy.conflict_policy, conflicts["window_conflicts"] + conflicts["schedule_conflicts"],
        )
        events = [
            event(
                "window_rejected",
                None,
                window_id="",
                station_id=station_id,
                gates=gate_ids,
                start_at=wanted.start,
                end_at=wanted.end,
                requested_by=requested_by,
                reason=reason,
                why=why,
            )
        ]
        self._store.commit(events, self._clock.now())
        self._audit(
            "window_apply",
            station_id,
            ok=False,
            gates=gate_ids,
            start_at=wanted.start,
            end_at=wanted.end,
            why=why,
        )
        self._publish("window", "window", "rejected")
        message = "与已批检修窗口冲突" if why == "window" else "保运营站：时段内有接车"
        return {
            "ok": False,
            "window": None,
            "conflicts": conflicts,
            "gate_conflicts": self._gate_summary(conflicts),
            "forced_windows": [],
            "schedule_displacements": [],
            "locked_now": [],
            "yield_plan": proposal.as_dict(),
            "violations": [message],
        }

    # ---- 生命周期 -----------------------------------------------------

    @_decision_locked
    def activate(self, window_id: str) -> dict:
        """手工把已批窗口提前置为占用（正常由 tick 自动到点）。"""

        moment = self._clock.now()
        window = self._store.require(window_id)
        if window.is_terminal or window.status == EXPIRED_UNCONFIRMED:
            raise RailError(f"窗口 {window_id} 状态 {window.status} 不能激活", component="window")
        if not window.covers(moment):
            raise RailError("不在窗口时段内，不能激活", component="window")
        changed = MaintenanceWindow.from_dict(window.as_dict())
        changed.status = ACTIVE
        changed.activated_at = moment
        changed.revision += 1
        self._store.commit([event("window_activated", changed)], moment)
        locked = self._apply_lock(changed.gate_ids, True, changed)
        self._audit("window_activate", changed.station_id, window_id=window_id, gates=locked)
        return {"ok": True, "window_id": window_id, "locked": locked, "violations": []}

    @_decision_locked
    def extend_window(
        self, window_id: str, new_end_at: float, requested_by: str
    ) -> dict:
        """续窗：新止点前的冲突照常检测，冲突即拒（续期不享受豁免）。"""

        moment = self._clock.now()
        window = self._store.require(window_id)
        if window.status not in (APPROVED, ACTIVE):
            raise RailError(f"窗口 {window_id} 状态 {window.status} 不能续期", component="window")
        if new_end_at <= window.end_at:
            raise RailError("续窗止点必须晚于原止点", component="window")
        wanted = Interval(window.start_at, new_end_at)
        live = [item for item in self._store.live() if item.id != window_id]
        report = self._finder.find(
            window.gate_ids, wanted, live,
            {window.station_id: self._timetables.require_latest(window.station_id)},
        )
        if report.hard:
            return {
                "ok": False,
                "window_id": window_id,
                "conflicts": report.as_dict(),
                "violations": ["续期时段与其他窗口冲突"],
            }
        if report.schedule_conflicts and window.conflict_policy == SERVICE_FIRST:
            return {
                "ok": False,
                "window_id": window_id,
                "conflicts": report.as_dict(),
                "violations": ["续期时段内有接车（保运营站）"],
            }
        extended_copy = MaintenanceWindow.from_dict(window.as_dict())
        extended_copy.end_at = new_end_at
        extended_copy.revision += 1
        self._store.commit([event("window_extended", extended_copy, by=requested_by)], moment)
        self._audit("window_extend", extended_copy.station_id, window_id=window_id, end_at=new_end_at)
        return {"ok": True, "window": extended_copy.as_dict(), "conflicts": report.as_dict(), "violations": []}

    @_decision_locked
    def cancel(self, window_id: str, requested_by: str) -> dict:
        """取消尚未到点（或到点未锁）的窗口；已 active 的走完工释放。"""

        moment = self._clock.now()
        window = self._store.require(window_id)
        if window.status not in (APPROVED, ACTIVE):
            raise RailError(f"窗口 {window_id} 状态 {window.status} 不能取消", component="window")
        cancelled_copy = MaintenanceWindow.from_dict(window.as_dict())
        cancelled_copy.status = CANCELLED
        cancelled_copy.cancelled_at = moment
        cancelled_copy.revision += 1
        self._store.commit(
            [event("window_cancelled", cancelled_copy, by=requested_by, forced=False)], moment
        )
        released = self._apply_lock(window.gate_ids, False, window)
        self._audit("window_cancel", window.station_id, window_id=window_id, gates=released)
        return {"ok": True, "window_id": window_id, "released": released, "violations": []}

    @_decision_locked
    def complete_early(self, window_id: str, requested_by: str) -> dict:
        """现场提前完工：立即释放门体，仍需事后销账。"""

        moment = self._clock.now()
        window = self._store.require(window_id)
        if window.status not in (APPROVED, ACTIVE):
            raise RailError(f"窗口 {window_id} 状态 {window.status} 不能完工", component="window")
        expired_copy = MaintenanceWindow.from_dict(window.as_dict())
        expired_copy.status = EXPIRED_UNCONFIRMED
        expired_copy.released_at = moment
        expired_copy.revision += 1
        self._store.commit(
            [event("window_expired", expired_copy, early=True, by=requested_by)], moment
        )
        released = self._apply_lock(window.gate_ids, False, window)
        self._audit("window_complete_early", window.station_id, window_id=window_id, gates=released)
        return {"ok": True, "window_id": window_id, "released": released, "violations": []}

    @_decision_locked
    def confirm_close(self, window_id: str, closed_by: str) -> dict:
        """人工销账：确认现场收工，挂账窗口进入不可变历史，门恢复申请资格。"""

        moment = self._clock.now()
        window = self._store.require(window_id)
        if window.status != EXPIRED_UNCONFIRMED:
            raise RailError(
                f"窗口 {window_id} 状态 {window.status}，只有挂账窗口需要销账",
                component="window",
            )
        closed_copy = MaintenanceWindow.from_dict(window.as_dict())
        closed_copy.status = CLOSED
        closed_copy.closed_at = moment
        closed_copy.closed_by = closed_by
        closed_copy.revision += 1
        self._store.commit([event("window_closed", closed_copy, by=closed_by)], moment)
        self._audit("window_close", closed_copy.station_id, window_id=window_id, by=closed_by)
        self._publish("window", "window", "closed")
        return {"ok": True, "window_id": window_id, "closed_by": closed_by, "violations": []}

    # ---- 控制周期 -----------------------------------------------------

    @_decision_locked
    def tick(self, now: float | None = None) -> dict:
        """到点扫描：到点激活上锁，到点+宽限自动释放挂账。一个事件批提交。

        先在副本上算完全部状态变更并提交，提交成功后才动门锁；提交失败则门维持
        原状、下一周期重扫，不会出现"锁已变、状态没落"的半拍。
        """

        moment = self._clock.now() if now is None else now
        activations: list[dict] = []
        expirations: list[dict] = []
        events: list = []
        to_lock: list[MaintenanceWindow] = []
        to_release: list[MaintenanceWindow] = []
        for window in self._store.live():
            if window.status == APPROVED and window.covers(moment):
                changed = MaintenanceWindow.from_dict(window.as_dict())
                changed.status = ACTIVE
                changed.activated_at = moment
                changed.revision += 1
                events.append(event("window_activated", changed, via="tick"))
                to_lock.append(changed)
                activations.append({"window_id": window.id, "locked": list(window.gate_ids)})
            elif window.status in (APPROVED, ACTIVE):
                grace = self.policy_of(window.station_id).expiry_grace_s
                if moment >= window.end_at + grace:
                    changed = MaintenanceWindow.from_dict(window.as_dict())
                    changed.status = EXPIRED_UNCONFIRMED
                    changed.released_at = moment
                    changed.revision += 1
                    events.append(event("window_expired", changed, via="tick", grace_s=grace))
                    to_release.append(changed)
                    expirations.append({"window_id": window.id, "released": list(window.gate_ids)})
        if not events:
            return {
                "ok": True, "at": moment, "activated": [], "expired": [],
                "locked": [], "released": [], "violations": [],
            }
        self._store.commit(events, moment)
        locked_all = [gate for window in to_lock for gate in self._apply_lock(window.gate_ids, True, window)]
        released_all = [
            gate for window in to_release for gate in self._apply_lock(window.gate_ids, False, window)
        ]
        if activations:
            self._audit("window_tick_activate", "", activations=activations)
        if expirations:
            self._audit("window_tick_expire", "", expirations=expirations)
        return {
            "ok": True,
            "at": moment,
            "activated": activations,
            "expired": expirations,
            "locked": sorted(set(locked_all)),
            "released": sorted(set(released_all)),
            "violations": [],
        }

    # ---- 查询：同一份占用 ---------------------------------------------

    def windows(self, station_id: str = "", status: str = "") -> list[MaintenanceWindow]:
        items = self._store.live()
        if station_id:
            items = [item for item in items if item.station_id == station_id]
        if status:
            items = [item for item in items if item.status == status]
        return items

    def get(self, window_id: str) -> MaintenanceWindow | None:
        return self._store.get(window_id)

    def require(self, window_id: str) -> MaintenanceWindow:
        return self._store.require(window_id)

    def occupancy_at(self, moment: float) -> dict[str, Occupancy]:
        """排班、门控、锁定、审计共用：某时刻全部门的有效占用。"""

        return occupied_gates(self._store.live(), moment)

    def occupancy_for_gate(self, gate_id: str, moment: float) -> Occupancy | None:
        return self.occupancy_at(moment).get(gate_id)

    def is_gate_held(self, gate_id: str, moment: float | None = None) -> bool:
        """门控自动开门的唯一判定入口：被占即禁止自动开门。"""

        at = self._clock.now() if moment is None else moment
        return self.occupancy_for_gate(gate_id, at) is not None

    def held_gates(self, moment: float | None = None) -> list[str]:
        return sorted(self.occupancy_at(self._clock.now() if moment is None else moment))

    def history(self) -> list[MaintenanceWindow]:
        return self._store.history()

    def verify(self) -> dict:
        return self._store.verify_chain()

    def chain_error(self) -> ChainBroken | None:
        try:
            self._store.verify_chain()
        except ChainBroken as error:
            return error
        return None

    @staticmethod
    def _gate_summary(conflicts: dict) -> dict[str, list[str]]:
        """把冲突列表折叠成"门 → 撞了什么"的快速视图。"""

        summary: dict[str, list[str]] = {}
        for item in conflicts.get("window_conflicts", []):
            summary.setdefault(item["gate_id"], []).append(f"window:{item['window_id']}")
        for item in conflicts.get("schedule_conflicts", []):
            summary.setdefault(item["gate_id"], []).append(f"train:{item['train_id']}")
        return summary

    # ---- 内部 ---------------------------------------------------------

    def _validate_gates(self, station_id: str, gate_ids: list[str]) -> list[str]:
        if not station_id:
            raise RailError("站台标识为空", component="window")
        cleaned = sorted({str(item) for item in gate_ids if str(item)})
        if not cleaned:
            raise RailError("至少指定一樘门", component="window")
        known = set(self._station_gates(station_id))
        unknown = [gate_id for gate_id in cleaned if gate_id not in known]
        if unknown:
            raise RailError(f"门不属于站台 {station_id}: {','.join(unknown)}", component="window")
        return cleaned

    def _station_gates(self, station_id: str) -> list[str]:
        if self.gate_lister is not None:
            return self.gate_lister(station_id)
        return []

    def _unconfirmed_gates(self, gate_ids: list[str]) -> list[str]:
        blocked = {
            gate_id
            for window in self._store.live()
            if window.status == EXPIRED_UNCONFIRMED
            for gate_id in window.gate_ids
        }
        return [gate_id for gate_id in gate_ids if gate_id in blocked]

    def _force_targets(self, report) -> list[MaintenanceWindow]:
        ids = {item.window_id for item in report.window_conflicts}
        return [self._store.require(window_id) for window_id in sorted(ids)]

    def _apply_lock(self, gate_ids: list[str], locked: bool, window: MaintenanceWindow) -> list[str]:
        if self.lock_applier is None:
            return []
        return self.lock_applier(list(gate_ids), locked, window)

    def _audit(self, kind: str, station_id: str, **detail: object) -> None:
        if self.audit is not None:
            self.audit(kind, station_id, **detail)

    def _publish(self, area: str, device: str, action: str) -> None:
        if self.publish is not None:
            self.publish(area, device, action)


def build_window_service(
    store_root_or_store,
    timetables: TimetableRegistry | None = None,
    clock: Clock | None = None,
) -> WindowService:
    """构造独立服务（不挂平台时用于测试/工具）。"""

    from ..clock import SystemClock
    from ..store.jsonfile import JsonStore

    if isinstance(store_root_or_store, WindowStore):
        window_store = store_root_or_store
        json_store = JsonStore(window_store.root)
        timetable_registry = timetables or TimetableRegistry(json_store)
    else:
        json_store = JsonStore(str(store_root_or_store))
        window_store = WindowStore(json_store)
        timetable_registry = timetables or TimetableRegistry(json_store)
    return WindowService(window_store, timetable_registry, clock or SystemClock())
