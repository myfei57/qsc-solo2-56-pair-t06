"""检修窗口系统的单元验证（标准库 unittest，无外部依赖）。

覆盖：冲突检测与让路方案、按站策略差异、物理占用硬冲突、门控集成、到期人工
确认、并发申请、部分失败回滚、outbox 重试与对账、fence 防旧指令、跨天窗口、
历史不可变（终态冻结 + 哈希链）、幂等提交与崩溃恢复。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from psdline.app import build_platform  # noqa: E402
from psdline.clock import ManualClock  # noqa: E402
from psdline.config import default_config  # noqa: E402
from psdline.errors import RailError  # noqa: E402
from psdline.eventbus import EventBus  # noqa: E402
from psdline.audit import AuditLog  # noqa: E402
from psdline.maintenance.intervals import merge_spans, overlaps, slice_by_day  # noqa: E402
from psdline.maintenance import (  # noqa: E402
    ACTIVE,
    APPROVED,
    APPLICANT,
    CANCELLED,
    DISPATCHER,
    DoorInhibitorDevice,
    EMERGENCY,
    EXPIRED,
    MAINTENANCE_FIRST,
    PREEMPTED,
    RELEASED,
    REJECTED,
    REQUESTED,
    SERVICE_FIRST,
    MaintenanceService,
    SUPERVISOR,
)
from psdline.maintenance.ledger import canonical  # noqa: E402
from psdline.maintenance.repos import FAILED, SENT  # noqa: E402
from psdline.maintenance.timetable import derive_slots  # noqa: E402
from psdline.store import JsonStore, JsonlJournal  # noqa: E402

T0 = 1_758_000_000.0  # 固定起点（2025-09-16 前后），便于手算。


def make_service(root: str, clock: ManualClock | None = None, **kwargs):
    store = JsonStore(root)
    clock = clock or ManualClock(T0)
    audit = AuditLog(clock, EventBus(), JsonlJournal(root, "audit"))
    groups = kwargs.pop("groups", ["G01", "G02", "G03", "G04"])
    service = MaintenanceService(
        store,
        clock,
        EventBus(),
        audit,
        headways_provider=lambda station_id: [180.0],
        group_provider=lambda station_id: list(groups),
        **kwargs,
    )
    return service, clock, audit


class IntervalTests(unittest.TestCase):
    def test_half_open_adjacent_windows_do_not_overlap(self) -> None:
        self.assertTrue(overlaps(0, 10, 5, 15))
        self.assertFalse(overlaps(0, 10, 10, 20))

    def test_merge_spans(self) -> None:
        self.assertEqual(merge_spans([(0, 5), (4, 10), (12, 14)]), [(0.0, 10.0), (12.0, 14.0)])

    def test_slice_crosses_midnight(self) -> None:
        # UTC 22:00 到次日 02:00，跨两个自然日。
        slices = slice_by_day(22 * 3600, 26 * 3600)
        self.assertEqual([item.date for item in slices], ["1970-01-01", "1970-01-02"])
        self.assertEqual(slices[0].end, 24 * 3600)
        self.assertEqual(slices[1].start, 24 * 3600)

    def test_service_day_boundary_4am(self) -> None:
        # 1 月 2 日凌晨 2:30 的作业在 4 点分界口径下属于 1 月 1 日运营日。
        at = 26.5 * 3600
        slices = slice_by_day(at, at + 1800, day_boundary_offset_s=4 * 3600)
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0].date, "1970-01-01")

    def test_service_day_after_4am_belongs_to_new_day(self) -> None:
        # 1 月 2 日凌晨 4:30 已经跨过 4 点分界，属于 1 月 2 日运营日。
        at = 28.5 * 3600
        slices = slice_by_day(at, at + 1800, day_boundary_offset_s=4 * 3600)
        self.assertEqual(slices[0].date, "1970-01-02")


class TimetableTests(unittest.TestCase):
    def test_derive_slots_skips_out_of_service_window(self) -> None:
        # 视野覆盖凌晨收班时段，推演车次不应落在 0-5 点。
        slots = derive_slots("ST1", [180.0], 0.0, 24 * 3600.0, dwell_s=60.0)
        self.assertTrue(slots)
        for slot in slots:
            hour = int((slot.arrive % 86400) // 3600)
            self.assertGreaterEqual(hour, 5)


class ConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service, self.clock, _ = make_service(self.temp.name)
        # 10:00-10:05 有一趟计划车（缓冲 5 分钟）。
        self.service.register_service("ST1", "TR-1", T0 + 3600, T0 + 3690)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_service_first_blocks_window_on_service_conflict(self) -> None:
        report = self.service.analyze("ST1", "G01", T0 + 3500, T0 + 3800)
        self.assertEqual(report["precedence"], SERVICE_FIRST)
        self.assertTrue(report["hard_conflicts"])
        self.assertFalse(report["approvable"])
        kinds = {item["kind"] for item in report["proposals"]}
        # 撞车时给出的方案必须是"让车"的方案，不能建议调列车。
        self.assertTrue(kinds)
        self.assertNotIn("调车", kinds)

    def test_proposals_are_feasible(self) -> None:
        # 窗口 09:55-10:20 撞 10:00 的车；所有 SHIFT 方案必须真的不撞。
        report = self.service.analyze("ST1", "G01", T0 + 3300, T0 + 4800)
        self.assertTrue(report["proposals"])
        for proposal in report["proposals"]:
            if proposal["kind"] in ("SHIFT_AFTER", "SHIFT_BEFORE", "TRIM"):
                for start, end in proposal["windows"]:
                    check = self.service.analyze("ST1", "G01", start, end)
                    self.assertFalse(check["hard_conflicts"], proposal)

    def test_buffer_zone_also_conflicts(self) -> None:
        # 列车 10:00 到达，缓冲 5 分钟：窗口 09:56-09:59 不撞核心停站，
        # 但落在接车前的避让余量内，仍然算硬冲突。
        report = self.service.analyze("ST1", "G01", T0 + 3360, T0 + 3540)
        self.assertTrue(report["hard_conflicts"])
        self.assertIn("避让余量", report["hard_conflicts"][0]["detail"])

    def test_clean_window_approvable(self) -> None:
        report = self.service.analyze("ST1", "G01", T0 + 4000, T0 + 5000)
        self.assertTrue(report["approvable"])
        self.assertFalse(report["hard_conflicts"])

    def test_maintenance_first_makes_service_conflict_soft(self) -> None:
        self.service.set_policy("ST1", precedence=MAINTENANCE_FIRST)
        report = self.service.analyze("ST1", "G01", T0 + 3500, T0 + 3800)
        self.assertFalse(report["hard_conflicts"])
        self.assertTrue(report["soft_conflicts"])
        self.assertTrue(report["approvable"])
        affected = {
            tuple(item["affected_trains"])
            for item in report["proposals"]
            if item["affected_trains"]
        }
        self.assertIn(("TR-1",), affected)

    def test_physical_occupancy_is_hard_in_both_policies(self) -> None:
        first = self.service.request_window("ST1", "G01", T0 + 10_000, T0 + 11_000, "crew-A")
        self.service.approve_window(first["window_id"], "dispatcher")
        for precedence in (SERVICE_FIRST, MAINTENANCE_FIRST):
            self.service.set_policy("ST1", precedence=precedence)
            report = self.service.analyze("ST1", "G01", T0 + 10_200, T0 + 10_800)
            self.assertTrue(
                any(item["kind"] == "occupancy" for item in report["hard_conflicts"]),
                precedence,
            )
            self.assertFalse(report["approvable"])

    def test_pending_request_is_soft_hint_only(self) -> None:
        self.service.request_window("ST1", "G01", T0 + 10_000, T0 + 11_000, "crew-A")
        report = self.service.analyze("ST1", "G01", T0 + 10_200, T0 + 10_800)
        self.assertFalse(report["hard_conflicts"])
        self.assertTrue(any(item["kind"] == "pending" for item in report["soft_conflicts"]))

    def test_alt_zone_proposal(self) -> None:
        first = self.service.request_window("ST1", "G01", T0 + 10_000, T0 + 20_000, "crew-A")
        self.service.approve_window(first["window_id"], "dispatcher")
        report = self.service.analyze("ST1", "G01", T0 + 10_200, T0 + 10_800)
        alt = [item for item in report["proposals"] if item["kind"] == "ALT_ZONE"]
        self.assertTrue(alt)
        self.assertIn(alt[0]["group_id"], {"G02", "G03", "G04"})


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service, self.clock, _ = make_service(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _approved(self, start: float, end: float, group: str = "G01", kind: str = "routine"):
        result = self.service.request_window("ST1", group, start, end, "crew", kind=kind)
        window_id = result["window_id"]
        self.service.approve_window(window_id, "dispatcher")
        return window_id

    def test_full_lifecycle_inhibit_then_restore(self) -> None:
        window_id = self._approved(T0 + 1000, T0 + 2000)
        self.assertTrue(self.service.group_inhibited("ST1", "G01")["inhibited"])
        self.service.start_window(window_id)
        self.assertEqual(self.service.get_window(window_id).status, ACTIVE)
        self.service.finish_window(window_id, "crew")
        self.assertFalse(self.service.group_inhibited("ST1", "G01")["inhibited"])
        actions = [item["action"] for item in self.service.device.delivered]
        self.assertEqual(actions, ["inhibit", "restore"])

    def test_expiry_keeps_door_inhibited_and_requires_manual_release(self) -> None:
        window_id = self._approved(T0 + 1000, T0 + 2000)
        self.clock.advance(1000)
        self.service.tick()  # 到点开工
        self.clock.advance(1000)
        tick = self.service.tick()  # 到期
        self.assertEqual(tick["expired"], [window_id])
        self.assertEqual(self.service.get_window(window_id).status, EXPIRED)
        # 到期不自动释放：门仍然禁用。
        self.assertTrue(self.service.group_inhibited("ST1", "G01")["inhibited"])
        # 普通完工在到期后被拒绝。
        with self.assertRaises(RailError):
            self.service.finish_window(window_id, "crew")
        # 主管必须重复窗口标识才能强制收尾。
        with self.assertRaises(RailError):
            self.service.force_finish_window(window_id, "chief", role=SUPERVISOR, confirm="nope")
        self.service.force_finish_window(window_id, "chief", role=SUPERVISOR, confirm=window_id)
        self.assertFalse(self.service.group_inhibited("ST1", "G01")["inhibited"])

    def test_grace_period_escalates_but_never_auto_releases(self) -> None:
        window_id = self._approved(T0 + 1000, T0 + 2000)
        self.clock.advance(2000 + 901)
        tick = self.service.tick()
        self.assertEqual(tick["expired"], [window_id])
        self.assertEqual(tick["escalated"], [window_id])
        self.assertEqual(self.service.get_window(window_id).status, EXPIRED)
        self.assertTrue(self.service.group_inhibited("ST1", "G01")["inhibited"])

    def test_cancel_approved_window_restores_door(self) -> None:
        window_id = self._approved(T0 + 5000, T0 + 6000)
        self.assertTrue(self.service.group_inhibited("ST1", "G01")["inhibited"])
        self.service.cancel_window(window_id, "dispatcher")
        self.assertFalse(self.service.group_inhibited("ST1", "G01")["inhibited"])

    def test_cancel_active_window_rejected(self) -> None:
        window_id = self._approved(T0 + 1000, T0 + 2000)
        self.clock.advance(1000)
        self.service.tick()
        with self.assertRaises(RailError):
            self.service.cancel_window(window_id, "crew")

    def test_illegal_transition_rejected(self) -> None:
        result = self.service.request_window("ST1", "G01", T0 + 1000, T0 + 2000, "crew")
        window_id = result["window_id"]
        with self.assertRaises(RailError):
            self.service.finish_window(window_id, "crew")

    def test_extend_rechecks_conflicts(self) -> None:
        window_id = self._approved(T0 + 1000, T0 + 2000)
        self.service.register_service("ST1", "TR-9", T0 + 2500, T0 + 2560)
        with self.assertRaises(RailError):
            self.service.extend_window(window_id, "dispatcher", T0 + 3000)


class EmergencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service, self.clock, _ = make_service(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_emergency_preempts_approved_routine_window(self) -> None:
        routine = self.service.request_window("ST1", "G01", T0 + 1000, T0 + 5000, "crew")
        self.service.approve_window(routine["window_id"], "dispatcher")
        emergency = self.service.request_window(
            "ST1", "G01", T0 + 2000, T0 + 3000, "rescue", kind=EMERGENCY
        )
        report = self.service.analyze(
            "ST1", "G01", T0 + 2000, T0 + 3000, kind=EMERGENCY
        )
        self.assertTrue(report["approvable"])
        self.assertTrue(report["requires_override"])
        result = self.service.approve_window(
            emergency["window_id"], "chief", role=SUPERVISOR, force=True
        )
        self.assertEqual(result["preempted"], [routine["window_id"]])
        self.assertEqual(self.service.get_window(routine["window_id"]).status, PREEMPTED)

    def test_emergency_cannot_preempt_active_window(self) -> None:
        routine = self.service.request_window("ST1", "G01", T0 + 1000, T0 + 5000, "crew")
        self.service.approve_window(routine["window_id"], "dispatcher")
        self.clock.advance(1000)
        self.service.tick()
        report = self.service.analyze("ST1", "G01", T0 + 2000, T0 + 3000, kind=EMERGENCY)
        self.assertFalse(report["approvable"])
        with self.assertRaises(RailError):
            self.service.approve_window(
                routine["window_id"], "chief", role=SUPERVISOR, force=True
            )

    def test_force_requires_supervisor(self) -> None:
        result = self.service.request_window(
            "ST1", "G01", T0 + 2000, T0 + 3000, "rescue", kind=EMERGENCY
        )
        self.service.register_service("ST1", "TR-1", T0 + 2500, T0 + 2560)
        with self.assertRaises(RailError):
            self.service.approve_window(result["window_id"], "dispatcher", force=True)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service, self.clock, _ = make_service(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_concurrent_approve_only_one_wins(self) -> None:
        """两个重叠申请并发审批，只允许一个成功（串行化后做冲突重读）。"""

        ids = [
            self.service.request_window("ST1", "G01", T0 + 1000, T0 + 2000, f"crew-{i}")[
                "window_id"
            ]
            for i in range(2)
        ]
        outcomes: list[str] = []
        lock = threading.Lock()

        def approve(window_id: str) -> None:
            try:
                self.service.approve_window(window_id, "dispatcher")
                with lock:
                    outcomes.append("ok")
            except RailError:
                with lock:
                    outcomes.append("rejected")

        threads = [threading.Thread(target=approve, args=(wid,)) for wid in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["ok", "rejected"])
        approved = [item for item in self.service.list_windows() if item.status == APPROVED]
        self.assertEqual(len(approved), 1)

    def test_idempotent_request_returns_same_window(self) -> None:
        kwargs = dict(
            station_id="ST1",
            group_id="G01",
            start=T0 + 1000,
            end=T0 + 2000,
            applicant="crew",
            idem_key="ticket-123",
        )
        first = self.service.request_window(**kwargs)
        second = self.service.request_window(**kwargs)
        self.assertEqual(first["window_id"], second["window_id"])
        self.assertTrue(second.get("idempotent"))

    def test_cross_process_approve_serialized(self) -> None:
        """两个独立进程并发批同一门区的重叠窗口，flock 保证只成一个。"""

        import multiprocessing

        temp = self.temp.name
        ctx = multiprocessing.get_context("fork") if hasattr(multiprocessing, "get_context") else multiprocessing

        def worker(out_q, applicant: str) -> None:
            try:
                service, _, _ = make_service(temp)
                result = service.request_window(
                    "ST1", "G01", T0 + 1000, T0 + 2000, applicant
                )
                service.approve_window(result["window_id"], "dispatcher")
                out_q.put(("ok", result["window_id"]))
            except RailError as error:
                out_q.put(("rejected", error.message))

        queue: multiprocessing.Queue = ctx.Queue()
        procs = [
            ctx.Process(target=worker, args=(queue, f"crew-{i}"))
            for i in range(2)
        ]
        for proc in procs:
            proc.start()
        outcomes = [queue.get(timeout=20) for _ in procs]
        for proc in procs:
            proc.join(timeout=20)
        self.assertEqual(sorted(item[0] for item in outcomes), ["ok", "rejected"])


class RollbackTests(unittest.TestCase):
    def test_crash_before_commit_leaves_no_trace(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        def hook(phase: str) -> None:
            if phase == "before_commit":
                raise RuntimeError("simulated crash before commit")

        service, _, _ = make_service(temp.name, crash_hook=hook)
        with self.assertRaises(RuntimeError):
            service.request_window("ST1", "G01", T0 + 1000, T0 + 2000, "crew")
        self.assertEqual(service.list_windows(), [])
        self.assertEqual(service.ledger.count, 0)

        # 用同一存储重建服务：没有任何半成品，状态干净。
        service2, _, _ = make_service(temp.name)
        self.assertEqual(service2.list_windows(), [])
        self.assertEqual(service2.verify_history()["events"], 0)

    def test_crash_after_event_rebuilds_from_ledger(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        def hook(phase: str) -> None:
            if phase == "after_event":
                raise RuntimeError("simulated crash between event and current state")

        service, _, _ = make_service(temp.name, crash_hook=hook)
        with self.assertRaises(RuntimeError):
            service.request_window("ST1", "G01", T0 + 1000, T0 + 2000, "crew")
        # 事件已落盘、当前态文档还没写：重启后以流水为准补回，状态一致且无脏文档。
        service2, _, _ = make_service(temp.name)
        windows = service2.list_windows()
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].status, REQUESTED)
        self.assertTrue(service2.verify_history()["ok"])


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.device = DoorInhibitorDevice()
        self.service, self.clock, _ = make_service(self.temp.name, device=self.device)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _approved(self):
        result = self.service.request_window("ST1", "G01", T0 + 1000, T0 + 2000, "crew")
        return self.service.approve_window(result["window_id"], "dispatcher")

    def test_device_failure_retried_then_delivered(self) -> None:
        self.device.fail_next = 1
        self._approved()
        summary = self.service.outbox_summary()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["sent"], 0)
        # 下一轮投递重试成功。
        result = self.service.relay()
        self.assertTrue(result["sent"])
        self.assertEqual(self.service.outbox_summary()["failed"], 0)
        self.assertTrue(self.device.inhibited("ST1:G01"))

    def test_reconcile_compensates_missing_effect(self) -> None:
        self._approved()
        # 模拟设备侧状态丢失（重启）：重新对账后应再次 inhibit。
        self.device._state.clear()
        result = self.service.reconcile()
        self.assertTrue(result["compensated"])
        self.assertTrue(self.device.inhibited("ST1:G01"))

    def test_stale_restore_cannot_open_newer_occupancy(self) -> None:
        first = self.service.request_window("ST1", "G01", T0 + 1000, T0 + 2000, "crew-A")
        self.service.approve_window(first["window_id"], "dispatcher")
        self.service.finish_window(
            self._start(first["window_id"]), "crew-A"
        ) if False else None
        # 正常完工第一个窗口（先开工再完工）。
        self.clock.advance(1000)
        self.service.tick()
        self.service.finish_window(first["window_id"], "crew-A")
        # 第二个窗口立刻占用同一门区；fence 序号更高。
        second = self.service.request_window("ST1", "G01", T0 + 5000, T0 + 6000, "crew-B")
        self.service.approve_window(second["window_id"], "dispatcher")
        # 手工伪造一条"迟到的旧 restore"（fence=2，应被当前 inhibit fence=3 挡住）。
        commands = self.service.outbox.list()
        stale = dict(commands[1])  # 第一条 restore
        result = self.device.send(stale)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "stale_fence")
        self.assertTrue(self.device.inhibited("ST1:G01"))

    def _start(self, window_id: str) -> str:
        return window_id


class ImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service, self.clock, _ = make_service(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_terminal_window_document_is_frozen(self) -> None:
        result = self.service.request_window("ST1", "G01", T0 + 1000, T0 + 2000, "crew")
        self.service.reject_window(result["window_id"], "dispatcher")
        window = self.service.get_window(result["window_id"])
        self.assertEqual(window.status, REJECTED)
        with self.assertRaises(RailError):
            self.service.windows.save(window)
        with self.assertRaises(RailError):
            self.service.approve_window(result["window_id"], "dispatcher")

    def test_registry_blocks_illegal_status_rewrite(self) -> None:
        """绕过服务直接把非终态文档改成非法状态，也必须被存储层拦住。"""

        result = self.service.request_window("ST1", "G01", T0 + 1000, T0 + 2000, "crew")
        self.service.approve_window(result["window_id"], "dispatcher")
        self.clock.advance(1000)
        self.service.tick()
        self.clock.advance(1000)
        self.service.tick()
        window = self.service.get_window(result["window_id"])
        self.assertEqual(window.status, EXPIRED)
        # 合法的同状态字段更新（标记升级）允许。
        window.escalated = True
        self.service.windows.save(window)
        # 非法回退到 APPROVED 拒绝。
        window.status = APPROVED
        with self.assertRaises(RailError):
            self.service.windows.save(window)

    def test_hash_chain_detects_tampering(self) -> None:
        for index in range(3):
            result = self.service.request_window(
                "ST1", "G01", T0 + 10_000 * index, T0 + 10_000 * index + 1000, "crew"
            )
            self.service.reject_window(result["window_id"], "dispatcher")
        self.assertTrue(self.service.verify_history()["ok"])
        # 直接篡改流水文件（绕过服务），校验必须断链。
        journal_path = os.path.join(self.temp.name, "window-events-00000.jsonl")
        with open(journal_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        record = json.loads(lines[1])
        record["data"]["reason"] = "tampered"
        lines[1] = json.dumps(record, ensure_ascii=False) + "\n"
        with open(journal_path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        verification = self.service.verify_history()
        self.assertFalse(verification["ok"])
        self.assertIn(verification["reason"], {"hash_mismatch", "chain_broken"})


class PlatformIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clock = ManualClock(T0)
        self.platform = build_platform(
            default_config(), clock=self.clock, storage_root=os.path.join(self.temp.name, "state")
        )
        self.platform.start()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_arrival_blocked_while_zone_occupied(self) -> None:
        result = self.platform.request_maintenance(
            "ST1", "G01", T0 + 1000, T0 + 7200, "crew", actor=APPLICANT
        )
        self.platform.approve_maintenance(result["window_id"], "dispatcher")
        # 同一时刻到站开门：G01 是首门区，应被统一占用视图拦下。
        arrival = self.platform.arrive("T09", "ST1", 0)
        self.assertFalse(arrival["ok"])
        self.assertTrue(any("检修占用" in item for item in arrival["violations"]))

    def test_batch_open_skips_occupied_zone(self) -> None:
        result = self.platform.request_maintenance(
            "ST1", "G02", T0 + 1000, T0 + 7200, "crew"
        )
        self.platform.approve_maintenance(result["window_id"], "dispatcher")
        batch = self.platform.batch_open("ST1")
        self.assertEqual(batch["skipped"].get("G02"), "maintenance_inhibited")
        self.assertIn("G01", batch["opened"])

    def test_cycle_drives_window_state_and_occupancy_view(self) -> None:
        result = self.platform.request_maintenance("ST1", "G01", T0 + 60, T0 + 120, "crew")
        self.platform.approve_maintenance(result["window_id"], "dispatcher")
        occupancy = self.platform.maintenance_occupancy("ST1", T0, T0 + 3600)
        self.assertEqual(occupancy["occupied_groups"], ["G01"])
        self.clock.advance(60)
        cycle = self.platform.cycle()
        self.assertEqual(cycle["maintenance"]["started"], [result["window_id"]])
        self.clock.advance(60)
        cycle = self.platform.cycle()
        self.assertEqual(cycle["maintenance"]["expired"], [result["window_id"]])
        # 到期仍占门。
        self.assertTrue(
            self.platform.maintenance.group_inhibited("ST1", "G01")["inhibited"]
        )

    def test_cross_day_window_in_view(self) -> None:
        start = T0 - T0 % 86400 + 22 * 3600
        result = self.platform.request_maintenance("ST1", "G01", start, start + 4 * 3600, "crew")
        view = self.platform.maintenance_window_view(result["window_id"])
        self.assertEqual(len(view["service_days"]), 2)

    def test_per_station_policy_differs(self) -> None:
        self.platform.set_maintenance_policy("ST2", precedence=MAINTENANCE_FIRST)
        self.platform.register_service_slot("ST2", "TR-X", T0 + 1000, T0 + 1060)
        self.platform.register_service_slot("ST1", "TR-X", T0 + 1000, T0 + 1060)
        st1 = self.platform.analyze_maintenance("ST1", "G01", T0 + 900, T0 + 1200)
        st2 = self.platform.analyze_maintenance("ST2", "G01", T0 + 900, T0 + 1200)
        self.assertTrue(st1["hard_conflicts"])
        self.assertFalse(st2["hard_conflicts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
