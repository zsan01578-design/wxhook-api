import asyncio
import json
import sqlite3
import threading
from unittest.mock import AsyncMock
from zipfile import ZipFile

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tests.test_douyin import GROUP, PNG, options
from wxhook_api.services.backups import BackupService
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.storage_guard import LowDiskSpace, StorageGuard


async def backup_service(tmp_path, monkeypatch):
    monkeypatch.setenv("WXHOOK_BACKUP_DIR", str(tmp_path / "backups"))
    groups = GroupManager(tmp_path / "source.sqlite3")
    await groups.start([GROUP])
    config = tmp_path / "config.yml"
    config.write_text("test-value: synthetic-config\n", encoding="utf-8")
    service = BackupService(groups, {"settings.yml": config})
    return groups, service


async def test_online_backup_restores_database_and_config_without_touching_source(tmp_path, monkeypatch):
    groups, service = await backup_service(tmp_path, monkeypatch)
    await groups.update_group(GROUP, {"note": "saved note", "blocked": True})
    path = await service.run_once()
    with ZipFile(path) as bundle:
        assert bundle.testzip() is None
        assert bundle.read("config/settings.yml") == (tmp_path / "config.yml").read_bytes()
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["version"] == 1
        restored = tmp_path / "restored.sqlite3"
        restored.write_bytes(bundle.read("management.sqlite3"))
    with sqlite3.connect(restored) as db:
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert db.execute("SELECT note,blocked FROM groups WHERE id=?", (GROUP,)).fetchone() == ("saved note", 1)
    assert not groups.allowed(GROUP)
    assert service.snapshot()["last_success_at"] and service.snapshot()["last_error"] == ""
    assert len(service._files()) == 1


async def test_retention_is_scoped_and_failed_backup_keeps_previous_snapshot(tmp_path, monkeypatch):
    groups, service = await backup_service(tmp_path, monkeypatch)
    service.keep = 2
    first = await service.run_once()
    foreign = service.directory / "another-database.zip"
    foreign.write_bytes(b"do not remove")
    second = await service.run_once()
    third = await service.run_once()
    assert not first.exists() and second.exists() and third.exists() and foreign.exists()
    def fail(*args):
        raise sqlite3.DatabaseError("private database location")
    monkeypatch.setattr(service, "_database", fail)
    with pytest.raises(sqlite3.DatabaseError):
        await service.run_once()
    assert second.exists() and third.exists() and foreign.read_bytes() == b"do not remove"
    assert not list(service.directory.glob("*.part"))
    assert "private database" not in service.snapshot()["last_error"]


async def test_low_disk_prevents_backup_and_recovers_on_new_check(tmp_path, monkeypatch):
    groups, service = await backup_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service.storage, "_available", lambda: 0)
    with pytest.raises(LowDiskSpace):
        await service.run_once()
    assert service._files() == [] and groups.allowed(GROUP)
    monkeypatch.setattr(service.storage, "_available", lambda: 10**12)
    service.storage._checked = float("-inf")
    assert (await service.run_once()).is_file()
    assert service.status["last_error"] == ""


async def test_cancelled_backup_waits_for_writer_and_publishes_completed_result(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = service._create
    def pause():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(service, "_create", pause)
    task = asyncio.create_task(service.run_once())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert service._lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert len(service._files()) == 1 and service.status["last_success_at"]
        assert not service._lock.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_backup_scheduler_retries_faults_without_exiting(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    service.run_once = AsyncMock(side_effect=[OSError("private source"), None])
    delays = []
    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError
    monkeypatch.setattr("wxhook_api.services.backups.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await service._run()
    assert service.run_once.await_count == 2
    assert delays == [60, service.interval]


async def test_media_low_disk_stops_before_network_and_recovers(tmp_path, monkeypatch):
    calls = []
    def respond(request):
        calls.append(request.url)
        return httpx.Response(200, content=PNG)
    service = DouyinService(options(tmp_path), transport=httpx.MockTransport(respond))
    await service.start()
    folder = tmp_path / "asset"
    folder.mkdir()
    monkeypatch.setattr(service.storage, "_available", lambda: 0)
    try:
        with pytest.raises(LowDiskSpace):
            await service._download("https://p3.douyinpic.com/test.png", "image", folder, 1, GROUP, 100000)
        assert calls == [] and list(folder.iterdir()) == []
        monkeypatch.setattr(service.storage, "_available", lambda: 10**12)
        service.storage._checked = float("-inf")
        result = await service._download("https://p3.douyinpic.com/test.png", "image", folder, 1, GROUP, 100000)
        assert result["size"] == len(PNG) and (folder / "001.png").read_bytes() == PNG
        assert not service.storage.snapshot()["paused"]
    finally:
        await service.stop()


async def test_unknown_length_media_cleans_partial_when_headroom_runs_out(tmp_path, monkeypatch):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield PNG + bytes(65536 - len(PNG))
            yield bytes(65536)
    service = DouyinService(options(tmp_path), transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream())))
    monkeypatch.setattr(service.storage, "_available", lambda: service.storage.minimum + 65536)
    await service.start()
    folder = tmp_path / "asset"
    folder.mkdir()
    try:
        with pytest.raises(LowDiskSpace):
            await service._download("https://p3.douyinpic.com/test.png", "image", folder, 1, GROUP, 200000)
        assert list(folder.iterdir()) == []
    finally:
        await service.stop()


async def test_unavailable_drive_fails_without_looping_at_missing_root(tmp_path):
    class MissingDrive:
        def resolve(self):
            return self
        def exists(self):
            return False
        @property
        def parent(self):
            return self
    guard = StorageGuard(tmp_path)
    guard.path = MissingDrive()
    with pytest.raises(LowDiskSpace):
        await asyncio.wait_for(guard.ensure(), 1)
    assert guard.snapshot()["paused"]


@pytest.mark.parametrize("name,value", [("WXHOOK_BACKUP_INTERVAL", "nan"), ("WXHOOK_BACKUP_INTERVAL", "1"),
                                        ("WXHOOK_BACKUP_KEEP", "0"), ("WXHOOK_BACKUP_KEEP", "31")])
async def test_invalid_backup_limits_are_rejected(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        BackupService(GroupManager(tmp_path / "db.sqlite3"), {})


async def test_enabled_backup_starts_after_schema_and_finishes_on_shutdown(tmp_path, monkeypatch):
    from dataclasses import replace
    from wxhook_api.main import create_app
    from wxhook_api.core.config import load_settings
    monkeypatch.setenv("WXHOOK_BACKUP_ENABLED", "true")
    monkeypatch.setenv("WXHOOK_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr("wxhook_api.main.PROJECT_ROOT", tmp_path)
    config = tmp_path / "version.yml"
    config.write_text("start-port: 30001\n", encoding="utf-8")
    app = create_app(settings=replace(load_settings(), config_path=config),
        douyin_service=DouyinService(options(tmp_path)),
        hook_transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
    async with LifespanManager(app):
        service = app.state.backups
        task = service._task
        async with asyncio.timeout(3):
            while not service.status["last_success_at"]:
                await asyncio.sleep(0.01)
        archive = service._files()[0]
        with ZipFile(archive) as bundle:
            restored = tmp_path / "restored.sqlite3"
            restored.write_bytes(bundle.read("management.sqlite3"))
        with sqlite3.connect(restored) as db:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='callback_receipts'").fetchone()
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='group_tasks'").fetchone()
    assert task.done() and service._task is None
