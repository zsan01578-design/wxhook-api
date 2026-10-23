from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import time


class LowDiskSpace(OSError):
    pass


class StorageGuard:
    """Best-effort headroom for optional large writes, not a filesystem quota."""

    def __init__(self, path: Path, *, env: str = "WXHOOK_MEDIA_MIN_FREE_MB"):
        self.path = path
        mb = int(os.getenv(env, "1024"))
        if mb < 0:
            raise ValueError(f"{env} must be nonnegative")
        self.minimum = mb * 1024 * 1024
        self.free: int | None = None
        self._checked = float("-inf")
        self.error = ""

    def snapshot(self):
        return {"minimum_free_bytes": self.minimum, "free_bytes": self.free,
                "paused": bool(self.error), "error": self.error}

    def _available(self):
        target = self.path.resolve()
        while not target.exists():
            parent = target.parent
            if parent == target:
                raise OSError("Storage drive is unavailable")
            target = parent
        return shutil.disk_usage(target).free

    async def ensure(self, additional: int = 0):
        if time.monotonic() - self._checked >= 1:
            try:
                self.free = await asyncio.to_thread(self._available)
            except OSError:
                self.error = "无法检查磁盘空间，已暂停新增文件，请检查存储目录。"
                raise LowDiskSpace(self.error) from None
            self._checked = time.monotonic()
        if self.free is None or self.free < self.minimum + additional:
            self.error = "磁盘剩余空间不足，已暂停新增文件；释放空间后新任务会自动重新检查。"
            raise LowDiskSpace(self.error)
        self.error = ""

    def written(self, size: int):
        if self.free is not None:
            self.free -= size
