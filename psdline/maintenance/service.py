"""检修窗口编排服务。

:class:`MaintenanceService` 是检修域对外的唯一入口，也是"排班、门控、锁定、
审计读同一份占用"的落点：

* :meth:`occupancy` / :meth:`group_inhibited` 是占用的唯一查询路径，门控开关门、
  控制台、调度确认都读它；
* 所有写操作在同一把提交锁内完成"重读冲突 → 追加事件 → 写当前态 → 写指令箱"，
  事件流水是权威，任何一步失败整体不留痕；
* 对门控设备的禁止/恢复指令经 outbox 提交后投递，失败可重试，:meth:`reconcile`
  按期望占用对账补偿，fence 保证旧指令不会放开新占用。

到期策略：到期只进入 ``EXPIRED``（门继续禁用），必须现场确认才释放；超过宽限期
未确认只升级提醒，绝不自动开门。
"""

from __future__ import annotations

import threading
from typing import Callable

from ..clock import Clock
from ..errors import RailError
from ..eventbus import Event, EventBus
from ..ids import IdFactory
from ..store import JsonStore, JsonlJournal
from .filelock import FileLock
from .intervals import overlaps, slice_by_day
from .ledger import WindowLedger
from .planner import (
    HARD,
    Hit,
    SOFT,
    build_proposals,
    occupancy_hits,
    service_hits,
)
from .policy import (
    APPLICANT,
    DISPATCHER,
    MAINTENANCE_FIRST,
    POLICIES,
    SUPERVISOR,
    PolicyRegistry,
    StationPolicy,
)
from .repos import (
    FAILED,
    INHIBIT,
    PENDING,
    RESTORE,
    SENT,
    DoorInhibitorDevice,
    FenceCounter,
    IdempotencyStore,
    Outbox,
    WindowRegistry,
)
from .timetable import DERIVED, TimetableRegistry, derive_slots
from .window import (
    ACTIVE,
    APPROVED,
    CANCELLED,
    EMERGENCY,
    EXPIRED,
    LIVE,
    OCCUPYING,
    RELEASED,
    REJECTED,
    REQUESTED,
    ROUTINE,
    PREEMPTED,
    WINDOW_KINDS,
    Window,
    can_transition,
)

MANAGER = "maintenance"


class MaintenanceService:
    """检修窗口申请、审批、让路、占用查询与设备联动。"""

    def __init__(
        self,
        store: JsonStore,
        clock: Clock,
        bus: EventBus,
        audit,
        *,
        timetable: TimetableRegistry | None = None,
        headways_provider: Callable[[str], list[float]] | None = None,
        group_provider: Callable[[str], list[str]] | None = None,
        device: DoorInhibitorDevice | None = None,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._bus = bus
        self._audit = audit
        self._ids = IdFactory("win")
        self.windows = WindowRegistry(store)
        self.timetable = timetable or TimetableRegistry(store)
        self.idem = IdempotencyStore(store)
        self.outbox = Outbox(store)
        self.fences = FenceCounter(store)
        self.ledger = WindowLedger(
            JsonlJournal(store.root, "window-events", segment_limit=512)
        )
        self._policies = PolicyRegistry(store)
        self._headways = headways_provider or (lambda station_id: [])
        self._groups = group_provider or (lambda station_id: [])
        self.device = device or DoorInhibitorDevice()
        self._crash_hook = crash_hook or (lambda phase: None)
        self._commit_lock = threading.RLock()
        self._restore()

    # ---- 恢复 ---------------------------------------------------------

    def _restore(self) -> None:
        """启动时校验当前态与流水一致，不一致以流水为准重建。"""

        replayed = self.ledger.replay()
        on_disk = {item.id: item for item in self.windows.list()}
        if set(replayed) != set(on_disk) or any(
            replayed[wid].as_dict() != on_disk[wid].as_dict() for wid in replayed
        ):
            # 流水权威：清掉孤儿文档再重建。重放结果都来自已提交事件，直接落盘。
            for wid in set(on_disk) - set(replayed):
                self._store.remove(WindowRegistry._doc_id(wid))
            self.windows.reset_from(list(replayed.values()))

    # ---- 策略与排班 ---------------------------------------------------

    def get_policy(self, station_id: str) -> StationPolicy:
        return self._policies.get(station_id)

    def set_policy(self, station_id: str, actor: str = SUPERVISOR, **fields) -> StationPolicy:
        current = self.get_policy(station_id).as_dict()
        current.update({key: value for key, value in fields.items() if value is not None})
        precedence = str(current.get("precedence", ""))
        if precedence not in POLICIES:
            raise RailError(f"未知的冲突策略: {precedence}", component="maintenance")
        policy = StationPolicy.from_dict({"station_id": station_id, **current})
        saved = self._policies.save(policy)
        self._record("maintenance_policy_set", station_id, actor=actor, precedence=precedence)
        return saved

    def register_service(
        self,
        station_id: str,
        train_id: str,
        arrive: float,
        depart: float,
        *,
        actor: str = DISPATCHER,
        buffer_s: float | None = None,
    ):
        """登记一趟计划接车时段。"""

        policy = self.get_policy(station_id)
        from .timetable import ServiceSlot

        slot = ServiceSlot(
            station_id=station_id,
            train_id=train_id,
            arrive=float(arrive),
            depart=float(depart),
            buffer_s=policy.service_buffer_s if buffer_s is None else float(buffer_s),
        )
        self.timetable.add(slot)
        self._record(
            "maintenance_service_set",
            station_id,
            actor=actor,
            train_id=train_id,
            arrive=arrive,
            depart=depart,
        )
        return slot

    def service_slots(self, station_id: str, start: float, end: float) -> list:
        """视野内的排班占用：计划车次优先，未覆盖区间用 headway 推演补齐。"""

        policy = self.get_policy(station_id)
        planned = self.timetable.within(station_id, start, end, buffered=True)
        covered = self.timetable.list(station_id)
        if covered:
            return planned
        return derive_slots(
            station_id,
            self._headways(station_id),
            start,
            end,
            dwell_s=60.0,
            buffer_s=policy.service_buffer_s,
            service_start_hour=policy.service_start_hour,
            service_end_hour=policy.service_end_hour,
            tz_offset_s=policy.tz_offset_s,
        )

    # ---- 冲突分析 -----------------------------------------------------

    def analyze(
        self,
        station_id: str,
        group_id: str,
        start: float,
        end: float,
        *,
        exclude_window: str = "",
        kind: str = ROUTINE,
        pending_blocks: bool = False,
    ) -> dict:
        """只读冲突分析：撞点、严重度、可否批准、让路方案。

        ``pending_blocks`` 用于审批时刻：其他还在排队的 REQUESTED 申请在审批者眼里
        是硬冲突（先到先得），避免两个并发申请都看到对方"还没批"而双双通过。
        """

        policy = self.get_policy(station_id)
        self._validate_interval(station_id, group_id, start, end, policy)
        slots = self.service_slots(station_id, start, end)
        others = [
            item
            for item in self.windows.list()
            if item.status in LIVE and item.id != exclude_window
        ]
        hard_service = policy.precedence != MAINTENANCE_FIRST
        hits = service_hits(
            station_id, start, end, slots, buffer_s=policy.service_buffer_s, hard=hard_service
        )
        hits += occupancy_hits(station_id, group_id, start, end, others)
        if pending_blocks:
            hits = [
                Hit(
                    kind=hit.kind,
                    severity=HARD,
                    ref=hit.ref,
                    start=hit.start,
                    end=hit.end,
                    detail=hit.detail + "（另一申请正在排队，按先到先得处理）",
                )
                if hit.kind == "pending"
                else hit
                for hit in hits
            ]

        hard = [hit for hit in hits if hit.severity == HARD]
        soft = [hit for hit in hits if hit.severity == SOFT]
        group_busy = {
            group: [
                (item.start, item.end)
                for item in others
                if item.group_id == group and item.status in OCCUPYING
            ]
            for group in self._groups(station_id)
            if group != group_id
        }
        proposals = build_proposals(
            start,
            end,
            hits,
            policy=policy,
            service_slots=slots,
            other_groups=[group for group in self._groups(station_id) if group != group_id],
            group_busy=group_busy,
        )
        service_conflict = any(hit.kind == "service" for hit in hard)
        preemptable = [hit for hit in hard if hit.kind == "occupancy" and self._preemptable(hit.ref)]
        if not hard:
            approvable = True
        elif kind == EMERGENCY:
            # 紧急抢修可以强制：排班冲突由主管签字联动改点；占用冲突只允许挤掉
            # 已批准未开工的常规窗口；ACTIVE/EXPIRED 的占用谁都不能软件挤掉。
            approvable = service_conflict or all(
                hit.kind == "service"
                or (hit.kind == "occupancy" and self._preemptable(hit.ref))
                for hit in hard
            )
        else:
            approvable = False
        return {
            "station_id": station_id,
            "group_id": group_id,
            "start": start,
            "end": end,
            "precedence": policy.precedence,
            "hard_conflicts": [hit.as_dict() for hit in hard],
            "soft_conflicts": [hit.as_dict() for hit in soft],
            "service_slots": [slot.as_dict() for slot in slots],
            "approvable": approvable,
            "approvable_reason": self._approvable_reason(approvable, hard, kind),
            "requires_override": bool(hard) and kind == EMERGENCY,
            "service_conflict": service_conflict,
            "preemptable": [hit.as_dict() for hit in preemptable],
            "proposals": [proposal.as_dict() for proposal in proposals],
        }

    def _preemptable(self, window_id: str) -> bool:
        # 已批准未开工的常规窗口可被挤掉；排队中的常规申请可被直接拒掉让位。
        window = self.windows.get(window_id)
        return bool(
            window
            and window.kind == ROUTINE
            and window.status in (APPROVED, REQUESTED)
        )

    @staticmethod
    def _approvable_reason(approvable: bool, hard: list[Hit], kind: str) -> str:
        if approvable:
            return "无硬冲突" if not hard else "紧急抢修可由主管强制占用"
        if kind == EMERGENCY:
            return "存在进行中或已到期未释放的占用，紧急抢修也不能软件挤掉"
        return "存在硬冲突，请按让路方案调整或升级为紧急抢修"

    # ---- 申请 ---------------------------------------------------------

    def request_window(
        self,
        station_id: str,
        group_id: str,
        start: float,
        end: float,
        applicant: str,
        *,
        kind: str = ROUTINE,
        reason: str = "",
        ticket: str = "",
        note: str = "",
        idem_key: str = "",
        actor: str = APPLICANT,
    ) -> dict:
        """提交一个检修占用申请（允许带着冲突先排队，审批时再卡）。"""

        if actor not in (APPLICANT, DISPATCHER, SUPERVISOR):
            raise RailError(f"未知角色: {actor}", component="maintenance")
        if idem_key:
            cached = self.idem.lookup(idem_key)
            if cached is not None:
                return {"ok": True, "idempotent": True, **cached}
        kind = kind if kind in WINDOW_KINDS else ROUTINE
        policy = self.get_policy(station_id)
        self._validate_interval(station_id, group_id, start, end, policy)

        with self._commit(station_id):
            now = self._clock.now()
            window = Window(
                id=self._ids.next(),
                station_id=station_id,
                group_id=group_id,
                start=float(start),
                end=float(end),
                applicant=applicant,
                status=REQUESTED,
                kind=kind,
                reason=reason,
                ticket=ticket,
                note=note,
                idem_key=idem_key,
                created_at=now,
                updated_at=now,
            )
            # 事件先行、当前态随后：若在两步之间掉电，重放流水能把当前态补回来；
            # 反过来（先写当前态）则可能留下"有文档、无事件"的脏窗口。
            self.ledger.append(
                window.id,
                "requested",
                now,
                applicant,
                {
                    "station_id": station_id,
                    "group_id": group_id,
                    "start": window.start,
                    "end": window.end,
                    "applicant": applicant,
                    "kind": kind,
                    "reason": reason,
                    "ticket": ticket,
                    "note": note,
                    "idem_key": idem_key,
                },
            )
            self._crash_hook("after_event")
            self.windows.save(window)
            outcome = {"ok": True, "window_id": window.id, "status": REQUESTED}
            if idem_key:
                self.idem.remember(idem_key, outcome)
        self._record(
            "maintenance_requested",
            station_id,
            actor=applicant,
            window_id=window.id,
            group_id=group_id,
            window_kind=kind,
        )
        return {"ok": True, "window_id": window.id, "status": REQUESTED, "analysis": self.analyze(
            station_id, group_id, start, end, exclude_window=window.id, kind=kind
        )}

    # ---- 审批 / 拒批 / 挤占 ------------------------------------------

    def approve_window(
        self,
        window_id: str,
        actor: str,
        *,
        role: str = DISPATCHER,
        force: bool = False,
    ) -> dict:
        """批准申请。service_first 下撞车必须 force（且仅紧急 + 主管）。

        权威冲突复核在提交锁内完成：两个并发审批被锁串行化后，后到者会看到先到者
        已落成的 APPROVED 占用，不可能双双通过。锁外只做角色与状态等快校验。
        """

        window = self.windows.require(window_id)
        if role != DISPATCHER and role != SUPERVISOR:
            raise RailError("只有行车调度或值站主管可以批窗口", component="maintenance")
        if window.status != REQUESTED:
            raise RailError(
                f"窗口 {window_id} 状态为 {window.status}，无法批准", component="maintenance"
            )

        with self._commit(window.station_id):
            # 锁内重读：冲突集以此刻磁盘上的占用为准（不含其他 REQUESTED 申请——
            # 它们被这把锁串行化，先到先得，后到者在第一个批准落盘后自然撞硬冲突）。
            report = self.analyze(
                window.station_id,
                window.group_id,
                window.start,
                window.end,
                exclude_window=window.id,
                kind=window.kind,
            )
            hard = report["hard_conflicts"]
            if hard and not force:
                raise RailError(
                    f"窗口 {window_id} 有 {len(hard)} 处硬冲突，不能批准；"
                    f"请参考让路方案或使用紧急强制",
                    component="maintenance",
                )
            if hard:
                if window.kind != EMERGENCY or role != SUPERVISOR:
                    raise RailError(
                        "强制占用只允许紧急抢修由值站主管执行", component="maintenance"
                    )
                for hit in hard:
                    if hit["kind"] == "service":
                        continue  # 撞车由主管签字、调度联动改点
                    target = self.windows.require(hit["ref"])
                    if not self._preemptable(target.id):
                        raise RailError(
                            f"被挤窗口 {target.id} 状态为 {target.status}，不能软件挤占",
                            component="maintenance",
                        )

            now = self._clock.now()
            preempted: list[str] = []
            rejected: list[str] = []
            if force and hard:
                for hit in hard:
                    if hit["kind"] == "service":
                        continue
                    target = self.windows.require(hit["ref"])
                    if target.status == APPROVED:
                        # 已批准未开工：挤占并恢复它此前发出的门禁用。
                        self._transition(
                            target, PREEMPTED, now, actor, {"note": f"让位于紧急窗口 {window_id}"}
                        )
                        self._enqueue_door(target, RESTORE, actor)
                        preempted.append(target.id)
                    else:
                        self._transition(
                            target, REJECTED, now, actor, {"note": f"紧急窗口 {window_id} 优先"}
                        )
                        rejected.append(target.id)
            self._transition(window, APPROVED, now, actor, {"force": bool(force and hard)})
            self._enqueue_door(window, INHIBIT, actor)
        self._record(
            "maintenance_approved",
            window.station_id,
            actor=actor,
            window_id=window_id,
            forced=bool(force and hard),
            preempted=",".join(preempted),
        )
        self._relay()
        return {
            "ok": True,
            "window_id": window_id,
            "status": APPROVED,
            "preempted": preempted,
            "rejected": rejected,
            "forced": bool(force and hard),
            "affected_trains": sorted(
                {hit["ref"] for hit in hard if hit["kind"] == "service"}
            ),
        }

    def reject_window(self, window_id: str, actor: str, *, role: str = DISPATCHER, note: str = "") -> dict:
        window = self.windows.require(window_id)
        if role not in (DISPATCHER, SUPERVISOR):
            raise RailError("只有行车调度或值站主管可以拒批", component="maintenance")
        with self._commit(window.station_id):
            self._transition(window, REJECTED, self._clock.now(), actor, {"note": note})
        self._record("maintenance_rejected", window.station_id, actor=actor, window_id=window_id)
        return {"ok": True, "window_id": window_id, "status": REJECTED}

    # ---- 开工 / 到期 / 完工 ------------------------------------------

    def start_window(self, window_id: str, actor: str = APPLICANT) -> dict:
        """现场登记开工（也可由 tick 在到点时自动开工）。"""

        window = self.windows.require(window_id)
        with self._commit(window.station_id):
            self._transition(window, ACTIVE, self._clock.now(), actor, {}, etype="started")
        self._record("maintenance_started", window.station_id, actor=actor, window_id=window_id)
        return {"ok": True, "window_id": window_id, "status": ACTIVE}

    def tick(self, at: float | None = None) -> dict:
        """时间推进：到点开工、到期转入 EXPIRED（继续锁门）、宽限期满升级。

        一轮 tick 内允许同一窗口连续走完多个时间点（例如长时间停机后第一个周期
        直接发现 ACTIVE 窗口已过期且超过宽限期），但到期与升级分别记事件、分别
        进返回值，审计上仍然能看出先后。
        """

        moment = self._clock.now() if at is None else float(at)
        started: list[str] = []
        expired: list[str] = []
        escalated: list[str] = []
        for window in self.windows.list():
            if window.status == APPROVED and moment >= window.start:
                with self._commit(window.station_id):
                    self._transition(window, ACTIVE, moment, "system", {}, etype="started")
                started.append(window.id)
                self._record("maintenance_started", window.station_id, actor="system", window_id=window.id)
            # 注意：上面分支可能已把窗口推进到 ACTIVE，这里继续评估到期/升级。
            if window.status == ACTIVE and moment >= window.end:
                with self._commit(window.station_id):
                    self._transition(window, EXPIRED, moment, "system", {})
                expired.append(window.id)
                self._record("maintenance_expired", window.station_id, actor="system", window_id=window.id)
            if (
                window.status == EXPIRED
                and not window.escalated
                and moment >= window.end + self.get_policy(window.station_id).expire_grace_s
            ):
                with self._commit(window.station_id):
                    self.ledger.append(window.id, "escalated", moment, "system", {})
                    window.escalated = True
                    window.updated_at = moment
                    self.windows.save(window)
                escalated.append(window.id)
                self._bus.publish(
                    Event.of(
                        "maintenance",
                        MANAGER,
                        "window_escalated",
                        severity="alarm",
                        station_id=window.station_id,
                        window_id=window.id,
                    )
                )
                self._record(
                    "maintenance_escalated",
                    window.station_id,
                    actor="system",
                    window_id=window.id,
                    overdue_s=moment - window.end,
                )
        self._relay()
        return {"started": started, "expired": expired, "escalated": escalated}

    def finish_window(self, window_id: str, actor: str, *, note: str = "") -> dict:
        """现场完工确认：在期窗口直接收尾释放，门恢复自动开门。"""

        window = self.windows.require(window_id)
        if window.status == EXPIRED:
            raise RailError(
                f"窗口 {window_id} 已到期，完工确认需要值站主管强制收尾",
                component="maintenance",
            )
        if window.status != ACTIVE:
            raise RailError(
                f"窗口 {window_id} 状态为 {window.status}，只有进行中的窗口可以完工",
                component="maintenance",
            )
        with self._commit(window.station_id):
            self._transition(window, RELEASED, self._clock.now(), actor, {"actor": actor})
            self._enqueue_door(window, RESTORE, actor)
        self._record("maintenance_released", window.station_id, actor=actor, window_id=window_id)
        self._relay()
        return {"ok": True, "window_id": window_id, "status": RELEASED}

    def force_finish_window(
        self, window_id: str, actor: str, *, role: str = SUPERVISOR, confirm: str = "", note: str = ""
    ) -> dict:
        """值站主管对到期窗口强制收尾；confirm 必须等于窗口标识（防误操作）。"""

        window = self.windows.require(window_id)
        if role != SUPERVISOR:
            raise RailError("到期窗口收尾必须由值站主管执行", component="maintenance")
        if window.status != EXPIRED:
            raise RailError(
                f"窗口 {window_id} 状态为 {window.status}，无需强制收尾",
                component="maintenance",
            )
        if confirm != window_id:
            raise RailError(
                "强制收尾需要在 confirm 字段重复窗口标识", component="maintenance"
            )
        with self._commit(window.station_id):
            self._transition(
                window, RELEASED, self._clock.now(), actor, {"actor": actor, "note": note}, etype="force_released"
            )
            self._enqueue_door(window, RESTORE, actor)
        self._record(
            "maintenance_force_released",
            window.station_id,
            actor=actor,
            window_id=window_id,
            overdue_s=self._clock.now() - window.end,
        )
        self._relay()
        return {"ok": True, "window_id": window_id, "status": RELEASED}

    def cancel_window(self, window_id: str, actor: str, *, note: str = "") -> dict:
        """取消尚未开工的窗口；进行中/已到期窗口必须走完工流程（防止静默解锁）。"""

        window = self.windows.require(window_id)
        was_approved = window.status == APPROVED
        with self._commit(window.station_id):
            self._transition(window, CANCELLED, self._clock.now(), actor, {"note": note})
            if was_approved:
                self._enqueue_door(window, RESTORE, actor)
        self._record("maintenance_cancelled", window.station_id, actor=actor, window_id=window_id)
        self._relay()
        return {"ok": True, "window_id": window_id, "status": CANCELLED}

    def extend_window(
        self, window_id: str, actor: str, new_end: float, *, role: str = DISPATCHER
    ) -> dict:
        """延长窗口：新时段必须重新过一遍冲突检查。"""

        window = self.windows.require(window_id)
        if window.status not in (APPROVED, ACTIVE):
            raise RailError(
                f"窗口 {window_id} 状态为 {window.status}，不能延期", component="maintenance"
            )
        if float(new_end) <= window.end:
            raise RailError("延期结束时刻必须晚于原结束时刻", component="maintenance")
        policy = self.get_policy(window.station_id)
        if float(new_end) - window.start > policy.max_window_s:
            raise RailError("延期后超过单窗口最长时长", component="maintenance")
        report = self.analyze(
            window.station_id, window.group_id, window.start, float(new_end), exclude_window=window.id
        )
        if report["hard_conflicts"]:
            raise RailError("延长期内存在硬冲突，不能延期", component="maintenance")
        with self._commit(window.station_id):
            self.ledger.append(
                window.id,
                "extended",
                self._clock.now(),
                actor,
                {"start": window.start, "end": float(new_end)},
            )
            window.end = float(new_end)
            window.updated_at = self._clock.now()
            self.windows.save(window)
        self._record(
            "maintenance_extended",
            window.station_id,
            actor=actor,
            window_id=window_id,
            new_end=new_end,
        )
        return {"ok": True, "window_id": window_id, "end": float(new_end)}

    # ---- 统一占用视图 -------------------------------------------------

    def occupancy(self, station_id: str, start: float, end: float) -> dict:
        """唯一占用查询：返回视野内的窗口占用与排班占用，供门控/调度/审计共读。"""

        windows = [
            item
            for item in self.windows.list()
            if item.station_id == station_id
            and item.status in OCCUPYING
            and overlaps(start, end, item.start, item.end)
        ]
        slots = self.service_slots(station_id, start, end)
        return {
            "station_id": station_id,
            "start": start,
            "end": end,
            "windows": [item.as_dict() for item in windows],
            "service": [slot.as_dict() for slot in slots],
            "occupied_groups": sorted({item.group_id for item in windows}),
            "window_count": len(windows),
            "service_count": len(slots),
        }

    def active_window_for(self, station_id: str, group_id: str, at: float | None = None) -> Window | None:
        """返回某门区此刻形成物理占用的窗口（权威门禁用）。

        只要窗口处于 APPROVED/ACTIVE/EXPIRED 就占门，与"现在几点"无关：批准即锁
        （防止批了但还没开工的空当有人开门），EXPIRED 是"已到期未完工确认"的悬挂
        状态也继续占门，直到主管收尾。时间推进（REQUESTED→APPROVED→ACTIVE→
        EXPIRED）由 :meth:`tick` 负责，门禁不重复做时间判断。同刻存在多个窗口时
        （理论上硬冲突会拦）取序号最大的最新窗口。
        """

        candidates = [
            item
            for item in self.windows.list()
            if item.station_id == station_id
            and item.group_id == group_id
            and item.status in OCCUPYING
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.seq)

    def group_inhibited(self, station_id: str, group_id: str, *, at: float | None = None) -> dict:
        """门控查询入口：该门区此刻是否禁止自动开门。"""

        window = self.active_window_for(station_id, group_id, at=at)
        if window is None:
            return {"inhibited": False, "reason": "", "window_id": ""}
        reason = {
            APPROVED: "窗口已批准，等待开工",
            ACTIVE: "检修进行中",
            EXPIRED: "窗口已到期但未完工确认",
        }[window.status]
        return {"inhibited": True, "reason": reason, "window_id": window.id, "status": window.status}

    # ---- 设备投递与对账 ----------------------------------------------

    def relay(self) -> dict:
        """公开的投递触发（测试 / CLI / 控制周期都可调用）。"""

        return self._relay()

    def _relay(self) -> dict:
        sent: list[int] = []
        failed: list[dict] = []
        for command in self.outbox.pending():
            result = self.device.send(command)
            if result.get("ok"):
                self.outbox.mark(command["id"], SENT)
                sent.append(command["id"])
            else:
                self.outbox.mark(command["id"], FAILED, str(result.get("reason", "")))
                failed.append({"id": command["id"], "reason": result.get("reason", "")})
        return {"sent": sent, "failed": failed}

    def reconcile(self) -> dict:
        """按当前占用对账设备：缺指令补指令，旧状态被 fence 自动挡住。"""

        desired: dict[str, tuple[str, str]] = {}
        for window in self.windows.list():
            if window.status in OCCUPYING:
                key = self._door_key(window.station_id, window.group_id)
                desired[key] = (INHIBIT, window.id)
        enqueued: list[str] = []
        for key, (action, window_id) in desired.items():
            if action == INHIBIT and self.device.inhibited(key):
                continue
            if not self.outbox.pending() or not any(
                item.get("key") == key and item.get("action") == action
                for item in self.outbox.pending()
            ):
                command = self._enqueue(key, action, window_id, "system")
                enqueued.append(str(command["id"]))
        result = self._relay()
        return {"compensated": enqueued, **result}

    def outbox_summary(self) -> dict:
        commands = self.outbox.list()
        return {
            "total": len(commands),
            "pending": sum(1 for item in commands if item["status"] == PENDING),
            "sent": sum(1 for item in commands if item["status"] == SENT),
            "failed": sum(1 for item in commands if item["status"] == FAILED),
        }

    # ---- 查询与校验 ---------------------------------------------------

    def get_window(self, window_id: str) -> Window:
        return self.windows.require(window_id)

    def list_windows(self, station_id: str = "", *, statuses: set[str] | None = None) -> list[Window]:
        windows = self.windows.list()
        if station_id:
            windows = [item for item in windows if item.station_id == station_id]
        if statuses is not None:
            windows = [item for item in windows if item.status in statuses]
        return windows

    def window_view(self, window_id: str) -> dict:
        window = self.get_window(window_id)
        policy = self.get_policy(window.station_id)
        return {
            "window": window.as_dict(),
            "service_days": [
                item.as_dict()
                for item in slice_by_day(
                    window.start,
                    window.end,
                    day_boundary_offset_s=policy.day_boundary_offset_s,
                    tz_offset_s=policy.tz_offset_s,
                )
            ],
        }

    def verify_history(self) -> dict:
        """校验事件流水哈希链（历史不可变的对外检查口）。"""

        return self.ledger.verify()

    def _validate_interval(
        self, station_id: str, group_id: str, start: float, end: float, policy: StationPolicy
    ) -> None:
        if not station_id:
            raise RailError("缺少站台", component="maintenance")
        if not group_id:
            raise RailError("缺少门区", component="maintenance")
        if float(end) <= float(start):
            raise RailError("结束时刻必须晚于开始时刻", component="maintenance")
        if float(end) - float(start) > policy.max_window_s:
            raise RailError(
                f"窗口时长超过上限 {policy.max_window_s:.0f} 秒", component="maintenance"
            )
        if group_id not in self._groups(station_id):
            raise RailError(
                f"站台 {station_id} 没有门区 {group_id}", component="maintenance"
            )

    # ---- 内部 ---------------------------------------------------------

    def _commit(self, station_id: str):
        return _Commit(self, station_id)

    def _transition(self, window: Window, target: str, at: float, actor: str, data: dict, *, etype: str = "") -> None:
        if not can_transition(window.status, target):
            raise RailError(
                f"窗口 {window.id} 不能从 {window.status} 转到 {target}",
                component="maintenance",
            )
        self.ledger.append(window.id, etype or target.lower(), at, actor, data)
        window.status = target
        window.updated_at = at
        self.windows.save(window)

    def _door_key(self, station_id: str, group_id: str) -> str:
        return f"{station_id}:{group_id}"

    def _enqueue_door(self, window: Window, action: str, actor: str) -> dict:
        return self._enqueue(
            self._door_key(window.station_id, window.group_id), action, window.id, actor
        )

    def _enqueue(self, key: str, action: str, window_id: str, actor: str) -> dict:
        fence = self.fences.next_value(key)
        return self.outbox.enqueue(
            {
                "key": key,
                "action": action,
                "window_id": window_id,
                "fence": fence,
                "at": self._clock.now(),
                "actor": actor,
            }
        )

    def _record(self, kind: str, station_id: str, *, actor: str = "", **detail) -> None:
        if self._audit is None:
            return
        clean = {key: value for key, value in detail.items() if key not in ("kind", "train_id", "station_id")}
        self._audit.record(kind, "", station_id, actor=actor, **clean)
        payload = {key: value for key, value in clean.items() if isinstance(value, (str, int, float, bool))}
        self._bus.publish(
            Event.of(
                "maintenance",
                MANAGER,
                kind,
                severity="notice",
                station_id=station_id,
                actor=actor,
                **payload,
            )
        )

class _Commit:
    """提交区间：跨进程锁 + 崩溃钩子。

    钩子在持锁、落任何文档之前触发（``before_commit``），抛异常时整个申请不留痕；
    在事件已追加、当前态尚未写完时触发（``after_event``）模拟掉电，重放恢复由
    测试验证。
    """

    def __init__(self, service: MaintenanceService, station_id: str) -> None:
        self._service = service
        self._lock = FileLock(service._store.root)

    def __enter__(self):
        self._service._commit_lock.acquire()
        try:
            self._lock.__enter__()
            self._service._crash_hook("before_commit")
        except BaseException:
            # 钩子或加锁失败：释放已持有的两把锁，避免文件句柄/线程锁泄漏。
            if self._lock._handle is not None:
                self._lock.__exit__(None, None, None)
            self._service._commit_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        # after_commit 钩子在释放锁之前触发：它若模拟掉电抛异常，也要保证锁文件
        # 句柄正常关闭。
        if exc_type is None:
            try:
                self._service._crash_hook("after_commit")
            except BaseException:
                self._lock.__exit__(None, None, None)
                self._service._commit_lock.release()
                raise
        self._lock.__exit__(exc_type, exc, tb)
        self._service._commit_lock.release()
        return False
