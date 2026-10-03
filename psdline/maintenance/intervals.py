"""占用区间的纯函数运算。

所有占用都表示成闭开区间 ``[start, end)``（Unix 时间戳秒）：起点算占用、终点
不算，因此一个窗口 10:00 结束与下一个 10:00 开始的窗口不算冲突，跨午夜的窗口
也只是一个跨零点的普通区间，不需要任何特例。

这一层不碰存储与时钟，只做区间合并、空闲切片、让路候选计算与运营日切片，保证
冲突规则可以在验证环境里直接断言。
"""

from __future__ import annotations

from dataclasses import dataclass

SECONDS_PER_DAY = 86_400
DEFAULT_BUFFER_S = 300


def overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    """两个闭开区间是否相交。"""

    return a_start < b_end and b_start < a_end


def merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """把相交或相邻的区间合并成互不相交的升序区间序列。"""

    ordered = sorted((float(start), float(end)) for start, end in spans if start < end)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def free_gaps(
    horizon_start: float,
    horizon_end: float,
    busy: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """在 ``[horizon_start, horizon_end)`` 内扣掉忙区间，返回空闲区间序列。"""

    if horizon_end <= horizon_start:
        return []
    gaps: list[tuple[float, float]] = []
    cursor = float(horizon_start)
    for busy_start, busy_end in merge_spans(busy):
        busy_start = max(busy_start, cursor)
        if busy_start >= horizon_end:
            break
        if busy_start > cursor:
            gaps.append((cursor, busy_start))
        cursor = max(cursor, busy_end)
        if cursor >= horizon_end:
            break
    if cursor < horizon_end:
        gaps.append((cursor, horizon_end))
    return gaps


def fits(gap: tuple[float, float], duration: float) -> bool:
    """某空闲区间能否容下给定时长（允许占满整个区间）。"""

    return gap[1] - gap[0] >= duration


@dataclass(frozen=True)
class DaySlice:
    """窗口落在某一个自然日上的片段。"""

    date: str
    start: float
    end: float

    def as_dict(self) -> dict:
        return {"date": self.date, "start": self.start, "end": self.end}


def slice_by_day(
    start: float,
    end: float,
    *,
    day_boundary_offset_s: int = 0,
    tz_offset_s: int = 0,
) -> list[DaySlice]:
    """把一个区间按运营日切成若干片段。

    ``tz_offset_s`` 是本地时区相对 UTC 的固定偏移；``day_boundary_offset_s`` 是
    运营日分界点相对本地零点的偏移（例如凌晨 4 点收班则传 4*3600）。运营日的
    归属按"本地时间减去分界点偏移后落在哪一天"计算，因此 02:30 的作业在 4 点
    分界的线路上属于前一天运营日——这正是排班口径，而不是简单按日历零点切。
    """

    if end <= start:
        return []
    boundary = int(tz_offset_s) + int(day_boundary_offset_s)

    def day_index(at: float) -> int:
        return int((at - boundary) // SECONDS_PER_DAY)

    def day_label(index: int) -> str:
        # 第 index 个运营日覆盖 [boundary + index*D, boundary + (index+1)*D)，
        # 标签取该区间内本地零点（= boundary + index*D - day_boundary_offset_s）。
        local_midnight = index * SECONDS_PER_DAY + int(tz_offset_s)
        return _ymd(local_midnight)

    def next_boundary(index: int) -> float:
        return float(boundary + (index + 1) * SECONDS_PER_DAY)

    slices: list[DaySlice] = []
    cursor = float(start)
    index = day_index(cursor)
    while cursor < end:
        boundary_at = next_boundary(index)
        piece_end = min(float(end), boundary_at)
        slices.append(DaySlice(date=day_label(index), start=cursor, end=piece_end))
        cursor = piece_end
        index += 1
    return slices


def _ymd(at: float) -> str:
    """把 Unix 时间戳格式化成 UTC 口径的 ``YYYY-MM-DD``（标签仅用于展示）。"""

    import time

    return time.strftime("%Y-%m-%d", time.gmtime(at))
