"""接车时刻表：窗口与排班冲突的事实来源。

现有排班（:class:`psdline.station.schedule.Schedule`）只给追踪间隔，给不出"几点
几分有车要停这樘门"。窗口系统需要的是到点时段，因此另立一份不可变接车计划：
每个 :class:`TrainVisit` 是一列车在某站的到发占用区间，外加它会联动的门清单
（空表示整站门）。

时刻表是版本化的：保检修的站批准窗口后会要求让排班改点，改点不是抹掉旧计划，
而是写入 ``revision + 1`` 的新版本；旧版本连同窗口审计一起保留，解释"当时按哪份
排班判的冲突"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import RailError
from ..store import JsonStore
from .occupancy import Interval, overlaps


@dataclass(frozen=True)
class TrainVisit:
    """一列车在某站的一次停靠（接车占用）。"""

    train_id: str
    station_id: str
    arrive_at: float
    depart_at: float
    gate_ids: tuple[str, ...] = ()
    track: str = ""

    def validate(self) -> "TrainVisit":
        if not self.train_id:
            raise RailError("接车计划缺少车次", component="window")
        if not self.station_id:
            raise RailError("接车计划缺少站台", component="window")
        if self.depart_at <= self.arrive_at:
            raise RailError("发车时刻必须晚于到站时刻", component="window")
        return self

    @property
    def interval(self) -> Interval:
        return Interval(self.arrive_at, self.depart_at)

    def affects(self, gate_id: str) -> bool:
        """门清单为空表示整站门都要联动。"""

        return not self.gate_ids or gate_id in self.gate_ids

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "station_id": self.station_id,
            "arrive_at": self.arrive_at,
            "depart_at": self.depart_at,
            "gate_ids": list(self.gate_ids),
            "track": self.track,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "TrainVisit":
        return cls(
            train_id=str(payload.get("train_id", "")),
            station_id=str(payload.get("station_id", "")),
            arrive_at=float(payload.get("arrive_at", 0.0)),
            depart_at=float(payload.get("depart_at", 0.0)),
            gate_ids=tuple(str(item) for item in payload.get("gate_ids", [])),
            track=str(payload.get("track", "")),
        )


@dataclass
class Timetable:
    """一个站某一版本的接车时刻表。"""

    station_id: str
    revision: int
    visits: list[TrainVisit] = field(default_factory=list)
    created_at: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "note": self.note,
            "visits": [visit.as_dict() for visit in self.visits],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Timetable":
        return cls(
            station_id=str(payload.get("station_id", "")),
            revision=int(payload.get("revision", 1)),
            created_at=float(payload.get("created_at", 0.0)),
            note=str(payload.get("note", "")),
            visits=[
                TrainVisit.from_dict(item)
                for item in payload.get("visits", [])
                if isinstance(item, dict)
            ],
        )


class TimetableRegistry:
    """时刻表版本读写，落在 ``window/timetable/<station>/<revision>``。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(station_id: str, revision: int) -> str:
        return f"window/timetable/{station_id}/{revision:04d}"

    def save(self, timetable: Timetable) -> Timetable:
        timetable.visits = sorted(timetable.visits, key=lambda item: item.arrive_at)
        for visit in timetable.visits:
            visit.validate()
            if visit.station_id != timetable.station_id:
                raise RailError(
                    f"时刻表 {timetable.station_id} 混入了 {visit.station_id} 的接车",
                    component="window",
                )
        self._detect_self_overlap(timetable.visits)
        self._store.write(self._doc_id(timetable.station_id, timetable.revision), timetable.as_dict())
        return timetable

    def get(self, station_id: str, revision: int) -> Timetable | None:
        raw = self._store.read(self._doc_id(station_id, revision))
        return Timetable.from_dict(raw) if isinstance(raw, dict) else None

    def revisions(self, station_id: str) -> list[int]:
        prefix = f"window/timetable/{station_id}/"
        found = [
            int(doc_id.rsplit("/", 1)[-1])
            for doc_id in self._store.list_ids()
            if doc_id.startswith(prefix)
        ]
        return sorted(found)

    def latest(self, station_id: str) -> Timetable | None:
        revisions = self.revisions(station_id)
        return self.get(station_id, revisions[-1]) if revisions else None

    def require_latest(self, station_id: str) -> Timetable:
        timetable = self.latest(station_id)
        if timetable is None:
            raise RailError(f"站台 {station_id} 没有接车时刻表", component="window")
        return timetable

    def next_revision(self, station_id: str) -> int:
        revisions = self.revisions(station_id)
        return revisions[-1] + 1 if revisions else 1

    def _detect_self_overlap(self, visits: list[TrainVisit]) -> None:
        """同一站台同一轨道的接车区间不能互相重叠（排班自身先得自洽）。"""

        ordered = sorted(visits, key=lambda item: item.arrive_at)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.track == current.track and overlaps(previous.interval, current.interval):
                raise RailError(
                    f"时刻表自相重叠: {previous.train_id} 与 {current.train_id}",
                    component="window",
                )
