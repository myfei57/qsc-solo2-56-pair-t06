"""接车排班时段。

检修窗口要能看出"和排班冲不冲突"，就必须把排班表达成时间轴上的占用区间。一条
:class:`ServiceSlot` 表示某站台某趟车在 ``[arrive, depart)`` 内占用站台门区
（到站开门到发车离站），另外在两侧各留 ``buffer_s`` 的接车余量，冲突判定按带
余量的区间走。

排班有两种来源：显式登记的计划车次（权威），以及在没有显式计划时按追踪间隔
（headway）展开的推演车次。推演车次只用于冲突预显，登记成计划后不再重复展开。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..errors import RailError
from ..store import JsonStore
from .intervals import DEFAULT_BUFFER_S, SECONDS_PER_DAY, overlaps

PLANNED = "planned"
DERIVED = "derived"


@dataclass(frozen=True)
class ServiceSlot:
    """一趟车在某站台的占用时段。"""

    station_id: str
    train_id: str
    arrive: float
    depart: float
    source: str = PLANNED
    buffer_s: float = DEFAULT_BUFFER_S

    def __post_init__(self) -> None:
        if self.depart <= self.arrive:
            object.__setattr__(self, "depart", self.arrive + 1.0)

    def core_span(self) -> tuple[float, float]:
        return (self.arrive, self.depart)

    def buffered_span(self) -> tuple[float, float]:
        return (self.arrive - self.buffer_s, self.depart + self.buffer_s)

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "train_id": self.train_id,
            "arrive": self.arrive,
            "depart": self.depart,
            "source": self.source,
            "buffer_s": self.buffer_s,
        }


class TimetableRegistry:
    """显式计划车次的登记与读取（落在 ``timetable/<station>`` 文档）。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(station_id: str) -> str:
        return f"timetable/{station_id}"

    def _read(self, station_id: str) -> list[dict]:
        raw = self._store.read(self._doc_id(station_id))
        return raw["slots"] if isinstance(raw, dict) and isinstance(raw.get("slots"), list) else []

    def list(self, station_id: str) -> list[ServiceSlot]:
        return [
            ServiceSlot(
                station_id=str(item.get("station_id", station_id)),
                train_id=str(item.get("train_id", "")),
                arrive=float(item.get("arrive", 0.0)),
                depart=float(item.get("depart", 0.0)),
                source=PLANNED,
                buffer_s=float(item.get("buffer_s", DEFAULT_BUFFER_S)),
            )
            for item in self._read(station_id)
        ]

    def add(self, slot: ServiceSlot) -> ServiceSlot:
        """登记一趟计划车；同站台同车次同时段已存在时幂等返回。"""

        if not slot.station_id or not slot.train_id:
            raise RailError("计划车次缺少站台或车次标识", component="maintenance")
        slots = self._read(slot.station_id)
        for item in slots:
            if (
                str(item.get("train_id")) == slot.train_id
                and float(item.get("arrive", 0.0)) == slot.arrive
            ):
                return slot
        slots.append(slot.as_dict())
        slots.sort(key=lambda item: float(item.get("arrive", 0.0)))
        self._store.write(self._doc_id(slot.station_id), {"station_id": slot.station_id, "slots": slots})
        return slot

    def within(
        self, station_id: str, start: float, end: float, *, buffered: bool = False
    ) -> list[ServiceSlot]:
        """返回与给定区间相交的全部计划车次。

        ``buffered`` 为真时按两侧接车余量后的区间判定，用于冲突分析（窗口落在
        车前避让余量里也要算撞车）。
        """

        result = []
        for slot in self.list(station_id):
            span = slot.buffered_span() if buffered else slot.core_span()
            if overlaps(start, end, span[0], span[1]):
                result.append(slot)
        return result


def derive_slots(
    station_id: str,
    headways_s: Iterable[float],
    horizon_start: float,
    horizon_end: float,
    *,
    dwell_s: float = 60.0,
    buffer_s: float = DEFAULT_BUFFER_S,
    service_start_hour: int = 5,
    service_end_hour: int = 24,
    tz_offset_s: int = 0,
) -> list[ServiceSlot]:
    """按追踪间隔在给定视野内推演接车时段（仅在运营时段内）。

    推演从视野起点对齐到第一个追踪间隔边界，逐班累加，跳过落在运营时段之外的
    班次（例如凌晨收班窗口）。结果显式标记为 ``derived``，便于审批时和计划车次
    区分；调用方负责不要把推演结果持久化成事实。
    """

    headways = [float(item) for item in headways_s if float(item) > 0]
    if not headways or horizon_end <= horizon_start:
        return []
    open_border = tz_offset_s + service_start_hour * 3600
    close_border = tz_offset_s + service_end_hour * 3600

    def in_service(at: float) -> bool:
        local_day = int((at - tz_offset_s) // SECONDS_PER_DAY)
        opened = local_day * SECONDS_PER_DAY + open_border
        closed = local_day * SECONDS_PER_DAY + close_border
        return opened <= at < closed

    slots: list[ServiceSlot] = []
    cursor = float(horizon_start)
    index = 0
    while cursor < horizon_end:
        headway = headways[index % len(headways)]
        if in_service(cursor):
            slots.append(
                ServiceSlot(
                    station_id=station_id,
                    train_id=f"DERIVED-{len(slots) + 1:03d}",
                    arrive=cursor,
                    depart=cursor + dwell_s,
                    source=DERIVED,
                    buffer_s=buffer_s,
                )
            )
        cursor += headway
        index += 1
    return slots
