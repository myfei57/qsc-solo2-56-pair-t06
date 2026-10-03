"""门控控制器装配。"""

from __future__ import annotations

from typing import Callable

from ..psd import LockoutRegistry, PSDRegistry
from ..store import CommandLog, JsonStore
from ..train import AlignRegistry


class DoorController:
    """门控动作的装配入口。"""

    def __init__(
        self,
        store: JsonStore,
        align: AlignRegistry,
        commands: CommandLog,
        gates: PSDRegistry,
        lockouts: LockoutRegistry,
    ) -> None:
        self._store = store
        self._align = align
        self._commands = commands
        self._gates = gates
        self._lockouts = lockouts
        self._batch_limit = 64
        self._opens = 0
        self._closes = 0
        # 检修占用守卫：给定 (station_id, group_id) 返回
        # {"inhibited": bool, "reason": str, "window_id": str}。门区联动与批量
        # 开门都读这一个入口，与排班/锁定/审计看到的是同一份占用。
        self._maintenance_guard: Callable[[str, str], dict] | None = None

    @property
    def store(self) -> JsonStore:
        return self._store

    @property
    def align(self) -> AlignRegistry:
        return self._align

    @property
    def commands(self) -> CommandLog:
        return self._commands

    @property
    def gates(self) -> PSDRegistry:
        return self._gates

    @property
    def lockouts(self) -> LockoutRegistry:
        return self._lockouts

    @property
    def opens(self) -> int:
        """返回累计开门次数。"""

        return self._opens

    @property
    def closes(self) -> int:
        """返回累计关门次数。"""

        return self._closes

    def set_batch_limit(self, limit: int) -> None:
        """调整一次批量开门的门区上限。"""

        if limit <= 0:
            raise ValueError("批量上限必须为正")
        self._batch_limit = limit

    def set_maintenance_guard(self, guard: Callable[[str, str], dict] | None) -> None:
        """注入检修占用查询（统一占用视图的门控侧入口）。"""

        self._maintenance_guard = guard

    def maintenance_inhibition(self, station_id: str, group_id: str) -> dict:
        """查询某门区是否被检修占用禁用自动开门；未注入守卫时一律放行。"""

        if self._maintenance_guard is None:
            return {"inhibited": False, "reason": "", "window_id": ""}
        result = self._maintenance_guard(station_id, group_id)
        if not isinstance(result, dict):
            return {"inhibited": bool(result), "reason": "maintenance_occupancy", "window_id": ""}
        return {
            "inhibited": bool(result.get("inhibited")),
            "reason": str(result.get("reason", "maintenance_occupancy")),
            "window_id": str(result.get("window_id", "")),
        }

    def note_open(self) -> int:
        """在一次成功开门后累加计数。"""

        self._opens += 1
        return self._opens

    def note_close(self) -> int:
        """在一次成功关门后累加计数。"""

        self._closes += 1
        return self._closes

    def batch_limit(self) -> int:
        """返回当前批量上限。"""

        return self._batch_limit
