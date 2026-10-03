"""门区检修窗口子系统。

一套覆盖"申请占用 → 冲突检测 → 让路方案 → 占用锁定 → 到期释放 → 人工销账"的
检修窗口能力。排班、门控、联锁锁定与审计读到的是同一份占用（见
:mod:`psdline.window.occupancy` 上的共享判定），不允许各组件各自维护一份。

关键取舍（设计期定死，见各模块文档字符串）：

* 冲突策略按站可配（:data:`~psdline.window.window.SERVICE_FIRST` /
  :data:`~psdline.window.window.MAINTENANCE_FIRST`）。撞检修申请恒为硬冲突；
  撞排班时，保运营的站拒绝申请并给让路方案，保检修的站批准申请并把排班变更
  作为让路方案。
* 窗口到期由控制周期自动释放门体，但窗口留在 ``expired_unconfirmed`` 挂账态，
  必须值班人 :meth:`WindowService.confirm_close` 销账；未销账的门不能再申请。
"""

from __future__ import annotations

from .conflict import Conflict, ConflictFinder, ScheduleConflict, WindowConflict
from .occupancy import Interval, overlaps, occupied_gates
from .service import WindowService, build_window_service
from .store import ChainBroken, WindowStore
from .timetable import Timetable, TimetableRegistry, TrainVisit
from .window import (
    ACTIVE,
    APPROVED,
    CANCELLED,
    CLOSED,
    EXPIRED_UNCONFIRMED,
    MAINTENANCE_FIRST,
    REJECTED,
    SERVICE_FIRST,
    TERMINAL_STATES,
    MaintenanceWindow,
    StationWindowPolicy,
)
from .yieldplan import YieldPlanner, YieldProposal

__all__ = [
    "ACTIVE",
    "APPROVED",
    "CANCELLED",
    "CLOSED",
    "CONFIRMED",
    "EXPIRED_UNCONFIRMED",
    "MAINTENANCE_FIRST",
    "REJECTED",
    "SERVICE_FIRST",
    "TERMINAL_STATES",
    "Conflict",
    "ConflictFinder",
    "Interval",
    "MaintenanceWindow",
    "ScheduleConflict",
    "StationWindowPolicy",
    "TrainVisit",
    "Timetable",
    "TimetableRegistry",
    "WindowConflict",
    "WindowService",
    "YieldPlanner",
    "YieldProposal",
    "build_window_service",
    "occupied_gates",
    "overlaps",
]
