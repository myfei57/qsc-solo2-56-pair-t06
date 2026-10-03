"""检修窗口：冲突检测与让路方案的纯逻辑测试。"""

from __future__ import annotations

from psdline.window import (
    APPROVED,
    ConflictFinder,
    Interval,
    MaintenanceWindow,
    SERVICE_FIRST,
    TrainVisit,
    YieldPlanner,
    overlaps,
)
from psdline.window.timetable import Timetable


def _window(window_id, gates, start, end, status=APPROVED, station="ST1"):
    return MaintenanceWindow(
        id=window_id,
        station_id=station,
        gate_ids=list(gates),
        start_at=start,
        end_at=end,
        requested_by="crew",
        reason="t",
        status=status,
        created_at=0.0,
    )


def test_half_open_interval_overlap():
    assert overlaps(Interval(0, 10), Interval(10, 20)) is False
    assert overlaps(Interval(0, 10), Interval(5, 15)) is True
    assert Interval(0, 10).contains(10) is False
    assert Interval(0, 10).contains(0) is True


def test_window_conflict_is_hard_and_only_for_shared_gate():
    finder = ConflictFinder()
    live = [_window("w1", ["g1", "g2"], 0, 10)]
    same_gate = finder.find(["g1"], Interval(5, 15), live)
    other_gate = finder.find(["g3"], Interval(5, 15), live)
    assert same_gate.hard is True
    assert same_gate.window_conflicts[0].window_id == "w1"
    assert other_gate.hard is False


def test_active_window_is_never_forceable_approved_is():
    finder = ConflictFinder()
    live = [
        _window("approved", ["g1"], 100, 200, status=APPROVED),
        _window("active", ["g2"], 100, 200, status="active"),
    ]
    report_approved = finder.find(["g1"], Interval(150, 180), live, allow_force=True)
    report_active = finder.find(["g2"], Interval(150, 180), live, allow_force=True)
    assert report_approved.forceable is True
    assert report_active.forceable is False


def test_schedule_conflicts_only_when_visit_touches_gate():
    finder = ConflictFinder()
    timetable = Timetable(
        station_id="ST1",
        revision=1,
        visits=[TrainVisit("T1", "ST1", 100, 160, gate_ids=("g1",))],
    )
    hit = finder.find(["g1"], Interval(0, 200), [], {"ST1": timetable})
    miss_gate = finder.find(["g2"], Interval(0, 200), [], {"ST1": timetable})
    miss_time = finder.find(["g1"], Interval(200, 300), [], {"ST1": timetable})
    assert len(hit.schedule_conflicts) == 1
    assert miss_gate.schedule_conflicts == []
    assert miss_time.schedule_conflicts == []


def test_blanket_visit_covers_every_gate():
    finder = ConflictFinder()
    timetable = Timetable(
        station_id="ST1", revision=1, visits=[TrainVisit("T1", "ST1", 0, 10)]
    )
    assert finder.find(["anything"], Interval(0, 10), [], {"ST1": timetable}).schedule_conflicts


def test_yield_planner_shift_avoids_both_windows_and_visits():
    planner = YieldPlanner(search_horizon_s=2000)
    # 原申请 [100,200) 与既有窗口 [150,250) 冲突；[400,460) 还有一班接车。
    live = [_window("w1", ["g1"], 150, 250)]
    timetable = Timetable(
        station_id="ST1",
        revision=1,
        visits=[TrainVisit("T1", "ST1", 400, 460, gate_ids=("g1",))],
    )
    proposal = planner.plan(
        ["g1"], Interval(100, 200), live, ["g1", "g2"], timetable, SERVICE_FIRST
    )
    shift = next(option for option in proposal.options if option.kind == "shift")
    shifted = Interval(shift.start_at, shift.end_at)
    assert not shifted.overlaps(Interval(150, 250))
    assert not shifted.overlaps(Interval(400, 460))
    assert shift.end_at - shift.start_at == 100


def test_yield_planner_relocates_to_free_gate():
    planner = YieldPlanner(search_horizon_s=100)
    live = [_window("w1", ["g1"], 0, 100)]
    proposal = planner.plan(
        ["g1"], Interval(0, 50), live, ["g1", "g2", "g3"], None, SERVICE_FIRST
    )
    relocation = next(option for option in proposal.options if option.kind == "relocate")
    assert "g1" not in relocation.gate_ids
    assert relocation.start_at == 0 and relocation.end_at == 50


def test_service_first_planner_never_displaces_schedule():
    planner = YieldPlanner()
    timetable = Timetable(
        station_id="ST1", revision=1, visits=[TrainVisit("T1", "ST1", 0, 10)]
    )
    proposal = planner.plan(
        ["g1"], Interval(0, 100), [], ["g1"], timetable, SERVICE_FIRST
    )
    assert all(option.kind != "displace_schedule" for option in proposal.options)


def test_cross_day_window_is_detected():
    window = _window("w1", ["g1"], 86_399.0, 90_000.0)
    assert window.spans_midnight() is True
    same_day = _window("w2", ["g1"], 100.0, 200.0)
    assert same_day.spans_midnight() is False
