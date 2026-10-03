"""冲突检测与让路方案。

冲突分两类：

* **排班冲突**：窗口（含接车余量）与计划/推演车次相交。在 ``service_first``
  策略下是硬冲突，在 ``maintenance_first`` 下是可由调度确认后放行的软冲突。
* **占用冲突**：同门区存在状态为 APPROVED/ACTIVE/EXPIRED 的窗口。永远是硬
  冲突；REQUESTED 的窗口只算"排队"，提示但不否决。

让路方案有五种，全部基于忙区间合并后的真实空档计算，给出的每一个时段都经过
可行性复检（不会给一个同样撞车的方案）：

SHIFT_AFTER  整体顺延到最后一个撞点之后；
SHIFT_BEFORE 整体提前到第一个撞点之前；
TRIM         按撞点裁掉冲突的头部或尾部，保留无冲突的一段；
SPLIT        被撞点切成多段，只保留不短于最短可接受时长的段；
ALT_ZONE     改到同站台没有占用的其他门区（需要人工确认可行性）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .intervals import DEFAULT_BUFFER_S, fits, free_gaps, merge_spans, overlaps
from .policy import MAINTENANCE_FIRST, StationPolicy
from .timetable import DERIVED, ServiceSlot
from .window import OCCUPYING, REQUESTED, Window

HARD = "hard"
SOFT = "soft"

SERVICE = "service"
OCCUPANCY = "occupancy"
PENDING = "pending"

SHIFT_AFTER = "SHIFT_AFTER"
SHIFT_BEFORE = "SHIFT_BEFORE"
TRIM = "TRIM"
SPLIT = "SPLIT"
ALT_ZONE = "ALT_ZONE"


@dataclass(frozen=True)
class Hit:
    """一次具体冲突。"""

    kind: str
    severity: str
    ref: str
    start: float
    end: float
    detail: str

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "ref": self.ref,
            "start": self.start,
            "end": self.end,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Proposal:
    """一个让路候选。"""

    kind: str
    summary: str
    windows: list[tuple[float, float]] = field(default_factory=list)
    group_id: str = ""
    affected_trains: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "windows": [[start, end] for start, end in self.windows],
            "group_id": self.group_id,
            "affected_trains": list(self.affected_trains),
        }


def service_hits(
    station_id: str,
    start: float,
    end: float,
    slots: list[ServiceSlot],
    *,
    buffer_s: float = DEFAULT_BUFFER_S,
    hard: bool = True,
) -> list[Hit]:
    """窗口与排班车次的冲突。"""

    hits: list[Hit] = []
    for slot in slots:
        buffered = (slot.arrive - (slot.buffer_s or buffer_s), slot.depart + (slot.buffer_s or buffer_s))
        if not overlaps(start, end, buffered[0], buffered[1]):
            continue
        if not overlaps(start, end, slot.arrive, slot.depart):
            detail = "落在接车避让余量内"
        else:
            detail = "占用期间有接车"
        severity = HARD if (hard and slot.source != DERIVED) else SOFT
        if slot.source == DERIVED:
            detail += "（推演车次，以实际计划为准）"
        hits.append(
            Hit(
                kind=SERVICE,
                severity=severity,
                ref=slot.train_id,
                start=slot.arrive,
                end=slot.depart,
                detail=detail,
            )
        )
    return hits


def occupancy_hits(
    station_id: str,
    group_id: str,
    start: float,
    end: float,
    others: list[Window],
) -> list[Hit]:
    """窗口与同门区其他窗口的冲突。"""

    hits: list[Hit] = []
    for other in others:
        if other.station_id != station_id or other.group_id != group_id:
            continue
        if not overlaps(start, end, other.start, other.end):
            continue
        if other.status in OCCUPYING:
            hits.append(
                Hit(
                    kind=OCCUPANCY,
                    severity=HARD,
                    ref=other.id,
                    start=other.start,
                    end=other.end,
                    detail=f"同门区窗口 {other.id} 处于 {other.status}（{other.kind}）",
                )
            )
        elif other.status == REQUESTED:
            hits.append(
                Hit(
                    kind=PENDING,
                    severity=SOFT,
                    ref=other.id,
                    start=other.start,
                    end=other.end,
                    detail=f"同门区待批申请 {other.id}",
                )
            )
    return hits


def _busy_spans(hits: list[Hit], service_slots: list[ServiceSlot]) -> tuple[list[tuple[float, float]], list[tuple[float, float]], set[str]]:
    """把撞点拆成排班忙区间、占用忙区间，并收集受影响车次。"""

    service_spans: list[tuple[float, float]] = []
    occupancy_spans: list[tuple[float, float]] = []
    trains: set[str] = set()
    for hit in hits:
        if hit.kind == SERVICE:
            trains.add(hit.ref)
            service_spans.append((hit.start, hit.end))
        elif hit.kind in (OCCUPANCY, PENDING):
            occupancy_spans.append((hit.start, hit.end))
    for slot in service_slots:
        if any(overlaps(hit.start, hit.end, slot.arrive, slot.depart) for hit in hits if hit.kind == SERVICE):
            service_spans.append(slot.buffered_span())
    return merge_spans(service_spans), merge_spans(occupancy_spans), trains


def build_proposals(
    start: float,
    end: float,
    hits: list[Hit],
    *,
    policy: StationPolicy,
    service_slots: list[ServiceSlot] | None = None,
    other_groups: list[str] | None = None,
    group_busy: dict[str, list[tuple[float, float]]] | None = None,
) -> list[Proposal]:
    """根据撞点生成让路候选，全部做可行性复检。"""

    service_slots = service_slots or []
    duration = end - start
    service_spans, occupancy_spans, trains = _busy_spans(hits, service_slots)
    busy = merge_spans(service_spans + occupancy_spans)
    if not busy:
        return []
    minimum = min(duration, policy.min_acceptable_s)
    horizon_start = min(start, busy[0][0]) - policy.horizon_s
    horizon_end = max(end, busy[-1][1]) + policy.horizon_s
    proposals: list[Proposal] = []

    def feasible(candidate_start: float, candidate_end: float) -> bool:
        return fits((candidate_start, candidate_end), minimum) and not any(
            overlaps(candidate_start, candidate_end, span_start, span_end)
            for span_start, span_end in busy
        )

    # SHIFT_AFTER：顺延到最后一个撞点之后（受视野限制）。
    after_start = max(busy[-1][1], start)
    after_end = after_start + duration
    if feasible(after_start, after_end) and after_end <= horizon_end:
        proposals.append(
            Proposal(SHIFT_AFTER, f"整体顺延到撞点之后 {_clock(after_start)} 起", [(after_start, after_end)])
        )

    # SHIFT_BEFORE：提前到第一个撞点之前。
    before_end = min(busy[0][0], end)
    before_start = before_end - duration
    if feasible(before_start, before_end) and before_start >= horizon_start:
        proposals.append(
            Proposal(SHIFT_BEFORE, f"整体提前到撞点之前 {_clock(before_end)} 止", [(before_start, before_end)])
        )

    # TRIM：在原始窗口内扣掉撞点，取可行的头部或尾部（保留较长者）。
    gaps = free_gaps(start, end, busy)
    for gap_start, gap_end in gaps:
        if gap_end - gap_start < minimum:
            continue
        # 贴头或贴尾的空档才叫"裁"，中间空档交给 SPLIT。
        if gap_start == start:
            proposals.append(
                Proposal(TRIM, f"裁掉尾部撞点，保留 {_clock(gap_start)}–{_clock(gap_end)}", [(gap_start, gap_end)])
            )
        elif gap_end == end:
            proposals.append(
                Proposal(TRIM, f"裁掉头部撞点，保留 {_clock(gap_start)}–{_clock(gap_end)}", [(gap_start, gap_end)])
            )

    # SPLIT：被撞点切开，保留全部够长的段。
    pieces = [(g_start, g_end) for g_start, g_end in gaps if g_end - g_start >= minimum]
    if len(pieces) >= 2:
        proposals.append(
            Proposal(
                SPLIT,
                f"按撞点拆成 {len(pieces)} 段分段检修",
                pieces,
            )
        )

    # ALT_ZONE：换一个同站台空闲门区（只在占用冲突有意义；排班冲突对所有门区成立）。
    if any(hit.kind in (OCCUPANCY, PENDING) for hit in hits):
        for group in other_groups or []:
            spans = merge_spans((group_busy or {}).get(group, []))
            if not any(overlaps(start, end, span_start, span_end) for span_start, span_end in spans):
                proposals.append(
                    Proposal(ALT_ZONE, f"改到同站台空闲门区 {group}", [(start, end)], group_id=group)
                )

    # maintenance_first 额外给出"调整接车"建议（让车次于检修）。
    if policy.precedence == MAINTENANCE_FIRST and trains:
        proposals.append(
            Proposal(
                SHIFT_AFTER,
                f"经调度确认后按原窗口检修，协调 {len(trains)} 趟车改点/通过/扣车",
                [(start, end)],
                affected_trains=sorted(trains),
            )
        )
    return proposals


def _clock(at: float) -> str:
    import time

    return time.strftime("%m-%d %H:%M", time.gmtime(at))
