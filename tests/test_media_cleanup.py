import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import uuid4

from asgi_lifespan import LifespanManager
import httpx
import pytest

from wxhook_api.core.douyin_config import DouyinOptions, GroupSettings, MediaRetention, load_douyin_options, load_media_retention, save_group_settings
from wxhook_api.main import create_app
from wxhook_api.routes.douyin import LeasedFileResponse
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.media_cleanup import CHINA, CleanupReport, cleanup_media, next_cleanup_at, regular


NOW = datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
POLICY = MediaRetention()


def seed(root, *, age=timedelta(days=2), delivery="sent", status="completed", finished=True, converted=True):
    folder = root / uuid4().hex
    folder.mkdir(parents=True)
    end = NOW - age
    job = {"id": folder.name, "created_at": (end - timedelta(minutes=20)).isoformat(), "status": status,
           "delivery_status": delivery, "group_id": "test@chatroom", "sender": "wxid_test", "msgid": "1",
           "files": [{"name": "001.webp", "kind": "image", "size": 5, "send_status": "sent" if delivery == "sent" else "failed"}]}
    if finished:
        job["finished_at"] = end.isoformat()
    names = ["001.webp"] + (["send-001.png", "send-001.png.part", "002.part"] if converted else [])
    for name in names:
        path = folder / name
        path.write_bytes(b"media")
        os.utime(path, (end.timestamp(), end.timestamp()))
    record = folder / "result.json"
    record.write_text(json.dumps(job), encoding="utf-8")
    os.utime(record, (end.timestamp(), end.timestamp()))
    return folder, job


def service_for(root, *, enabled=True):
    return DouyinService(DouyinOptions(GroupSettings(), root.parent / "config.yml", root, retention=MediaRetention(enabled=enabled)))


def create_reparse_link(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        if os.name != "nt":
            pytest.skip("This filesystem cannot create links")
        # Directory junctions exercise the real Windows reparse-point boundary
        # without requiring the privilege needed for symbolic links.
        import _winapi
        try:
            _winapi.CreateJunction(str(target), str(link))
        except OSError:
            pytest.skip("This Windows filesystem cannot create reparse points")


@pytest.mark.parametrize("delivery,hours,deleted", [("sent", 23.99, False), ("sent", 24, True), ("failed", 71.99, False),
    ("failed", 72, True), ("unknown", 48, False), ("unknown", 72, True), ("partial", 48, False),
    ("partial", 72, True), ("disabled", 48, False), ("disabled", 72, True), ("skipped", 72, True)])
def test_media_retention_boundaries_and_all_delivery_outcomes(tmp_path, delivery, hours, deleted):
    folder, _ = seed(tmp_path, age=timedelta(hours=hours), delivery=delivery)
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert (folder / "001.webp").exists() is not deleted
    assert (folder / "send-001.png").exists() is not deleted
    assert (folder / "result.json").exists()
    assert report.deleted_files == (4 if deleted else 0)
    assert report.bytes_freed == (20 if deleted else 0)
    if deleted:
        job = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        assert job["files_cleaned_at"] == NOW.isoformat() and job["files"][0]["cleaned"]


def test_finish_time_and_recent_media_write_protect_old_jobs(tmp_path):
    folder, job = seed(tmp_path, age=timedelta(days=10))
    job["finished_at"] = (NOW - timedelta(hours=2)).isoformat()
    (folder / "result.json").write_text(json.dumps(job))
    assert cleanup_media(tmp_path, POLICY, set(), NOW).deleted_files == 0
    job["finished_at"] = (NOW - timedelta(days=10)).isoformat()
    (folder / "result.json").write_text(json.dumps(job))
    os.utime(folder / "send-001.png", ((NOW-timedelta(hours=1)).timestamp(),)*2)
    assert cleanup_media(tmp_path, POLICY, set(), NOW).deleted_files == 0


def test_sent_status_without_all_file_confirmations_uses_three_days(tmp_path):
    folder, job = seed(tmp_path, age=timedelta(hours=48))
    job["files"][0]["send_status"] = "unknown"
    (folder / "result.json").write_text(json.dumps(job))
    assert cleanup_media(tmp_path, POLICY, set(), NOW).deleted_files == 0
    assert (folder / "001.webp").exists()


def test_every_historical_directory_is_scanned_not_just_the_recent_hundred(tmp_path):
    for _ in range(105):
        seed(tmp_path, converted=False)
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert report.deleted_files == 105
    assert len(report.updated) == 105
    assert len(list(tmp_path.glob("*/result.json"))) == 105


async def test_rewriting_old_records_does_not_hide_recent_tasks_after_restart(tmp_path):
    for _ in range(105):
        folder, _ = seed(tmp_path, converted=False)
        # Simulate recently rewritten cleanup metadata for old tasks.
        os.utime(folder / "result.json", (NOW.timestamp(), NOW.timestamp()))
    folder, newest = seed(tmp_path, age=timedelta(hours=12), converted=False)
    service = service_for(tmp_path, enabled=False)
    await asyncio.to_thread(service._load_saved)
    jobs = service.snapshot()["jobs"]
    assert len(jobs) == 100 and jobs[0]["id"] == newest["id"]


def test_legacy_record_anchor_survives_cleanup_metadata_rewrites(tmp_path):
    folder, _ = seed(tmp_path, finished=False)
    cleanup_media(tmp_path, POLICY, set(), NOW)
    saved = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(saved["finished_at"]) == NOW - timedelta(days=2)
    report = cleanup_media(tmp_path, POLICY, set(), NOW + timedelta(days=29))
    assert report.deleted_records == 1 and not folder.exists()


def test_thirty_day_record_expiry_preserves_unrelated_user_files(tmp_path):
    folder, job = seed(tmp_path, age=timedelta(days=30))
    personal = folder / "notes.txt"
    personal.write_text("keep my file")
    unusual = folder / "١٢٣.mp4"
    unusual.write_bytes(b"user file, not a generated ASCII filename")
    other = tmp_path / "personal"
    other.mkdir()
    (other / "001.mp4").write_bytes(b"untouched")
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert job["id"] in report.removed and report.deleted_records == 1
    assert not (folder / "result.json").exists()
    assert personal.read_text() == "keep my file"
    assert unusual.read_bytes() == b"user file, not a generated ASCII filename"
    assert (other / "001.mp4").read_bytes() == b"untouched"


def test_corrupt_or_mismatched_records_are_never_deleted(tmp_path):
    folder, _ = seed(tmp_path, age=timedelta(days=40))
    (folder / "result.json").write_text('{"id":"not-this-directory"}')
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert report.skipped_unsafe == 1
    assert (folder / "001.webp").exists() and (folder / "result.json").exists()


def test_hardlinked_temporary_metadata_cannot_overwrite_external_files(tmp_path):
    folder, _ = seed(tmp_path)
    outside = tmp_path / "my-settings.json"
    outside.write_text("do not change")
    os.link(outside, folder / "result.json.tmp")
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert report.skipped_unsafe == 1 and report.deleted_files == 0
    assert outside.read_text() == "do not change"


def test_symlinked_task_folder_cannot_escape_the_download_root(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    outside, _ = seed(tmp_path / "outside")
    link = root / outside.name
    create_reparse_link(link, outside, directory=True)
    report = cleanup_media(root, POLICY, set(), NOW)
    assert report.skipped_unsafe == 1 and report.deleted_files == 0
    assert (outside / "001.webp").read_bytes() == b"media"


def test_broken_temporary_symlink_cannot_create_a_file_outside_the_task(tmp_path):
    folder, _ = seed(tmp_path)
    outside = tmp_path / "must-not-be-created.json"
    create_reparse_link(folder / "result.json.tmp", outside)
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert report.skipped_unsafe == 1 and not outside.exists()
    assert (folder / "001.webp").exists()


def test_windows_reparse_point_is_rejected_without_following_it():
    path = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400))
    assert not regular(path, directory=True)


def test_locked_media_is_reported_and_retried_without_postponing_record_expiry(tmp_path, monkeypatch):
    folder, _ = seed(tmp_path)
    original = Path.unlink

    def locked(path, *args, **kwargs):
        if path.name == "001.webp":
            raise PermissionError("file locked")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert report.errors == 1 and report.deleted_files == 3
    saved = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    assert saved["cleanup_error"] and not saved.get("files_cleaned_at")
    monkeypatch.setattr(Path, "unlink", original)
    assert cleanup_media(tmp_path, POLICY, set(), NOW+timedelta(hours=1)).deleted_files == 1
    saved = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    assert saved["files_cleaned_at"] and not saved.get("cleanup_error")


@pytest.mark.parametrize("status", ["queued", "parsing", "downloading", "sending"])
async def test_active_jobs_are_skipped_even_when_very_old(tmp_path, status):
    folder, job = seed(tmp_path, age=timedelta(days=40), status=status)
    service = service_for(tmp_path)
    service._jobs[job["id"]] = job
    report = await service.cleanup_expired(now=NOW)
    assert report.skipped_active == 1 and report.deleted_records == 0
    assert (folder / "001.webp").exists()


def test_abandoned_previous_process_job_can_eventually_be_cleaned(tmp_path):
    folder, _ = seed(tmp_path, age=timedelta(days=4), status="sending", delivery="sending")
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert report.deleted_files == 4
    saved = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "interrupted" and saved["delivery_status"] == "unknown"


async def test_live_file_lease_defers_cleanup_until_response_finishes(tmp_path):
    folder, job = seed(tmp_path)
    service = service_for(tmp_path)
    service._jobs[job["id"]] = job
    assert await service.acquire_file(job["id"], "001.webp") == folder / "001.webp"
    assert (await service.cleanup_expired(now=NOW)).skipped_active == 1
    assert (folder / "001.webp").exists()
    service.release_file(job["id"])
    assert (await service.cleanup_expired(now=NOW)).deleted_files == 4
    assert service.file_path(job["id"], "001.webp") is None
    assert service.snapshot()["jobs"][0]["files_cleaned_at"]
    assert (await service.cleanup_expired(now=NOW)).deleted_files == 0


async def test_expired_records_are_removed_from_the_in_memory_resource_list(tmp_path):
    folder, job = seed(tmp_path, age=timedelta(days=30))
    service = service_for(tmp_path)
    service._jobs[job["id"]] = job
    report = await service.cleanup_expired(now=NOW)
    assert report.deleted_records == 1 and not folder.exists()
    assert service.snapshot()["jobs"] == []


async def test_metadata_write_failure_does_not_leave_broken_previews_and_recovers(tmp_path, monkeypatch):
    folder, job = seed(tmp_path)
    service = service_for(tmp_path)
    service._jobs[job["id"]] = job
    original = Path.write_text

    def denied(path, *args, **kwargs):
        if path.name == "result.json.tmp":
            raise PermissionError("write denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", denied)
    result = await service.cleanup_expired(now=NOW)
    assert result.errors == 1 and result.deleted_files == 4
    assert service.snapshot()["jobs"][0]["files_cleaned_at"]
    assert service.snapshot()["jobs"][0]["cleanup_error"]
    assert not json.loads((folder / "result.json").read_text(encoding="utf-8")).get("files_cleaned_at")
    monkeypatch.setattr(Path, "write_text", original)
    assert (await service.cleanup_expired(now=NOW)).errors == 0
    assert not service.snapshot()["jobs"][0].get("cleanup_error")
    assert json.loads((folder / "result.json").read_text(encoding="utf-8"))["files_cleaned_at"]


async def test_actual_file_response_protects_files_until_the_body_is_sent(tmp_path, monkeypatch):
    from fastapi.responses import FileResponse
    folder, job = seed(tmp_path / "downloads")
    service = service_for(folder.parent, enabled=False)
    app = create_app(douyin_service=service)
    entered, release = asyncio.Event(), asyncio.Event()
    original = FileResponse.__call__

    async def slow_response(response, scope, receive, send):
        async def slow_send(message):
            if message["type"] == "http.response.body":
                entered.set()
                await release.wait()
            await send(message)
        await original(response, scope, receive, slow_send)

    monkeypatch.setattr(FileResponse, "__call__", slow_response)
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request = asyncio.create_task(client.get(f'/api/douyin/files/{job["id"]}/001.webp'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            service.options = replace(service.options, retention=POLICY)
            assert (await service.cleanup_expired(now=NOW)).skipped_active == 1
            release.set()
            assert (await request).content == b"media"
            assert service._file_leases == {}
            assert (await service.cleanup_expired(now=NOW)).deleted_files == 4
        finally:
            release.set()
            await request


async def test_cleaned_file_api_returns_gone_and_restart_keeps_cleanup_state(tmp_path):
    folder, job = seed(tmp_path / "downloads")
    service = service_for(folder.parent, enabled=False)
    app = create_app(douyin_service=service)
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f'/api/douyin/files/{job["id"]}/001.webp'
        assert (await client.get(url)).content == b"media"
        assert service._file_leases == {}
        service.options = replace(service.options, retention=POLICY)
        await service.cleanup_expired(now=NOW)
        assert (await client.get(url)).status_code == 410
        assert (await client.get(url.replace("001.webp", "999.png"))).status_code == 404
        result = (await client.get("/api/douyin/jobs")).json()
        assert result["jobs"][0]["files_cleaned_at"] and result["cleanup"]["deleted_files"] == 4
    restarted = service_for(folder.parent, enabled=False)
    await restarted.start()
    try:
        assert restarted.snapshot()["jobs"][0]["files_cleaned_at"]
        assert restarted.file_path(job["id"], "001.webp") is None
    finally:
        await restarted.stop()


async def test_file_response_cancellation_always_releases_the_lease(tmp_path, monkeypatch):
    from fastapi.responses import FileResponse
    released = []

    async def cancelled(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(FileResponse, "__call__", cancelled)
    response = LeasedFileResponse(tmp_path / "x", release=lambda: released.append(True))
    with pytest.raises(asyncio.CancelledError):
        await response({}, None, None)
    assert released == [True]


@pytest.mark.parametrize("instant,expected", [
    ("2026-10-16T18:59:00+00:00", "2026-10-17T03:00:00+08:00"),
    ("2026-10-16T19:00:00+00:00", "2026-10-18T03:00:00+08:00"),
    ("2026-10-16T13:00:00-07:00", "2026-10-18T03:00:00+08:00"),
])
def test_schedule_uses_beijing_time_independent_of_host_timezone(instant, expected):
    assert next_cleanup_at(datetime.fromisoformat(instant)).isoformat() == expected


async def test_scheduler_catches_up_at_start_then_runs_at_three_am(tmp_path, monkeypatch):
    import wxhook_api.services.douyin as module
    clock = [datetime(2026, 10, 16, 18, 59, 30, tzinfo=timezone.utc)]
    calls = []
    service = service_for(tmp_path)

    async def sweep():
        calls.append(clock[0])
        if len(calls) == 2:
            raise asyncio.CancelledError

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(service, "cleanup_expired", sweep)
    monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await service._run_cleanup()
    assert len(calls) == 2 and calls[1].astimezone(CHINA).hour == 3


async def test_shutdown_waits_for_running_filesystem_cleanup(tmp_path, monkeypatch):
    import wxhook_api.services.douyin as module
    entered, release = threading.Event(), threading.Event()
    finished = threading.Event()

    def sweep(*args):
        entered.set()
        release.wait(2)
        finished.set()
        return CleanupReport()

    monkeypatch.setattr(module, "cleanup_media", sweep)
    service = service_for(tmp_path)
    await service.start()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        stopped = asyncio.create_task(service.stop())
        await asyncio.sleep(0)
        assert not stopped.done()
        release.set()
        await asyncio.wait_for(stopped, 2)
        assert finished.is_set() and service._cleanup_worker is None
    finally:
        release.set()
        await service.stop()


def test_retention_defaults_and_config_persistence(tmp_path, monkeypatch):
    monkeypatch.delenv("WXHOOK_MEDIA_CLEANUP_ENABLED")
    assert load_media_retention() == POLICY
    target = tmp_path / "douyin.yml"
    target.write_text("cleanup-enabled: false\ncleanup-hour: 4\n")
    monkeypatch.setenv("WXHOOK_DOUYIN_CONFIG", str(target))
    options = load_douyin_options()
    assert not options.retention.enabled and options.retention.hour == 4
    save_group_settings(options, GroupSettings())
    assert load_douyin_options().retention == options.retention
    monkeypatch.setenv("WXHOOK_MEDIA_CLEANUP_ENABLED", "true")
    assert load_douyin_options().retention.enabled


@pytest.mark.parametrize("values", [{"cleanup-hour": -1}, {"cleanup-hour": 24}, {"cleanup-hour": True}, {"cleanup-enabled": "yes"}])
def test_invalid_cleanup_configuration_is_rejected(values, monkeypatch):
    monkeypatch.delenv("WXHOOK_MEDIA_CLEANUP_ENABLED")
    with pytest.raises(ValueError):
        load_media_retention(values)
