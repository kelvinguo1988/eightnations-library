"""限速与配额：每源每小时册数上限 + 进度回调。调度器(M2)与手动 fetch 共用。"""
import threading
import time
from collections import deque
from typing import Callable, Dict, Optional


class HourQuota:
    """滑动 1 小时窗口内每源启动的册数上限（进程内存态；重启即清零，可接受）。"""

    def __init__(self, default_quota: int = 10):
        self.default_quota = default_quota
        self._started: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, source_id: str, quota: Optional[int] = None) -> bool:
        q = self.default_quota if quota is None else quota
        if q <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            dq = self._started.setdefault(source_id, deque())
            while dq and now - dq[0] > 3600:
                dq.popleft()
            if len(dq) >= q:
                return False
            dq.append(now)
            return True

    def used(self, source_id: str) -> int:
        now = time.monotonic()
        with self._lock:
            dq = self._started.get(source_id, deque())
            while dq and now - dq[0] > 3600:
                dq.popleft()
            return len(dq)


class Progress:
    """下载进度回调句柄：适配器在页/卷完成时调用 tick()。"""

    def __init__(self, callback: Optional[Callable[[int, int], None]] = None,
                 total: int = 0):
        self.callback = callback
        self.done = 0
        self.total = total

    def tick(self, n: int = 1, total: Optional[int] = None) -> None:
        self.done += n
        if total is not None:
            self.total = total
        if self.callback:
            try:
                self.callback(self.done, self.total)
            except Exception:
                pass
