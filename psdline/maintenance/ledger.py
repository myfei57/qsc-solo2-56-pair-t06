"""检修窗口事件流水：只追加、哈希链、可重放。

当前态文档可以随时从流水重放重建，因此提交流程是"先追加事件，再更新当前态"：
哪怕写完事件后进程立刻崩溃，重启重放也能把窗口文档补回来，不需要跨文件事务。

每条事件携带 ``base_hash``（上一条事件的哈希）与自身 ``hash``，形成一条链；
:meth:`WindowLedger.verify` 重算全链，任何对历史记录的删改都会断链。流水本身
用 :class:`~psdline.store.JsonlJournal` 追加写，正常流程没有任何修改入口。
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from ..store import JsonlJournal
from ..errors import RailError
from .window import (
    ACTIVE,
    APPROVED,
    CANCELLED,
    EXPIRED,
    PREEMPTED,
    REJECTED,
    RELEASED,
    REQUESTED,
    Window,
)

GENESIS = "0" * 64


def canonical(payload: Any) -> str:
    """稳定序列化：键排序、无空白，保证哈希在任何机器上一致。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WindowLedger:
    """窗口生命周期事件流水。"""

    def __init__(self, journal: JsonlJournal) -> None:
        self._journal = journal
        self._lock = threading.RLock()
        self._tail_hash = GENESIS
        self._count = 0
        self._bootstrap()

    def _bootstrap(self) -> None:
        records = self._journal.read_all()
        self._count = len(records)
        if records:
            self._tail_hash = str(records[-1].get("hash", ""))

    @property
    def count(self) -> int:
        return self._count

    def append(self, window_id: str, etype: str, at: float, actor: str, data: dict) -> dict:
        """追加一条事件，返回落盘记录。"""

        if not window_id or not etype:
            raise RailError("事件缺少窗口标识或类型", component="maintenance")
        with self._lock:
            seq = self._count + 1
            record = {
                "seq": seq,
                "window_id": window_id,
                "type": etype,
                "at": float(at),
                "actor": str(actor),
                "base_hash": self._tail_hash,
                "data": dict(data),
            }
            record["hash"] = hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()
            self._journal.append(record)
            self._tail_hash = record["hash"]
            self._count = seq
            return dict(record)

    def read_all(self) -> list[dict]:
        return [dict(item) for item in self._journal.read_all()]

    def replay(self) -> dict[str, Window]:
        """把整条流水重放成窗口当前态。"""

        windows: dict[str, Window] = {}
        for record in self.read_all():
            self._apply(windows, record)
        return windows

    def verify(self) -> dict:
        """重放并校验哈希链，返回首处断裂位置（无断裂时 ``ok`` 为真）。"""

        previous = GENESIS
        windows: dict[str, Window] = {}
        expected_seq = 0
        for record in self.read_all():
            expected_seq += 1
            stored = str(record.get("hash", ""))
            payload = {key: value for key, value in record.items() if key != "hash"}
            digest = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
            if int(record.get("seq", 0)) != expected_seq:
                return {"ok": False, "reason": "sequence_gap", "at_seq": record.get("seq")}
            if str(record.get("base_hash", "")) != previous:
                return {"ok": False, "reason": "chain_broken", "at_seq": record.get("seq")}
            if digest != stored:
                return {"ok": False, "reason": "hash_mismatch", "at_seq": record.get("seq")}
            previous = stored
            self._apply(windows, record, verify=True)
        return {"ok": True, "events": expected_seq, "windows": len(windows), "tail": previous}

    # ---- 重放规则 -----------------------------------------------------

    def _apply(self, windows: dict[str, Window], record: dict, *, verify: bool = False) -> None:
        window_id = str(record.get("window_id", ""))
        etype = str(record.get("type", ""))
        data = record.get("data", {}) if isinstance(record.get("data"), dict) else {}
        window = windows.get(window_id)

        if etype == "requested":
            window = Window(
                id=window_id,
                station_id=str(data.get("station_id", "")),
                group_id=str(data.get("group_id", "")),
                start=float(data.get("start", 0.0)),
                end=float(data.get("end", 0.0)),
                applicant=str(data.get("applicant", "")),
                status=REQUESTED,
                kind=str(data.get("kind", "routine")),
                reason=str(data.get("reason", "")),
                ticket=str(data.get("ticket", "")),
                note=str(data.get("note", "")),
                idem_key=str(data.get("idem_key", "")),
                created_at=float(record.get("at", 0.0)),
                updated_at=float(record.get("at", 0.0)),
            )
            windows[window_id] = window
        elif window is None:
            raise RailError(f"事件 {etype} 引用了不存在的窗口 {window_id}", component="maintenance")
        elif etype in ("approved",):
            window.status = APPROVED
        elif etype in ("rejected",):
            window.status = REJECTED
            window.note = str(data.get("note", window.note))
        elif etype in ("cancelled",):
            window.status = CANCELLED
            window.note = str(data.get("note", window.note))
        elif etype in ("started", "active"):
            window.status = ACTIVE
        elif etype in ("expired",):
            window.status = EXPIRED
        elif etype in ("released", "force_released"):
            window.status = RELEASED
            window.released_by = str(data.get("actor", ""))
            if etype == "force_released":
                window.note = str(data.get("note", window.note))
        elif etype in ("preempted",):
            window.status = PREEMPTED
            window.note = str(data.get("note", window.note))
        elif etype == "escalated":
            window.escalated = True
        elif etype == "extended":
            window.start = float(data.get("start", window.start))
            window.end = float(data.get("end", window.end))
        else:
            if verify:
                raise RailError(f"未知的窗口事件类型: {etype}", component="maintenance")
        if window is not None:
            window.updated_at = float(record.get("at", window.updated_at))
            window.seq = int(record.get("seq", window.seq + 1))
