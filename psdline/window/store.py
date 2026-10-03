"""窗口存储：哈希链流水 + 内存投影。

不落"当前态"文档。窗口的全部事实是一条只追加的 JSONL 流水（``window-*.jsonl``），
每条记录是一个**事件批**：一次业务操作（申请、顶让、到期扫描……）产生的全部事件
打包在同一条记录里，一次 ``fsync`` 提交。由此得到三个性质：

* **原子提交 / 部分失败回滚**：操作先在内存里把整批事件构造完，提交前任何一步
  失败都一条不写；提交只有一次 append，不存在"写了一半"的现场。
* **历史不可变**：终态窗口（``closed``/``cancelled``/``rejected``）不会被删除或
  改写，投影时只从 live 表移出、转入历史表；任何修改旧记录的行为都会破坏
  SHA-256 哈希链，:meth:`WindowStore.verify_chain` 立即报错。
* **崩溃恢复**：重启时回放整条流水重建 live/历史，段文件缺失或序号不连续同样
  报错，不允许带着缺页的历史继续接申请。
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Callable

from ..errors import RailError, StorageError
from ..store.jsonfile import JsonStore
from ..store.journal import JsonlJournal
from .window import MaintenanceWindow

GENESIS = "0" * 64

#: 投影后从 live 表移出的事件后缀（进入不可变历史）。
TERMINAL_EVENTS = frozenset({"rejected", "cancelled", "closed"})


def _is_terminal(kind: str) -> bool:
    return kind == "rejected" or kind.endswith("_cancelled") or kind.endswith("_closed")


class ChainBroken(RailError):
    """窗口流水哈希链对不上（记录被改写、删除或缺段）。"""


class WindowStore:
    """窗口事件流水及其内存投影。"""

    def __init__(self, store: JsonStore) -> None:
        self._root = store.root
        self._journal = JsonlJournal(store.root, "window")
        self._lock = threading.RLock()
        self._live: dict[str, MaintenanceWindow] = {}
        self._history: list[MaintenanceWindow] = []
        self._last_hash = GENESIS
        self._seq = 0
        self._replay()

    @property
    def root(self) -> str:
        return self._root

    @property
    def journal(self) -> JsonlJournal:
        return self._journal

    # ---- 读 -----------------------------------------------------------

    def live(self) -> list[MaintenanceWindow]:
        """返回全部当前窗口（按起点排序）。"""

        with self._lock:
            return sorted(self._live.values(), key=lambda item: (item.start_at, item.id))

    def get(self, window_id: str) -> MaintenanceWindow | None:
        with self._lock:
            return self._live.get(window_id)

    def require(self, window_id: str) -> MaintenanceWindow:
        window = self.get(window_id)
        if window is None:
            raise RailError(f"窗口不存在或已终结: {window_id}", component="window")
        return window

    def history(self) -> list[MaintenanceWindow]:
        """返回全部已终结窗口（按终结先后）。只读，不可改。"""

        with self._lock:
            return list(self._history)

    @property
    def seq(self) -> int:
        return self._seq

    # ---- 提交 ---------------------------------------------------------

    def commit(
        self,
        events: list[dict],
        at: float,
        before_hook: Callable[[list[dict]], None] | None = None,
    ) -> list[MaintenanceWindow]:
        """原子提交一个事件批，返回投影后涉及的窗口。

        ``before_hook`` 在 append 之前、锁内调用，抛异常即整批回滚（流水里不留
        任何痕迹）。它用来模拟"持久化前夕的部分失败"，验证调用方的回滚路径。
        """

        if not events:
            return []
        if before_hook is not None:
            before_hook(events)
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "at": at,
                "events": events,
                "prev_hash": self._last_hash,
            }
            record["hash"] = _digest(record)
            try:
                self._journal.append(record)
            except StorageError:
                # append 失败：记录未入流水，序号与链尖不推进，整批视同未发生。
                self._seq -= 1
                raise
            self._last_hash = record["hash"]
            touched: list[MaintenanceWindow] = []
            for event in events:
                projected = self._project(event)
                if projected is not None:
                    touched.append(projected)
            return touched

    # ---- 回放与校验 ---------------------------------------------------

    def verify_chain(self) -> dict:
        """全量复核流水：哈希链、序号连续性，并对照内存投影。"""

        records = self._journal.read_all()
        previous = GENESIS
        live: dict[str, MaintenanceWindow] = {}
        history: list[MaintenanceWindow] = []
        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                raise ChainBroken(f"第 {index} 条记录不是对象", component="window")
            if int(record.get("seq", -1)) != index:
                raise ChainBroken(f"第 {index} 条记录序号断裂", component="window")
            if record.get("prev_hash") != previous:
                raise ChainBroken(f"第 {index} 条记录哈希链断裂", component="window")
            payload = {key: value for key, value in record.items() if key != "hash"}
            if _digest(payload) != record.get("hash"):
                raise ChainBroken(f"第 {index} 条记录内容被改写", component="window")
            previous = str(record.get("hash"))
            for event in record.get("events", []):
                _project_event(event, live, history)
        with self._lock:
            consistent = live.keys() == self._live.keys() and len(history) == len(self._history)
        return {
            "records": len(records),
            "events": sum(len(r.get("events", [])) for r in records),
            "live": len(live),
            "history": len(history),
            "consistent": consistent,
            "tail_hash": previous,
        }

    def _replay(self) -> None:
        records = self._journal.read_all()
        previous = GENESIS
        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict) or int(record.get("seq", -1)) != index:
                raise ChainBroken(f"第 {index} 条窗口流水记录损坏", component="window")
            if record.get("prev_hash") != previous:
                raise ChainBroken(f"第 {index} 条窗口流水哈希链断裂", component="window")
            if _digest({k: v for k, v in record.items() if k != "hash"}) != record.get("hash"):
                raise ChainBroken(f"第 {index} 条窗口流水内容被改写", component="window")
            previous = str(record.get("hash"))
            for event in record.get("events", []):
                self._project(event)
        self._last_hash = previous
        self._seq = len(records)

    def _project(self, event: dict) -> MaintenanceWindow | None:
        return _project_event(event, self._live, self._history)


def _project_event(
    event: dict,
    live: dict[str, MaintenanceWindow],
    history: list[MaintenanceWindow],
) -> MaintenanceWindow | None:
    """把单个事件投到 live/历史 两张表上。"""

    kind = str(event.get("type", ""))
    window_id = str(event.get("window_id", ""))
    raw_window = event.get("window")
    if _is_terminal(kind):
        window = (
            MaintenanceWindow.from_dict(raw_window)
            if isinstance(raw_window, dict)
            else live.pop(window_id, None)
        )
        if window is not None:
            live.pop(window.id, None)
            history.append(window)
        return window
    if not isinstance(raw_window, dict):
        return None
    window = MaintenanceWindow.from_dict(raw_window)
    live[window.id] = window
    return window


def event(kind: str, window: MaintenanceWindow | None, **detail: object) -> dict:
    """构造一个事件：默认携带窗口完整快照，``detail`` 记录为什么变。"""

    return {
        "type": kind,
        "window_id": window.id if window is not None else str(detail.pop("window_id", "")),
        "window": window.as_dict() if window is not None else None,
        "detail": {key: value for key, value in detail.items() if value is not None},
    }


def _digest(record: dict) -> str:
    """对"除 hash 外的全部字段"做规范化 JSON 后取 SHA-256。"""

    body = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
