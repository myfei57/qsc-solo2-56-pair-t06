"""检修窗口模型与按站冲突策略。

窗口有五个业务状态：

``requested`` 从未落盘——申请要么直接批准，要么带让路方案拒绝，不存在"待批"
              悬挂态占用门体，避免先到先得式的占位。
``approved``  已批准、尚未到点。未来时段内同门冲突检测把它算占用。
``active``    已到点，门体被锁出自动开门，现场可以检修。
``expired_unconfirmed``
              到点自动释放后的挂账态：门已交还运营，但值班人未确认现场收工。
              此态的门不能再提新申请，必须先销账。
``cancelled`` / ``rejected`` / ``closed``
              终态，只存在于不可变历史流水里。

跨天不是特例：起止一律存 Unix 秒（UTC），``end < start`` 之外不做任何"按自然日
切分"的假设，跨零点窗口就是一个普通区间。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import ConfigError, RailError

# ---- 窗口状态 -------------------------------------------------------------

APPROVED = "approved"
ACTIVE = "active"
EXPIRED_UNCONFIRMED = "expired_unconfirmed"
CANCELLED = "cancelled"
REJECTED = "rejected"
CLOSED = "closed"

#: 终态：进入后不可变，只保留在历史流水里。
TERMINAL_STATES = frozenset({CANCELLED, REJECTED, CLOSED})
#: 当前态：参与冲突检测与占用判定。
LIVE_STATES = frozenset({APPROVED, ACTIVE, EXPIRED_UNCONFIRMED})

# ---- 冲突策略 -------------------------------------------------------------

#: 保运营：撞排班即拒绝，让路方案改时间/换门。
SERVICE_FIRST = "service_first"
#: 保检修：撞排班仍批准，让路方案是让排班改点（需运营侧确认执行）。
MAINTENANCE_FIRST = "maintenance_first"
POLICIES = frozenset({SERVICE_FIRST, MAINTENANCE_FIRST})


@dataclass(frozen=True)
class StationWindowPolicy:
    """一个站的窗口策略。

    ``expiry_grace_s`` 是到期前宽限：控制周期在 ``end + grace`` 才自动释放，
    给现场一个缓冲；宽限期内门仍被占用，接车冲突会照常暴露。
    ``allow_force`` 控制本站值班人能否用 ``force`` 顶掉硬冲突（顶掉即取消对方
    未开始的窗口，双方都落审计）。
    """

    conflict_policy: str = SERVICE_FIRST
    expiry_grace_s: float = 0.0
    allow_force: bool = False

    def validate(self) -> "StationWindowPolicy":
        if self.conflict_policy not in POLICIES:
            raise ConfigError(
                f"未知冲突策略: {self.conflict_policy}", component="window"
            )
        if self.expiry_grace_s < 0:
            raise ConfigError("到期宽限不能为负", component="window")
        return self

    def as_dict(self) -> dict:
        return {
            "conflict_policy": self.conflict_policy,
            "expiry_grace_s": self.expiry_grace_s,
            "allow_force": self.allow_force,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "StationWindowPolicy":
        return cls(
            conflict_policy=str(payload.get("conflict_policy", SERVICE_FIRST)),
            expiry_grace_s=float(payload.get("expiry_grace_s", 0.0)),
            allow_force=bool(payload.get("allow_force", False)),
        ).validate()


@dataclass
class MaintenanceWindow:
    """一宗门区检修占用申请（及其批准后的生命周期状态）。"""

    id: str
    station_id: str
    gate_ids: list[str]
    start_at: float
    end_at: float
    requested_by: str
    reason: str
    status: str = APPROVED
    conflict_policy: str = SERVICE_FIRST
    created_at: float = 0.0
    activated_at: float | None = None
    released_at: float | None = None
    closed_at: float | None = None
    closed_by: str = ""
    cancelled_at: float | None = None
    extension_of: str = ""
    superseded_by: str = ""
    conflicts: list[dict] = field(default_factory=list)
    schedule_displacements: list[dict] = field(default_factory=list)
    revision: int = 1

    def validate(self) -> "MaintenanceWindow":
        if not self.id:
            raise RailError("窗口标识为空", component="window")
        if not self.station_id:
            raise RailError("窗口缺少站台", component="window")
        if not self.gate_ids:
            raise RailError("窗口至少要占一樘门", component="window")
        if len(set(self.gate_ids)) != len(self.gate_ids):
            raise RailError("窗口门清单重复", component="window")
        if self.start_at < 0:
            raise RailError("窗口起点不能为负", component="window")
        if self.end_at <= self.start_at:
            raise RailError("窗口止点必须晚于起点", component="window")
        if self.conflict_policy not in POLICIES:
            raise RailError(f"未知冲突策略: {self.conflict_policy}", component="window")
        return self

    @property
    def duration_s(self) -> float:
        return self.end_at - self.start_at

    @property
    def is_live(self) -> bool:
        """当前态（未进终态历史）。"""

        return self.status in LIVE_STATES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE

    def covers(self, moment: float) -> bool:
        return self.start_at <= moment < self.end_at

    def spans_midnight(self, day_start: float = 0.0) -> bool:
        """跨天判定：起止落在不同的自然日（UTC，相对 Unix 纪元）。

        ``day_start`` 允许测试注入本地时区零点偏移；生产侧统一按 UTC 自然日，
        展示层再做时区换算。
        """

        day = 86_400
        return int((self.start_at - day_start) // day) != int(
            (self.end_at - day_start) // day
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "station_id": self.station_id,
            "gate_ids": list(self.gate_ids),
            "start_at": self.start_at,
            "end_at": self.end_at,
            "requested_by": self.requested_by,
            "reason": self.reason,
            "status": self.status,
            "conflict_policy": self.conflict_policy,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "released_at": self.released_at,
            "closed_at": self.closed_at,
            "closed_by": self.closed_by,
            "cancelled_at": self.cancelled_at,
            "extension_of": self.extension_of,
            "superseded_by": self.superseded_by,
            "conflicts": [dict(item) for item in self.conflicts],
            "schedule_displacements": [dict(item) for item in self.schedule_displacements],
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "MaintenanceWindow":
        return cls(
            id=str(payload.get("id", "")),
            station_id=str(payload.get("station_id", "")),
            gate_ids=[str(item) for item in payload.get("gate_ids", [])],
            start_at=float(payload.get("start_at", 0.0)),
            end_at=float(payload.get("end_at", 0.0)),
            requested_by=str(payload.get("requested_by", "")),
            reason=str(payload.get("reason", "")),
            status=str(payload.get("status", APPROVED)),
            conflict_policy=str(payload.get("conflict_policy", SERVICE_FIRST)),
            created_at=float(payload.get("created_at", 0.0)),
            activated_at=_opt_float(payload.get("activated_at")),
            released_at=_opt_float(payload.get("released_at")),
            closed_at=_opt_float(payload.get("closed_at")),
            closed_by=str(payload.get("closed_by", "")),
            cancelled_at=_opt_float(payload.get("cancelled_at")),
            extension_of=str(payload.get("extension_of", "")),
            superseded_by=str(payload.get("superseded_by", "")),
            conflicts=[dict(item) for item in payload.get("conflicts", [])],
            schedule_displacements=[
                dict(item) for item in payload.get("schedule_displacements", [])
            ],
            revision=int(payload.get("revision", 1)),
        )


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
