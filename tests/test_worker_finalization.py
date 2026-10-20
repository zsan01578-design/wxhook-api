import asyncio
from dataclasses import replace

import pytest

from tests.test_chatbot import context, event
from tests.test_douyin import GROUP, SHORT, options
from wxhook_api.core.douyin_config import MediaRetention
from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin import DouyinService


async def test_cancelled_reply_finalization_always_releases_group_queue(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        groups = state.app.state.groups
        entered, release = asyncio.Event(), asyncio.Event()
        original = groups.finish_chat_reply
        active = []

        async def delayed(group, key, status):
            if status != "sent":
                return await original(group, key, status)
            active.append(asyncio.current_task())

            async def commit():
                entered.set()
                await release.wait()
                await original(group, key, status)

            await finish_before_cancel(commit())

        monkeypatch.setattr(groups, "finish_chat_reply", delayed)
        try:
            assert (await state.client.post("/receiveChatBotMsg", json=event())).status_code == 200
            await asyncio.wait_for(entered.wait(), 2)
            worker = active[0]
            worker.cancel()
            await asyncio.sleep(0)
            worker.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 3)
            await asyncio.wait_for(state.bot._queue.join(), 1)
            assert GROUP not in state.bot._queue._busy_groups
            assert not state.bot._pending_groups and not state.bot._pending_members
            with groups._connect() as db:
                assert db.execute("SELECT status FROM chat_reply_claims WHERE group_id=?", (GROUP,)).fetchone()[0] == "sent"
            assert len(state.sends) == 1
        finally:
            release.set()
            await asyncio.gather(*active, return_exceptions=True)


@pytest.mark.parametrize("kind", ["media", "notice"])
async def test_cancelling_media_worker_waiting_for_maintenance_releases_queue(tmp_path, kind):
    service = DouyinService(replace(options(tmp_path), retention=MediaRetention(enabled=False)))
    await service.start()
    await service._maintenance_lock.acquire()
    try:
        if kind == "media":
            queue, worker = service._queue, service._worker
            await service.receive(CallbackEvent.model_validate(event(SHORT)))
            job = service.snapshot()["jobs"][0]
        else:
            queue, worker = service._notices, service._notice_worker
            job = dict(id="a" * 32, group_id=GROUP, sender="member", status="failed", files=[])
            service._jobs[job["id"]] = job
            queue.put_nowait((job, "failure", "simulated failure"))
        await asyncio.sleep(0)
        assert queue.qsize() == 0
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2)
        await asyncio.wait_for(queue.join(), 1)
        assert not service._active_jobs
        if kind == "media":
            assert job["status"] == "interrupted"
    finally:
        service._maintenance_lock.release()
        await service.stop()
