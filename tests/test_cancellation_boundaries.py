import asyncio
import threading

import pytest

from tests.test_chatbot import context
from tests.test_douyin import GROUP
from tests.test_management_regressions import seed_tasks
from tests.test_media_cleanup import NOW, seed, service_for


@pytest.mark.parametrize("fail", [False, True])
async def test_cancelled_task_creation_holds_capacity_lock_until_commit(tmp_path, monkeypatch, fail):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.reminders.stop()
        tools._scheduler.cancel()
        await asyncio.gather(tools._scheduler, return_exceptions=True)
        await seed_tasks(tools, GROUP, "open", 999)
        entered, release = threading.Event(), threading.Event()
        original = tools.check_task_capacity

        def delayed(db, group):
            original(db, group)
            entered.set()
            assert release.wait(5)
            if fail:
                raise OSError("synthetic database write failure")

        monkeypatch.setattr(tools, "check_task_capacity", delayed)
        pending = asyncio.create_task(tools.save_task(GROUP, dict(title="first", owner="")))
        following = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            monkeypatch.setattr(tools, "check_task_capacity", original)
            following = asyncio.create_task(tools.save_task(GROUP, dict(title="second", owner="")))
            await asyncio.sleep(0)
            assert tools.groups._lock.locked() and not pending.done() and not following.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 3)
            if fail:
                await asyncio.wait_for(following, 3)
            else:
                with pytest.raises(ValueError, match="1000"):
                    await asyncio.wait_for(following, 3)
            tasks = (await tools.snapshot(GROUP))["tasks"]
            assert len(tasks) == 1000
            assert sum(task["title"] == ("second" if fail else "first") for task in tasks) == 1
        finally:
            release.set()
            await asyncio.gather(*(task for task in (pending, following) if task), return_exceptions=True)


@pytest.mark.parametrize("fail", [False, True])
async def test_repeated_cleanup_cancellation_keeps_file_reads_blocked(tmp_path, monkeypatch, fail):
    import wxhook_api.services.douyin as module

    folder, job = seed(tmp_path)
    service = service_for(tmp_path)
    service._jobs[job["id"]] = job
    revision = service.revision
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = module.cleanup_media

    def delayed(*args):
        entered.set()
        try:
            assert release.wait(5)
            if fail:
                raise OSError("synthetic cleanup failure")
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(module, "cleanup_media", delayed)
    pending = asyncio.create_task(service.cleanup_expired(now=NOW))
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        following = asyncio.create_task(service.acquire_file(job["id"], "001.webp"))
        await asyncio.sleep(0)
        assert service._maintenance_lock.locked() and not pending.done() and not following.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        path = await asyncio.wait_for(following, 3)
        assert finished.is_set()
        if fail:
            assert path == folder / "001.webp" and path.is_file()
            assert service.revision == revision
        else:
            assert path is None and not (folder / "001.webp").exists()
            assert service.snapshot()["jobs"][0]["files_cleaned_at"]
            assert service.cleanup_status["deleted_files"] == 4 and service.revision != revision
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 3)
        await asyncio.gather(*(task for task in (pending, following) if task), return_exceptions=True)
        service.release_file(job["id"])
