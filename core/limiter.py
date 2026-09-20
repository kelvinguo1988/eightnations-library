"""下载进度回调句柄。限速/配额已下沉：域名节流见 core/http，
每小时册数配额以 jobs 表为账本在 core/db.claim_and_start 原子消费
（跨进程共享，进程内内存配额在多执行方下会失效）。"""
from typing import Callable, Optional


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
