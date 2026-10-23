import asyncio
from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from asgi_lifespan import LifespanManager
import httpx
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, ITEM, PNG, SHORT, image_item, options, page
from tests.test_wechat_sync import FakeWeChat, ROOM, build
from wxhook_api.clients.hook_client import HookResponse
from wxhook_api.core.douyin_config import MediaRetention
from wxhook_api.core.errors import AccountMismatchError
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.callback_store import CallbackStore
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.events import EventDispatcher
from wxhook_api.services.groups import GroupManager


def event(msgid=1):
    return CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "msgid": msgid, "content": SHORT})


async def receipt_dispatcher(tmp_path):
    groups = GroupManager(tmp_path / "management.sqlite3")
    await groups.start([GROUP])
    store = CallbackStore(groups, capacity=1)
    dispatcher = EventDispatcher(1, workers=1, store=store)
    dispatcher.retry_delays = (0, 0)
    dispatcher.storage_retry_delays = (0.005,)
    return groups, store, dispatcher


async def test_http_readiness_tracks_storage_wait_and_automatic_recovery(monkeypatch):
    broken, restore = asyncio.Event(), asyncio.Event()

    def unexpected_hook(request):
        raise AssertionError("Callback recovery must remain local")

    app = create_app(hook_transport=httpx.MockTransport(unexpected_hook))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        dispatcher = app.state.event_dispatcher
        dispatcher.storage_retry_delays = (0.005,)
        original = dispatcher.store.attempt

        async def attempt(delivery):
            if not restore.is_set():
                broken.set()
                raise OSError("private disk path")
            await original(delivery)

        monkeypatch.setattr(dispatcher.store, "attempt", attempt)
        try:
            assert (await client.post("/receiveChatBotMsg", json=event().model_dump())).status_code == 200
            await asyncio.wait_for(broken.wait(), 2)
            response = await client.get("/ready")
            assert response.status_code == 503
            assert response.json()["processing"]["storage_waiting"] == 1
            assert "private disk path" not in response.text
            assert (await client.post("/receiveChatBotMsg", json=event(2).model_dump())).status_code == 503
            restore.set()
            await asyncio.wait_for(dispatcher._queue.join(), 2)
            assert (await client.get("/ready")).status_code == 200
            assert (await client.post("/receiveChatBotMsg", json=event(2).model_dump())).status_code == 200
            await asyncio.wait_for(dispatcher._queue.join(), 2)
        finally:
            restore.set()


async def test_storage_retries_use_capped_backoff(monkeypatch):
    dispatcher = EventDispatcher()
    operation = AsyncMock(side_effect=[OSError("unavailable")] * 6 + ["saved"])
    delays = []

    async def sleep(delay):
        assert dispatcher.snapshot()["storage_waiting"] == 1
        delays.append(delay)

    monkeypatch.setattr("wxhook_api.services.events.asyncio.sleep", sleep)
    assert await dispatcher._receipt_write("attempt", operation) == "saved"
    assert delays == [1, 3, 10, 30, 30, 30]
    assert dispatcher.snapshot()["storage_waiting"] == 0


@pytest.mark.parametrize("stage", ["attempt", "dispatched", "done", "failed", "blocked"])
async def test_receipt_storage_recovers_without_restart_or_repeating_automation(tmp_path, monkeypatch, stage):
    groups, store, dispatcher = await receipt_dispatcher(tmp_path)
    broken, restore = asyncio.Event(), asyncio.Event()
    original = store.attempt if stage == "attempt" else store.finish

    async def storage(*args):
        if not restore.is_set() and (stage == "attempt" or args[1] == stage):
            broken.set()
            raise sqlite3.OperationalError("synthetic private storage failure")
        return await original(*args)

    monkeypatch.setattr(store, "attempt" if stage == "attempt" else "finish", storage)
    archive = AsyncMock(side_effect=AccountMismatchError("account changed") if stage == "blocked"
        else sqlite3.OperationalError("archive failure") if stage == "failed" else groups.receive)
    automation = AsyncMock()
    dispatcher.register(archive, critical=True)
    dispatcher.register(automation)
    await dispatcher.start()
    try:
        assert await dispatcher.enqueue(event(0))
        await asyncio.wait_for(broken.wait(), 2)
        status = dispatcher.snapshot()
        assert not status["ready"] and status["storage_waiting"] == 1
        assert status["inflight"] == 1 and status["storage_retries"] >= 1
        assert not await dispatcher.enqueue(event(2))
        with groups._connect() as db:
            assert db.execute("SELECT count(*) FROM callback_receipts").fetchone()[0] == 1
        restore.set()
        await asyncio.wait_for(dispatcher._queue.join(), 2)
        assert dispatcher.snapshot()["ready"] and dispatcher.snapshot()["storage_waiting"] == 0
        attempts = 3 if stage == "failed" else 1
        assert archive.await_count == attempts
        assert automation.await_count == (0 if stage in {"failed", "blocked"} else 1)
        with groups._connect() as db:
            assert tuple(db.execute("SELECT state,event_json,attempts FROM callback_receipts").fetchone()) == (
                stage if stage in {"failed", "blocked"} else "done", "", attempts)
        if stage not in {"failed", "blocked"}:
            assert (await groups.messages(GROUP))["total"] == 1
        assert await dispatcher.enqueue(event(2))
        await asyncio.wait_for(dispatcher._queue.join(), 2)
    finally:
        restore.set()
        await dispatcher.stop()


@pytest.mark.parametrize("stage", ["attempt", "dispatched", "done"])
async def test_storage_outage_shutdown_and_restart_preserve_replay_boundary(tmp_path, monkeypatch, stage):
    groups, store, dispatcher = await receipt_dispatcher(tmp_path)
    broken = asyncio.Event()
    original = store.attempt if stage == "attempt" else store.finish

    async def storage(*args):
        if stage == "attempt" or args[1] == stage:
            broken.set()
            raise OSError("offline disk")
        return await original(*args)

    monkeypatch.setattr(store, "attempt" if stage == "attempt" else "finish", storage)
    automation = AsyncMock()
    dispatcher.register(groups.receive, critical=True)
    dispatcher.register(automation)
    await dispatcher.start()
    assert await dispatcher.enqueue(event(0))
    await asyncio.wait_for(broken.wait(), 2)
    await asyncio.wait_for(dispatcher.stop(timeout=0.01), 2)
    assert dispatcher.snapshot()["storage_waiting"] == dispatcher.snapshot()["inflight"] == 0
    assert not dispatcher._queue._busy
    monkeypatch.setattr(store, "attempt" if stage == "attempt" else "finish", original)
    await dispatcher.start()
    try:
        await asyncio.sleep(0)
        await dispatcher._recovery_task
        await asyncio.wait_for(dispatcher._queue.join(), 2)
        assert automation.await_count == 1
        assert (await groups.messages(GROUP))["total"] == 1
        with groups._connect() as db:
            assert db.execute("SELECT state FROM callback_receipts").fetchone()[0] == ("unknown" if stage == "done" else "done")
        assert await dispatcher.enqueue(event(2))
        await asyncio.wait_for(dispatcher._queue.join(), 2)
    finally:
        await dispatcher.stop()


async def test_storage_retry_keeps_same_conversation_in_order(tmp_path, monkeypatch):
    groups, store, dispatcher = await receipt_dispatcher(tmp_path)
    store.capacity = 3
    broken, restore = asyncio.Event(), asyncio.Event()
    original = store.attempt
    observed = []

    async def storage(delivery):
        if delivery.event.msgid == 1 and not restore.is_set():
            broken.set()
            raise OSError("offline disk")
        await original(delivery)

    async def handle(item):
        observed.append(item.msgid)

    monkeypatch.setattr(store, "attempt", storage)
    dispatcher.register(groups.receive, critical=True)
    dispatcher.register(handle)
    # Persist both before consumers start, as in a restart with a backlog.
    await store.start()
    await store.add(event(1))
    await store.add(event(2))
    await dispatcher.start()
    try:
        await asyncio.wait_for(broken.wait(), 2)
        assert observed == []
        restore.set()
        await dispatcher._recovery_task
        await asyncio.wait_for(dispatcher._queue.join(), 2)
        assert observed == [1, 2]
    finally:
        restore.set()
        await dispatcher.stop()


async def test_media_burst_keeps_running_and_queued_jobs_available_for_delivery(tmp_path):
    parsing, release = asyncio.Event(), asyncio.Event()
    first_request = True

    async def respond(request):
        nonlocal first_request
        if first_request:
            first_request = False
            parsing.set()
            await release.wait()
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": f"https://www.douyin.com/note/{ITEM}"})
        if request.url.host == "www.douyin.com":
            return httpx.Response(200, text=page(image_item(1)))
        return httpx.Response(200, content=PNG)

    service = DouyinService(replace(options(tmp_path, notify_group=False), retention=MediaRetention(enabled=False)),
                            transport=httpx.MockTransport(respond))
    service.messenger = SimpleNamespace(prepare=AsyncMock(side_effect=lambda path, kind: (path, "image")),
                                       send=AsyncMock(), text=AsyncMock())
    await service.start()
    try:
        await service.receive(event(1))
        await asyncio.wait_for(parsing.wait(), 2)
        running = next(iter(service._jobs.values()))
        for msgid in range(2, 202):
            await service.receive(event(msgid))
        queued = tuple(service._queue._queue)
        assert len(service._jobs) == 150 and len(queued) == 50
        assert running["id"] in service._jobs
        assert all(job["id"] in service._jobs for job in queued)
        release.set()
        await asyncio.wait_for(service._queue.join(), 15)
        assert running["delivery_status"] == "sent"
        assert all(job["delivery_status"] == "sent" for job in queued)
        assert service.messenger.send.await_count == 51
        assert service.file_path(running["id"], "001.png").is_file()
        assert len(service._jobs) <= 150
    finally:
        release.set()
        await service.stop()


@pytest.mark.parametrize("value", [2**63, -(2**63) - 1, "bad", [1], float("inf")])
async def test_invalid_history_type_isolates_bad_page_and_continues_other_groups(tmp_path, value):
    fake = FakeWeChat(groups=2, history=1)
    fake.histories[ROOM][0].update(local_type=value, type=value)
    good_row = FakeWeChat.message(2)
    fake.histories[ROOM].append(good_row)
    manager, client, sync = await build(tmp_path, fake)
    if isinstance(value, float):
        # The real Hook client rejects infinity before history normalization.
        def history(body):
            offset = (body.page - 1) * body.size
            return HookResponse({"status": 0, "wxid": body.wxid, "page": body.page,
                "data": fake.histories[body.wxid][offset:offset + body.size]}, 200)
        client.get_history = AsyncMock(side_effect=history)
    try:
        await sync.run_once()
        assert sync.status["phase"] == "partial"
        assert (await manager.messages(ROOM))["total"] == 0
        assert (await manager.messages("1001@chatroom"))["total"] == 1
        with manager._connect() as db:
            bad = db.execute("SELECT history_next_page,history_complete,history_error FROM groups WHERE id=?", (ROOM,)).fetchone()
            assert bad[0] == 1 and bad[1] == 0 and "类型" in bad[2]
    finally:
        await client.aclose()
        fake.db.close()


@pytest.mark.parametrize("value", [-(2**63), 2**63 - 1, 1])
async def test_history_type_boundaries_preserve_message_id(tmp_path, value):
    groups = GroupManager(tmp_path / "history.sqlite3")
    await groups.start([GROUP])
    message_id = 2**80 + 1
    assert await groups.import_history(GROUP, [{"msgid": message_id, "type": value, "timestamp": 1710000000,
                                               "content": "valid boundary", "sender": "member"}]) == (1, 1)
    row = (await groups.messages(GROUP))["messages"][0]
    assert row["type"] == value and row["msgid"] == str(message_id)


@pytest.mark.parametrize("kind", ["history", "contacts"])
async def test_waiting_sync_write_rechecks_account_suspension(tmp_path, kind):
    groups = GroupManager(tmp_path / "history.sqlite3")
    await groups.start([GROUP])
    await groups.claim_account("original")
    await groups._lock.acquire()
    operation = groups.import_history(GROUP, [{"msgid": 1, "type": 1, "timestamp": 1710000000,
        "content": "queued page", "sender": "member"}]) if kind == "history" else groups.sync_contacts(
        [{"id": GROUP, "name": "changed name"}], {GROUP: [{"id": "new_member", "name": "new name"}]})
    pending = asyncio.create_task(operation)
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        groups.suspend_account("account changed")
    finally:
        groups._lock.release()
    with pytest.raises(AccountMismatchError):
        await pending
    assert (await groups.messages(GROUP))["total"] == 0
    assert await groups.members(GROUP) == []
    with groups._connect() as db:
        assert db.execute("SELECT wechat_name FROM groups WHERE id=?", (GROUP,)).fetchone()[0] == ""
