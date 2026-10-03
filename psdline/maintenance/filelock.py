"""跨进程提交锁。

同一个存储目录可能被控制服务与命令行进程同时打开（例如服务在跑、值班员用 CLI
批窗口）。进程内用 ``RLock`` 串行化，跨进程靠对一个锁文件 ``flock``：持有期间
做"读冲突 → 写事件 → 写当前态"的完整提交，保证两个并发申请不会都看到对方还没
落盘、最后双双批准。POSIX 以外的平台退化为仅进程内锁。
"""

from __future__ import annotations

import os
import threading

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台
    fcntl = None  # type: ignore[assignment]


class FileLock:
    """锁文件上的排他锁，配合 ``with`` 使用。"""

    def __init__(self, root: str, name: str = "maintenance.lock") -> None:
        os.makedirs(root, exist_ok=True)
        self._path = os.path.join(root, name)
        self._thread_lock = threading.RLock()
        self._handle = None

    def __enter__(self) -> "FileLock":
        self._thread_lock.acquire()
        acquired = False
        try:
            if fcntl is not None and self._handle is None:
                handle = open(self._path, "a+", encoding="utf-8")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                self._handle = handle
            acquired = True
        finally:
            if not acquired:
                # 加锁本身失败：释放已拿到的线程锁，避免线程锁泄漏。
                if self._handle is not None:
                    self._handle.close()
                    self._handle = None
                self._thread_lock.release()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        self._thread_lock.release()
