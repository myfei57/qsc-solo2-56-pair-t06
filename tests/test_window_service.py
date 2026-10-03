"""检修窗口：平台级生命周期、门控联动、并发、回滚与哈希链。"""

from __future__ import annotations

import json
import os

import pytest

from psdline.door import open_group
from psdline.errors import InterlockError, RailError
from psdline.window import MAINTENANCE_FIRST


def _gate(platform, station_id, index=0):
    return platform.gates.by_station(station_id)[index].id


def _free_slot(platform, station_id, gate_id, duration=600, after_index=8):
    """在播种的末班接车之后取一段确定无排班的区间（半开区间，首尾相接合法）。"""

    now = platform.clock.now()
    timetable = platform.timetables.latest(station_id)
    last_depart = max(
        visit.depart_at for visit in timetable.visits if visit.affects(gate_id)
    )
    # 每个用例错开 30 分钟，避免不同用例落进彼此的窗口。
    start = last_depart + 1 + after_index * 1800
    return start, start + duration


def test_apply_conflict_service_first_returns_yield_plan(platform):
    platform.start()
    gate = _gate(platform, "ST1")
    now = platform.clock.now()
    result = platform.apply_window("ST1", [gate], now + 600, now + 700, "c", "x")
    assert result["ok"] is False
    assert result["yield_plan"] is not None
    assert platform.windows.windows() == []


def test_apply_then_activate_locks_and_blocks_auto_open(platform):
    platform.start()
    gate = _gate(platform, "ST1")
    start, end = _free_slot(platform, "ST1", gate, after_index=0)
    result = platform.apply_window("ST1", [gate], start, end, "c", "x")
    assert result["ok"] is True
    platform.clock.advance(start - platform.clock.now())
    tick = platform.window_tick()
    assert tick["activated"]
    assert platform.lockouts.is_locked(gate) is True
    assert platform.windows.is_gate_held(gate) is True
    batch = platform.batch_open("ST1")
    group = platform.gates.require(gate).group
    assert batch["skipped"][group] == "maintenance_occupied"
    with pytest.raises(InterlockError):
        open_group(platform.doors, platform.mappings, "ST1", group)


def test_expiry_autoreleases_then_confirm_reopens(platform):
    platform.start()
    gate = _gate(platform, "ST1")
    start, end = _free_slot(platform, "ST1", gate, after_index=1)
    window_id = platform.apply_window("ST1", [gate], start, end, "c", "x")["window"]["id"]
    platform.clock.advance(end - platform.clock.now())
    tick = platform.window_tick()
    assert tick["expired"]
    assert platform.lockouts.is_locked(gate) is False
    assert platform.windows.is_gate_held(gate) is False
    with pytest.raises(RailError):
        platform.apply_window("ST1", [gate], end + 100, end + 200, "c2", "y")
    assert platform.confirm_window(window_id, "dispatcher")["ok"] is True
    again = platform.apply_window("ST1", [gate], end + 100, end + 200, "c2", "y")
    assert again["ok"] is True


def test_maintenance_first_displaces_schedule(platform):
    platform.start()
    platform.set_station_window_policy("ST1", MAINTENANCE_FIRST)
    gate = _gate(platform, "ST1")
    now = platform.clock.now()
    before = platform.timetables.latest("ST1").revision
    result = platform.apply_window("ST1", [gate], now + 600, now + 700, "c", "x")
    assert result["ok"] is True
    assert result["schedule_displacements"]
    assert platform.timetables.latest("ST1").revision == before + 1
    moved = platform.timetables.latest("ST1").visits[0]
    assert moved.arrive_at >= now + 700


def test_concurrent_applications_only_one_wins(platform):
    platform.start()
    gate = _gate(platform, "ST1", index=2)
    start, end = _free_slot(platform, "ST1", gate, after_index=2)
    first = platform.apply_window("ST1", [gate], start, end, "A", "a")
    second = platform.apply_window("ST1", [gate], start, end, "B", "b")
    assert first["ok"] is True
    assert second["ok"] is False
    assert second["yield_plan"]["feasible"] is True
    assert len(platform.windows.windows(status="approved")) == 1


def test_force_cancels_approved_but_not_active(platform):
    platform.start()
    platform.set_station_window_policy("ST1", "service_first", allow_force=True)
    gate = _gate(platform, "ST1", index=3)
    start, end = _free_slot(platform, "ST1", gate, after_index=3)
    victim = platform.apply_window("ST1", [gate], start, end, "A", "a")
    forced = platform.apply_window(
        "ST1", [gate], start + 10, end - 10, "B", "b", force=True
    )
    assert forced["ok"] is True
    assert forced["forced_windows"] == [victim["window"]["id"]]
    assert victim["window"]["id"] in [item.id for item in platform.windows.history()]


def test_partial_failure_rolls_back_everything(platform):
    platform.start()
    platform.set_station_window_policy("ST1", "service_first", allow_force=True)
    gate = _gate(platform, "ST1", index=3)
    start, end = _free_slot(platform, "ST1", gate, after_index=4)
    victim = platform.apply_window("ST1", [gate], start, end, "A", "a")
    platform.windows.fault_on_commit = True
    with pytest.raises(RuntimeError):
        platform.apply_window("ST1", [gate], start + 10, end - 10, "B", "b", force=True)
    untouched = platform.windows.require(victim["window"]["id"])
    assert untouched.status == "approved"
    assert platform.windows.verify()["consistent"] is True


def test_extension_conflicts_are_checked(platform):
    platform.start()
    gate = _gate(platform, "ST1", index=1)
    start, end = _free_slot(platform, "ST1", gate, duration=300, after_index=5)
    window_id = platform.apply_window("ST1", [gate], start, end, "A", "a")["window"]["id"]
    other = platform.apply_window("ST1", [gate], end + 600, end + 1200, "B", "b")
    assert other["ok"] is True
    result = platform.extend_window(window_id, end + 900, "A")
    assert result["ok"] is False
    assert platform.windows.require(window_id).end_at == end


def test_terminal_windows_only_exist_in_history(platform):
    platform.start()
    gate = _gate(platform, "ST1", index=2)
    start, end = _free_slot(platform, "ST1", gate, after_index=6)
    window_id = platform.apply_window("ST1", [gate], start, end, "A", "a")["window"]["id"]
    assert platform.cancel_window(window_id, "A")["ok"] is True
    assert platform.windows.get(window_id) is None
    assert window_id in [item.id for item in platform.windows.history()]


def test_history_tampering_is_detected(platform):
    platform.start()
    gate = _gate(platform, "ST1", index=2)
    start, end = _free_slot(platform, "ST1", gate, after_index=7)
    window_id = platform.apply_window("ST1", [gate], start, end, "A", "a")["window"]["id"]
    platform.cancel_window(window_id, "A")
    segment = platform.window_store.journal.segments()[0]
    with open(segment, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    record = json.loads(lines[0])
    record["at"] += 1
    lines[0] = json.dumps(record, ensure_ascii=False) + "\n"
    with open(segment, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    with pytest.raises(RailError):
        platform.window_store.verify_chain()


def test_replay_rebuilds_state_after_restart(platform, tmp_path):
    platform.start()
    gate = _gate(platform, "ST1")
    start, end = _free_slot(platform, "ST1", gate, after_index=8)
    window_id = platform.apply_window("ST1", [gate], start, end, "A", "a")["window"]["id"]

    from psdline.app import build_platform
    from psdline.config import default_config

    rebuilt = build_platform(
        default_config(), clock=platform.clock, storage_root=platform.store.root
    )
    assert rebuilt.windows.require(window_id).status == "approved"
    assert rebuilt.windows.verify()["consistent"] is True


def test_unknown_gate_and_bad_interval_rejected(platform):
    platform.start()
    now = platform.clock.now()
    with pytest.raises(RailError):
        platform.apply_window("ST1", ["NOPE-1"], now + 100, now + 200, "c", "x")
    with pytest.raises(RailError):
        platform.apply_window("ST1", [_gate(platform, "ST1")], now + 200, now + 100, "c", "x")
    assert os.path.isdir(platform.store.root)
