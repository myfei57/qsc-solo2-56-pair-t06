"""检修域的落盘注册表、外发指令箱与门控设备适配器。

当前态文档（窗口、幂等记录、指令箱、fence 序号）都走原子 JSON 文档存储；窗口的
权威来源是事件流水（:mod:`psdline.maintenance.ledger`），文档只是快速读取路径，
随时可以重放重建。

外发的"禁止/恢复自动开门"指令不直接调设备，而是先写进指令箱（outbox）随事务一起
提交，再由投递器发送：设备暂时故障只影响投递，不会把已提交的占用弄丢。设备侧用
按门区单调递增的 fence 序号拒绝过期指令，避免"先释放旧窗口、再占用新窗口"时旧的
恢复指令晚到、把新占用的门错误放开。
"""

from __future__ import annotations

from ..errors import RailError
from ..store import JsonStore
from .window import TERMINAL, Window, can_transition

PENDING = "PENDING"
SENT = "SENT"
FAILED = "FAILED"

INHIBIT = "inhibit"
RESTORE = "restore"


class WindowRegistry:
    """窗口当前态读写，落在 ``maintenance/window/<id>``。

    终态文档拒绝覆盖（只允许从 EXPIRED 到 RELEASED 的收尾写入），作为"历史不可
    变"在存储层的第二道闸：即使有人绕过服务直接写注册表也会被拦下。
    """

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(window_id: str) -> str:
        return f"maintenance/window/{window_id}"

    def save(self, window: Window) -> Window:
        window.validate()
        raw = self._store.read(self._doc_id(window.id))
        if isinstance(raw, dict):
            previous = Window.from_dict(raw)
            if previous.status in TERMINAL:
                raise RailError(
                    f"窗口 {window.id} 已进入终态 {previous.status}，当前态不可变",
                    component="maintenance",
                )
            if window.status != previous.status and not can_transition(previous.status, window.status):
                # 存储层第二道闸：即使有人绕过服务直接写注册表，非法状态转移也会被拦。
                raise RailError(
                    f"窗口 {window.id} 不能从 {previous.status} 转到 {window.status}",
                    component="maintenance",
                )
        self._store.write(self._doc_id(window.id), window.as_dict())
        return window

    def raw_upsert(self, window: Window) -> None:
        """仅供事件流水重放重建使用：直接落盘，不做状态转移校验。"""

        window.validate()
        self._store.write(self._doc_id(window.id), window.as_dict())

    def get(self, window_id: str) -> Window | None:
        raw = self._store.read(self._doc_id(window_id))
        return Window.from_dict(raw) if isinstance(raw, dict) else None

    def require(self, window_id: str) -> Window:
        window = self.get(window_id)
        if window is None:
            raise RailError(f"检修窗口不存在: {window_id}", component="maintenance")
        return window

    def list(self) -> list[Window]:
        found: list[Window] = []
        for doc_id in self._store.list_ids():
            prefix = "maintenance/window/"
            if not doc_id.startswith(prefix) or doc_id.count("/") != 2:
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(Window.from_dict(raw))
        return sorted(found, key=lambda item: (item.start, item.id))

    def reset_from(self, windows: list[Window]) -> None:
        """用流水重放结果重建当前态（仅启动恢复使用，绕过状态机校验）。"""

        for window in windows:
            self.raw_upsert(window)


class IdempotencyStore:
    """幂等键记录：同一键的重复提交返回首个结果。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(key: str) -> str:
        return f"maintenance/idempotency/{key}"

    def lookup(self, key: str) -> dict | None:
        if not key:
            return None
        raw = self._store.read(self._doc_id(key))
        return raw if isinstance(raw, dict) else None

    def remember(self, key: str, outcome: dict) -> None:
        if key:
            self._store.write(self._doc_id(key), {"key": key, **dict(outcome)})


class Outbox:
    """外发指令箱，文档 ``maintenance/outbox`` 追加式保存。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._doc = "maintenance/outbox"

    def _load(self) -> list[dict]:
        raw = self._store.read(self._doc)
        return raw["commands"] if isinstance(raw, dict) and isinstance(raw.get("commands"), list) else []

    def enqueue(self, command: dict) -> dict:
        commands = self._load()
        command = {"id": len(commands) + 1, "status": PENDING, "attempts": 0, **dict(command)}
        commands.append(command)
        self._store.write(self._doc, {"commands": commands})
        return command

    def list(self) -> list[dict]:
        return self._load()

    def pending(self) -> list[dict]:
        return [item for item in self._load() if item["status"] in (PENDING, FAILED)]

    def mark(self, command_id: int, status: str, error: str = "") -> None:
        commands = self._load()
        for item in commands:
            if item["id"] == command_id:
                item["status"] = status
                item["attempts"] = int(item.get("attempts", 0)) + 1
                if error:
                    item["last_error"] = error
                break
        self._store.write(self._doc, {"commands": commands})


class FenceCounter:
    """按门区维护的单调 fence 序号。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._doc = "maintenance/fence"

    def _load(self) -> dict[str, int]:
        raw = self._store.read(self._doc)
        if not isinstance(raw, dict):
            return {}
        return {str(key): int(value) for key, value in raw.items() if key != "doc"}

    def next_value(self, key: str) -> int:
        values = self._load()
        values[key] = values.get(key, 0) + 1
        self._store.write(self._doc, values)
        return values[key]

    def value(self, key: str) -> int:
        return self._load().get(key, 0)


class DoorInhibitorDevice:
    """门控设备模拟器：记录每个门区收到的最新指令与 fence。

    ``fail_next`` 让下一条指令失败，用于验证"设备临时故障 → 投递重试 → 对账
    补偿"的部分失败处理；fence 落后或重复的指令会被拒绝（旧动作不能覆盖新状态）。
    """

    def __init__(self) -> None:
        self._state: dict[str, dict] = {}
        self.fail_next = 0
        self.delivered: list[dict] = []
        self.rejected: list[dict] = []

    def send(self, command: dict) -> dict:
        key = str(command.get("key", ""))
        fence = int(command.get("fence", 0))
        current = self._state.get(key)
        if current is not None and fence < current["fence"]:
            self.rejected.append(dict(command))
            return {"ok": False, "reason": "stale_fence", "fence": fence, "current": current["fence"]}
        if self.fail_next > 0:
            self.fail_next -= 1
            return {"ok": False, "reason": "device_unavailable", "fence": fence}
        self._state[key] = {"action": command.get("action"), "fence": fence, "window_id": command.get("window_id")}
        self.delivered.append(dict(command))
        return {"ok": True, "fence": fence}

    def inhibited(self, key: str) -> bool:
        current = self._state.get(key)
        return bool(current and current["action"] == INHIBIT)

    def snapshot(self) -> dict:
        return {key: dict(value) for key, value in self._state.items()}
