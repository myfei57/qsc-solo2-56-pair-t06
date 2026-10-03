"""共享占用：排班、门控、锁定、审计读同一份占用。

这是整个子系统唯一的占用事实来源——所有组件不自己缓存"这樘门现在归谁"，而是
拿当前窗口集合 + 时刻现算：

* 门控自动开门前调 :func:`occupied_gates`，被占的门整樘跳过；
* 联锁锁定看 :class:`~psdline.window.service.WindowService` 暴露出的有效占用；
* 审计把占用快照原样记进流水，事后复核与现场判定基于同一组区间。

区间判定全部用半开区间 ``[start, end)``：一樘门在 ``end`` 那一刻即视为释放，
接车与检修首尾相接不算冲突（运营时刻表本来就是按这个约定排的）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Interval:
    """半开时间区间 ``[start, end)``，单位 Unix 秒。"""

    start: float
    end: float

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("区间止点必须晚于起点")

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    def overlaps(self, other: "Interval") -> bool:
        return overlaps(self, other)

    def contains(self, moment: float) -> bool:
        return self.start <= moment < self.end

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "duration_s": self.duration_s}


def overlaps(left: Interval, right: Interval) -> bool:
    """半开区间相交判定：首尾相接（``left.end == right.start``）不相交。"""

    return left.start < right.end and right.start < left.end


@dataclass(frozen=True)
class Occupancy:
    """一樘门在一个区间上的有效占用（窗口在某个时刻投下的影子）。"""

    gate_id: str
    interval: Interval
    window_id: str
    station_id: str
    state: str

    def as_dict(self) -> dict:
        return {
            "gate_id": self.gate_id,
            "station_id": self.station_id,
            "window_id": self.window_id,
            "state": self.state,
            **self.interval.as_dict(),
        }


def occupied_gates(windows: list, moment: float) -> dict[str, Occupancy]:
    """返回某时刻被占用的门 → 占用记录。

    入参是任意带 ``status`` / ``start_at`` / ``end_at`` / ``gate_ids`` 的窗口对象
    列表（:class:`~psdline.window.window.MaintenanceWindow`）。``approved`` 在到点
    前也算占用（它是已承诺占用，只是门还没锁），``active`` 是正在锁门的占用；
    ``expired_unconfirmed`` 已交还门体，不再占用时刻，但门被销账闸住。
    """

    from .window import APPROVED, ACTIVE

    from .window import APPROVED, ACTIVE, EXPIRED_UNCONFIRMED

    held: dict[str, Occupancy] = {}
    for window in windows:
        if not window.is_live or window.status == EXPIRED_UNCONFIRMED:
            continue
        if not window.covers(moment):
            continue
        state = ACTIVE if window.is_active else APPROVED
        interval = Interval(window.start_at, window.end_at)
        for gate_id in window.gate_ids:
            held.setdefault(
                gate_id,
                Occupancy(
                    gate_id=gate_id,
                    interval=interval,
                    window_id=window.id,
                    station_id=window.station_id,
                    state=state,
                ),
            )
    return held


def intervals_for_gate(windows: list, gate_id: str) -> list[Interval]:
    """返回一樘门在所有当前窗口上的已承诺占用区间（排序）。"""

    intervals = [
        Interval(window.start_at, window.end_at)
        for window in windows
        if window.is_live and window.status != "expired_unconfirmed"
        and gate_id in window.gate_ids
    ]
    intervals.sort(key=lambda item: item.start)
    return intervals


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """合并相交或首尾相接的区间，输出排序后的不相交区间。"""

    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda item: item.start)
    merged: list[Interval] = [ordered[0]]
    for interval in ordered[1:]:
        last = merged[-1]
        if interval.start <= last.end:
            merged[-1] = Interval(last.start, max(last.end, interval.end))
        else:
            merged.append(interval)
    return merged


def complement_intervals(
    wanted: Interval, busy: list[Interval]
) -> list[Interval]:
    """求 ``wanted`` 减去 ``busy``（合并后）的空闲区间。"""

    free: list[Interval] = []
    cursor = wanted.start
    for blocked in merge_intervals(busy):
        if blocked.end <= wanted.start:
            continue
        if blocked.start >= wanted.end:
            break
        if blocked.start > cursor:
            free.append(Interval(cursor, min(blocked.start, wanted.end)))
        cursor = max(cursor, blocked.end)
        if cursor >= wanted.end:
            break
    if cursor < wanted.end:
        free.append(Interval(cursor, wanted.end))
    return free
