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
        # 检修窗口占用判定：(gate_id, moment) -> bool。未接线时恒为未占用。
        self._window_held: Callable[[str], bool] = lambda gate_id: False

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

    def set_window_guard(self, held: Callable[[str], bool]) -> None:
        """接入检修窗口占用判定，门控读同一份占用（不另存标志）。"""

        self._window_held = held

    def window_held(self, gate_id: str) -> bool:
        """该门当前是否处于检修占用（占用期间禁止自动开门）。"""

        return self._window_held(gate_id)

    def set_batch_limit(self, limit: int) -> None:
        """调整一次批量开门的门区上限。"""

        if limit <= 0:
            raise ValueError("批量上限必须为正")
        self._batch_limit = limit

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
