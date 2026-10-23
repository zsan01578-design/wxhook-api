from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.media_cleanup import safe_child
from wxhook_api.services.storage_guard import StorageGuard


class BackupService:
    """Online SQLite snapshots with bounded retention and independently saved configs."""

    def __init__(self, groups: GroupManager, configs: dict[str, Path], *, databases: dict[str, Path] | None = None):
        self.groups, self.configs = groups, configs
        self.databases = {"management.sqlite3": groups.path, **(databases or {})}
        self.enabled = os.getenv("WXHOOK_BACKUP_ENABLED", "true").lower() not in {"false", "0", "off"}
        self.directory = Path(os.getenv("WXHOOK_BACKUP_DIR") or groups.path.parent / "backups").resolve()
        self.interval = float(os.getenv("WXHOOK_BACKUP_INTERVAL", "86400"))
        self.keep = int(os.getenv("WXHOOK_BACKUP_KEEP", "7"))
        if not math.isfinite(self.interval) or self.interval < 300 or not 1 <= self.keep <= 30:
            raise ValueError("Backup interval must be at least 300 seconds; keep must be 1-30")
        identity = hashlib.sha256(str(groups.path.resolve()).casefold().encode()).hexdigest()[:12]
        self.prefix = f"wxhook-{identity}-"
        self.storage = StorageGuard(self.directory, env="WXHOOK_BACKUP_MIN_FREE_MB")
        self._task = None
        self._lock = asyncio.Lock()
        self._discovered_file = ""
        self.status = {"enabled": self.enabled, "last_success_at": "", "last_error": "", "last_file": "", "cleanup_error": ""}

    def snapshot(self):
        return {**self.status, "running": self._task is not None and not self._task.done(),
                "retained_limit": self.keep, "storage": self.storage.snapshot()}

    def _files(self):
        if not self.directory.is_dir():
            return []
        pattern = re.compile(re.escape(self.prefix) + r"\d{8}T\d{12}Z-[a-f0-9]{32}\.zip")
        return sorted((p for p in self.directory.iterdir() if pattern.fullmatch(p.name)
                       and safe_child(p, self.directory)), key=lambda p: p.name)

    def _existing(self, path: Path) -> dict:
        # A filename alone does not establish that a previous ZIP is usable.
        if not safe_child(path, self.directory):
            raise OSError("Unsafe backup path")
        with ZipFile(path) as bundle:
            if bundle.testzip() is not None or "management.sqlite3" not in bundle.namelist():
                raise BadZipFile("Invalid backup archive")
            entry = bundle.getinfo("manifest.json")
            if entry.file_size > 1024 * 1024:
                raise BadZipFile("Invalid backup manifest")
            manifest = json.loads(bundle.read(entry))
            if manifest.get("version") != 1:
                raise ValueError("Invalid backup manifest")
            created = datetime.fromisoformat(manifest["created_at"])
            if created.tzinfo is None:
                raise ValueError("Invalid backup timestamp")
        return {"last_success_at": created.astimezone(timezone.utc).isoformat(), "last_file": path.name}

    async def _discover(self, path: Path):
        async with self._lock:
            if self._discovered_file == path.name or self.status["last_file"] == path.name:
                return
            async def discover():
                try:
                    restored = await asyncio.to_thread(self._existing, path)
                except (OSError, BadZipFile, ValueError, KeyError, TypeError, AttributeError, OverflowError):
                    self.status["last_error"] = "最近备份未通过完整性检查，请检查备份文件；自动备份将在周期到期后继续。"
                    return
                self.status.update(restored, last_error="")
                self._discovered_file = path.name
            await finish_before_cancel(discover())

    @staticmethod
    def _database(source: Path, target: Path):
        deadline = time.monotonic() + 30
        def progress(*args):
            if time.monotonic() > deadline:
                raise TimeoutError("Backup exceeded time limit")
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=128, progress=progress, sleep=0.05)
            if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise sqlite3.DatabaseError("Backup validation failed")

    def _create(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        final = self.directory / f"{self.prefix}{stamp}-{uuid4().hex}.zip"
        temporary = []
        def scratch(suffix):
            fd, name = tempfile.mkstemp(dir=self.directory, suffix=suffix)
            os.close(fd)
            path = Path(name).resolve()
            if path.parent != self.directory:
                raise OSError("Invalid temporary backup path")
            temporary.append(path)
            return path
        try:
            archive = scratch(".zip.part")
            manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "sources": {}}
            with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=1) as bundle:
                for name, source in self.databases.items():
                    if not source.is_file():
                        continue
                    target = scratch(".sqlite3.part")
                    self._database(source, target)
                    bundle.write(target, name)
                    manifest["sources"][name] = str(source.resolve())
                    target.unlink()
                for name, source in self.configs.items():
                    if source.is_file():
                        if source.stat().st_size > 8 * 1024 * 1024:
                            raise OSError("Configuration exceeds backup limit")
                        bundle.writestr("config/" + name, source.read_bytes())
                        manifest["sources"]["config/" + name] = str(source.resolve())
                bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            with ZipFile(archive) as bundle:
                if bundle.testzip() is not None or "management.sqlite3" not in bundle.namelist():
                    raise sqlite3.DatabaseError("Backup archive validation failed")
            with archive.open("r+b") as saved:
                os.fsync(saved.fileno())
            archive.replace(final)
            return final
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)

    def _prune(self):
        # A locked file must not block other old snapshots. Keep the newest
        # configured number even when extra, undeletable files remain.
        failures = 0
        for old in self._files()[:-self.keep]:
            try:
                if safe_child(old, self.directory):
                    old.unlink()
                else:
                    failures += 1
            except FileNotFoundError:
                continue
            except OSError:
                failures += 1
        return failures

    async def _cleanup(self):
        try:
            failures = await asyncio.to_thread(self._prune)
        except OSError:
            failures = 1
        self.status["cleanup_error"] = ("部分旧备份暂时无法清理，将在 1 分钟后重试；最新备份仍可使用。"
                                        if failures else "")

    async def cleanup(self):
        async with self._lock:
            await finish_before_cancel(self._cleanup())

    async def run_once(self):
        async with self._lock:
            async def backup():
                try:
                    estimate = sum(path.stat().st_size for path in self.databases.values() if path.is_file()) * 2
                    await self.storage.ensure(estimate)
                    path = await asyncio.to_thread(self._create)
                except (OSError, sqlite3.Error, TimeoutError):
                    self.status["last_error"] = "备份失败，请检查磁盘空间、目录权限和数据库；原有备份保留。"
                    raise
                self.status.update(last_success_at=datetime.now(timezone.utc).isoformat(), last_error="", last_file=path.name)
                await self._cleanup()
                return path
            return await finish_before_cancel(backup())

    async def start(self):
        if self.enabled and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        # Check the latest file's age so ordinary restarts do not create another
        # full snapshot or rotate a healthy backup out unnecessarily.
        while True:
            try:
                # Retention can free headroom even when creating the next
                # snapshot fails. It must not depend on a successful backup.
                await self.cleanup()
                files = await asyncio.to_thread(self._files)
                if files:
                    await self._discover(files[-1])
                elapsed = time.time() - files[-1].stat().st_mtime if files else self.interval
                if elapsed >= self.interval:
                    await self.run_once()
                delay = self.interval - max(0, elapsed) if elapsed < self.interval else self.interval
                if self.status["cleanup_error"]:
                    delay = min(delay, 60)
            except Exception:
                self.status["last_error"] = "自动备份暂时失败，将在 1 分钟后重试；原有备份保留。"
                delay = 60
            await asyncio.sleep(max(1, delay))

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await finish_before_cancel(self._join())

    async def _join(self):
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
