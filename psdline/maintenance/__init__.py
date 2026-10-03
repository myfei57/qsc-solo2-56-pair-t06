"""检修窗口与门区占用。

域内分工：

窗口          :mod:`psdline.maintenance.window`     状态机与当前态
事件流水      :mod:`psdline.maintenance.ledger`     哈希链、只追加、重放
排班时段      :mod:`psdline.maintenance.timetable`  计划车次与 headway 推演
按站策略      :mod:`psdline.maintenance.policy`     运营优先 / 检修优先
冲突与让路    :mod:`psdline.maintenance.planner`    纯函数冲突计算
区间运算      :mod:`psdline.maintenance.intervals`  跨天切片与空档
编排服务      :mod:`psdline.maintenance.service`    对外唯一入口（含统一占用视图）
落盘与设备    :mod:`psdline.maintenance.repos`      注册表、outbox、fence、设备适配器
"""

from __future__ import annotations

from .filelock import FileLock
from .intervals import DaySlice, free_gaps, merge_spans, overlaps, slice_by_day
from .ledger import WindowLedger
from .planner import (
    ALT_ZONE,
    HARD,
    SHIFT_AFTER,
    SHIFT_BEFORE,
    SOFT,
    SPLIT,
    TRIM,
    Hit,
    Proposal,
    build_proposals,
    occupancy_hits,
    service_hits,
)
from .policy import (
    APPLICANT,
    DISPATCHER,
    MAINTENANCE_FIRST,
    SERVICE_FIRST,
    SUPERVISOR,
    PolicyRegistry,
    StationPolicy,
)
from .repos import (
    DoorInhibitorDevice,
    FenceCounter,
    IdempotencyStore,
    Outbox,
    WindowRegistry,
)
from .service import MaintenanceService
from .timetable import DERIVED, PLANNED, ServiceSlot, TimetableRegistry, derive_slots
from .window import (
    ACTIVE,
    APPROVED,
    CANCELLED,
    EMERGENCY,
    EXPIRED,
    LIVE,
    OCCUPYING,
    PREEMPTED,
    REJECTED,
    RELEASED,
    REQUESTED,
    ROUTINE,
    TERMINAL,
    Window,
)

__all__ = [
    "ALT_ZONE",
    "APPLICANT",
    "ACTIVE",
    "APPROVED",
    "CANCELLED",
    "DERIVED",
    "DISPATCHER",
    "DaySlice",
    "DoorInhibitorDevice",
    "EMERGENCY",
    "EXPIRED",
    "FenceCounter",
    "FileLock",
    "HARD",
    "IdempotencyStore",
    "LIVE",
    "MAINTENANCE_FIRST",
    "OCCUPYING",
    "Outbox",
    "PLANNED",
    "PREEMPTED",
    "PolicyRegistry",
    "Proposal",
    "RELEASED",
    "REJECTED",
    "REQUESTED",
    "ROUTINE",
    "SERVICE_FIRST",
    "SHIFT_AFTER",
    "SHIFT_BEFORE",
    "SOFT",
    "SPLIT",
    "SUPERVISOR",
    "ServiceSlot",
    "StationPolicy",
    "TERMINAL",
    "TRIM",
    "TimetableRegistry",
    "Window",
    "WindowLedger",
    "WindowRegistry",
    "build_proposals",
    "derive_slots",
    "free_gaps",
    "merge_spans",
    "occupancy_hits",
    "overlaps",
    "service_hits",
    "slice_by_day",
]
