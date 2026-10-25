"""Bounded local retention and storage statistics for the management database."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import sqlite3

from wxhook_api.core.persistence import finish_before_cancel


def _days(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a nonnegative integer") from None
    if value < 0 or value > 36500:
        raise ValueError(f"{name} must be between 0 and 36500")
    return value


def _interval() -> float:
    try:
        value = float(os.getenv("WXHOOK_RETENTION_INTERVAL", "86400"))
    except ValueError:
        raise ValueError("WXHOOK_RETENTION_INTERVAL must be a number") from None
    if not 300 <= value <= 30 * 86400:
        raise ValueError("WXHOOK_RETENTION_INTERVAL must be between 300 and 2592000 seconds")
    return value


class RetentionService:
    """Delete only terminal rows older than the configured windows.

    Chat retention is opt-in (0 keeps messages forever). Reply claims, finished
    tasks and terminal reminders can be bounded by default because their live
    counterparts and summaries are kept separately.
    """

    TABLES = ("groups", "members", "chat_messages", "chat_reply_claims", "group_tasks", "member_reminders")

    def __init__(self, groups, tools, *, chat_days=None, claim_days=None, task_days=None, reminder_days=None, interval=None, now=None):
        self.groups, self.tools = groups, tools
        self.chat_days = _days("WXHOOK_CHAT_RETENTION_DAYS", 0) if chat_days is None else max(0, int(chat_days))
        self.claim_days = _days("WXHOOK_CLAIM_RETENTION_DAYS", 90) if claim_days is None else max(0, int(claim_days))
        self.task_days = _days("WXHOOK_TASK_RETENTION_DAYS", 180) if task_days is None else max(0, int(task_days))
        self.reminder_days = _days("WXHOOK_REMINDER_RETENTION_DAYS", 180) if reminder_days is None else max(0, int(reminder_days))
        self.interval = _interval() if interval is None else max(300.0, float(interval))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._task = None
        self._lock = asyncio.Lock()
        self.status = {"last_run_at": "", "last_error": "", "deleted": {}, "database": {}}

    def snapshot(self):
        return {
            "enabled": any((self.chat_days, self.claim_days, self.task_days, self.reminder_days)),
            "chat_days": self.chat_days,
            "claim_days": self.claim_days,
            "task_days": self.task_days,
            "reminder_days": self.reminder_days,
            "interval": self.interval,
            **self.status,
        }

    async def run_once(self):
        async with self._lock:
            current = self._now()

            def cutoff(days: int) -> str:
                return (current - timedelta(days=days)).isoformat()

            def prune(db):
                deleted = {}
                if self.chat_days:
                    deleted["chat_messages"] = db.execute(
                        "DELETE FROM chat_messages WHERE timestamp<?",
                        (int((current - timedelta(days=self.chat_days)).timestamp()),)).rowcount
                if self.claim_days:
                    deleted["chat_reply_claims"] = db.execute(
                        "DELETE FROM chat_reply_claims WHERE updated_at<?", (cutoff(self.claim_days),)).rowcount
                if self.task_days:
                    deleted["group_tasks"] = db.execute(
                        "DELETE FROM group_tasks WHERE status IN ('done','cancelled') AND updated_at<?",
                        (cutoff(self.task_days),)).rowcount
                if self.reminder_days:
                    deleted["member_reminders"] = db.execute(
                        "DELETE FROM member_reminders WHERE status IN ('sent','failed','cancelled','unknown','skipped') AND updated_at<?",
                        (cutoff(self.reminder_days),)).rowcount
                # Let SQLite refresh planner statistics without a full VACUUM.
                db.execute("PRAGMA optimize")
                page_count = db.execute("PRAGMA page_count").fetchone()[0]
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                database = {"size_bytes": page_count * page_size}
                for table in self.TABLES:
                    database[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                return {"deleted": deleted, "database": database}

            async def commit():
                try:
                    result = await self.tools.db(prune)
                except (OSError, sqlite3.Error):
                    self.status["last_error"] = "无法清理本地数据，请检查数据库写入权限和磁盘空间。"
                    raise
                self.status.update(last_run_at=current.isoformat(), last_error="", **result)
                return result

            return await finish_before_cancel(commit())

    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        # Give the API and callback dispatcher time to start before scanning a
        # large existing database; subsequent runs are daily by default.
        try:
            await asyncio.sleep(5)
            while True:
                delay = self.interval
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.status["last_error"] = "数据清理暂时失败，将稍后重试。"
                    delay = 60
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await finish_before_cancel(self._join())

    async def _join(self):
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
