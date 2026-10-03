"""按站冲突策略。

冲突时"优先保运营还是优先保检修"两种取舍都成立，差别在各站的现场条件，因此策略
按站台配置，默认 ``service_first``（安全侧默认：宁可不修也不撞车）。

``service_first``  撞接车时段的窗口一律不批，让路方案只给"让车"的；
                   紧急抢修可由主管强制占用，撞点车次转为需调度改点/扣车。
``maintenance_first`` 检修让路优先，撞车的常规窗口也允许在调度确认后批准；
                   让路方案同时给出"改期"和"调整接车"两类建议。

物理冲突（同门区已有占用中的窗口）在两种策略下都是硬冲突，策略无法覆盖；紧急
抢修可以挤掉"已批准但尚未开工"的常规窗口，进行中的窗口不允许被软件挤掉。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ConfigError, RailError
from ..store import JsonStore

SERVICE_FIRST = "service_first"
MAINTENANCE_FIRST = "maintenance_first"
POLICIES = frozenset({SERVICE_FIRST, MAINTENANCE_FIRST})

# 角色：申请人 / 行车调度 / 值站主管。
APPLICANT = "applicant"
DISPATCHER = "dispatcher"
SUPERVISOR = "supervisor"
ROLES = frozenset({APPLICANT, DISPATCHER, SUPERVISOR})


@dataclass(frozen=True)
class StationPolicy:
    """一个站台的检修窗口策略。"""

    station_id: str
    precedence: str = SERVICE_FIRST
    # 到期后多少秒仍未完工确认就升级提醒（不自动释放）。
    expire_grace_s: float = 900.0
    # 接车时段两侧的避让余量。
    service_buffer_s: float = 300.0
    # 运营时段（本地小时，用于推演车次与让路候选）。
    service_start_hour: int = 5
    service_end_hour: int = 24
    # 运营日分界点相对本地零点的偏移秒数（凌晨收班线路设成 4*3600）。
    day_boundary_offset_s: int = 0
    tz_offset_s: int = 0
    # 单个窗口允许的最长时长。
    max_window_s: float = 72 * 3600.0
    # 让路候选可接受的最短时长（低于此值不再推荐"缩短/切分"）。
    min_acceptable_s: float = 1200.0
    # 让路候选搜索半径。
    horizon_s: float = 12 * 3600.0

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "precedence": self.precedence,
            "expire_grace_s": self.expire_grace_s,
            "service_buffer_s": self.service_buffer_s,
            "service_start_hour": self.service_start_hour,
            "service_end_hour": self.service_end_hour,
            "day_boundary_offset_s": self.day_boundary_offset_s,
            "tz_offset_s": self.tz_offset_s,
            "max_window_s": self.max_window_s,
            "min_acceptable_s": self.min_acceptable_s,
            "horizon_s": self.horizon_s,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "StationPolicy":
        return cls(
            station_id=str(payload.get("station_id", "")),
            precedence=str(payload.get("precedence", SERVICE_FIRST)),
            expire_grace_s=float(payload.get("expire_grace_s", 900.0)),
            service_buffer_s=float(payload.get("service_buffer_s", 300.0)),
            service_start_hour=int(payload.get("service_start_hour", 5)),
            service_end_hour=int(payload.get("service_end_hour", 24)),
            day_boundary_offset_s=int(payload.get("day_boundary_offset_s", 0)),
            tz_offset_s=int(payload.get("tz_offset_s", 0)),
            max_window_s=float(payload.get("max_window_s", 72 * 3600.0)),
            min_acceptable_s=float(payload.get("min_acceptable_s", 1200.0)),
            horizon_s=float(payload.get("horizon_s", 12 * 3600.0)),
        )

    def validate(self) -> "StationPolicy":
        if self.precedence not in POLICIES:
            raise ConfigError(f"未知的冲突策略: {self.precedence}", component="maintenance")
        if self.expire_grace_s < 0:
            raise ConfigError("到期宽限期不能为负", component="maintenance")
        if self.service_buffer_s < 0:
            raise ConfigError("接车避让余量不能为负", component="maintenance")
        if not 0 <= self.service_start_hour < self.service_end_hour <= 24:
            raise ConfigError("运营时段必须满足 0 <= start < end <= 24", component="maintenance")
        if self.max_window_s <= 0 or self.min_acceptable_s <= 0 or self.horizon_s <= 0:
            raise ConfigError("窗口限值必须为正", component="maintenance")
        return self


class PolicyRegistry:
    """按站策略读写，未配置的站台回落到默认策略。"""

    def __init__(self, store: JsonStore, default: StationPolicy | None = None) -> None:
        self._store = store
        self._default = (default or StationPolicy(station_id="")).validate()

    @staticmethod
    def _doc_id(station_id: str) -> str:
        return f"maintenance/{station_id}/policy"

    def get(self, station_id: str) -> StationPolicy:
        raw = self._store.read(self._doc_id(station_id))
        if not isinstance(raw, dict):
            return StationPolicy(station_id=station_id, **{
                key: value
                for key, value in self._default.as_dict().items()
                if key != "station_id"
            })
        return StationPolicy.from_dict(raw).validate()

    def save(self, policy: StationPolicy) -> StationPolicy:
        if not policy.station_id:
            raise RailError("策略缺少站台标识", component="maintenance")
        policy.validate()
        self._store.write(self._doc_id(policy.station_id), policy.as_dict())
        return policy
