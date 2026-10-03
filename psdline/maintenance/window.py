"""检修窗口领域模型与状态机。

窗口（:class:`Window`）是一份"某站台某门区在某时段被检修占用"的申请。它的当前态
会落盘成一份文档，但状态转移的唯一合法来源是事件流水（见
:mod:`psdline.maintenance.ledger`）：当前态文档丢了可以从流水重放，进入终态后
当前态文档也不允许再改（冻结），满足"历史不可变"。

状态机::

    REQUESTED ──approve──▶ APPROVED ──tick(到达开始时刻)──▶ ACTIVE
        │                     │                                │
        │                  reject/cancel                  finish/expire
        │                     ▼                                ▼
        │                 REJECTED/CANCELLED              EXPIRED(到期,门仍禁用)
        ▼                                                  finish/force_finish
    REJECTED                                              ──▶ RELEASED
    CANCELLED
    PREEMPTED （被紧急抢修挤掉，只允许从 APPROVED 进入）

``EXPIRED`` 不是终态：到期只代表审批时段用完，门仍然禁止自动开门，必须现场完工
确认（``finish`` 对在期窗口，``force_finish`` 对到期窗口）才进入 ``RELEASED``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import RailError

REQUESTED = "REQUESTED"
APPROVED = "APPROVED"
ACTIVE = "ACTIVE"
EXPIRED = "EXPIRED"
RELEASED = "RELEASED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
PREEMPTED = "PREEMPTED"

# 会对门产生物理占用的状态：审批通过即锁门，到期也不自动放开。
OCCUPYING = frozenset({APPROVED, ACTIVE, EXPIRED})
# 参与冲突判定的存活状态。
LIVE = frozenset({REQUESTED, APPROVED, ACTIVE, EXPIRED})
# 终态：除 EXPIRED 外都不再允许任何转移。
TERMINAL = frozenset({RELEASED, CANCELLED, REJECTED, PREEMPTED})

ROUTINE = "routine"
EMERGENCY = "emergency"
WINDOW_KINDS = frozenset({ROUTINE, EMERGENCY})

# 合法的状态转移表。
TRANSITIONS: dict[str, frozenset[str]] = {
    REQUESTED: frozenset({APPROVED, REJECTED, CANCELLED}),
    APPROVED: frozenset({ACTIVE, CANCELLED, PREEMPTED}),
    ACTIVE: frozenset({EXPIRED, RELEASED}),
    EXPIRED: frozenset({RELEASED}),
    RELEASED: frozenset(),
    CANCELLED: frozenset(),
    REJECTED: frozenset(),
    PREEMPTED: frozenset(),
}


@dataclass
class Window:
    """一樘门（门区）的检修占用申请。"""

    id: str
    station_id: str
    group_id: str
    start: float
    end: float
    applicant: str
    status: str = REQUESTED
    kind: str = ROUTINE
    reason: str = ""
    ticket: str = ""
    note: str = ""
    idem_key: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    released_by: str = ""
    escalated: bool = False
    seq: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "station_id": self.station_id,
            "group_id": self.group_id,
            "start": self.start,
            "end": self.end,
            "applicant": self.applicant,
            "status": self.status,
            "kind": self.kind,
            "reason": self.reason,
            "ticket": self.ticket,
            "note": self.note,
            "idem_key": self.idem_key,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "released_by": self.released_by,
            "escalated": self.escalated,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Window":
        return cls(
            id=str(payload.get("id", "")),
            station_id=str(payload.get("station_id", "")),
            group_id=str(payload.get("group_id", "")),
            start=float(payload.get("start", 0.0)),
            end=float(payload.get("end", 0.0)),
            applicant=str(payload.get("applicant", "")),
            status=str(payload.get("status", REQUESTED)),
            kind=str(payload.get("kind", ROUTINE)),
            reason=str(payload.get("reason", "")),
            ticket=str(payload.get("ticket", "")),
            note=str(payload.get("note", "")),
            idem_key=str(payload.get("idem_key", "")),
            created_at=float(payload.get("created_at", 0.0)),
            updated_at=float(payload.get("updated_at", 0.0)),
            released_by=str(payload.get("released_by", "")),
            escalated=bool(payload.get("escalated", False)),
            seq=int(payload.get("seq", 0)),
        )

    def validate(self) -> None:
        """落盘前的字段级校验。"""

        if not self.id:
            raise RailError("检修窗口标识为空", component="maintenance")
        if not self.station_id:
            raise RailError("检修窗口缺少站台", component="maintenance")
        if not self.group_id:
            raise RailError("检修窗口缺少门区", component="maintenance")
        if self.end <= self.start:
            raise RailError("检修窗口结束时刻必须晚于开始时刻", component="maintenance")
        if self.kind not in WINDOW_KINDS:
            raise RailError(f"未知的检修类型: {self.kind}", component="maintenance")
        if self.status not in TRANSITIONS:
            raise RailError(f"未知的窗口状态: {self.status}", component="maintenance")


def can_transition(current: str, target: str) -> bool:
    """状态转移是否合法。"""

    return target in TRANSITIONS.get(current, frozenset())
