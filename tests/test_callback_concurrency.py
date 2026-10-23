import asyncio

import pytest
import httpx
from asgi_lifespan import LifespanManager

from tests.test_callbacks import EVENT
from wxhook_api.core.config import Settings
from wxhook_api.main import create_app
from wxhook_api.services.events import EventDispatcher
from wxhook_api.services.groups import GroupManager
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.core.errors import AccountMismatchError
from wxhook_api.services.wechat_sync import SyncOptions


def callback(group, message_id):
    return {**EVENT, "roomid": group, "wxid": group, "msgid": message_id}


async def test_four_conversations_overlap_but_backlog_keeps_per_group_order():
    dispatcher = EventDispatcher(maxsize=20, workers=4)
    release = asyncio.Event()
    all_started = asyncio.Event()
    active, peak, started, finished = set(), 0, [], []

    async def handle(event):
        nonlocal peak
        group = event.roomid
        assert group not in active
        active.add(group)
        peak = max(peak, len(active))
        started.append((group, event.msgid))
        if len(active) == 4:
            all_started.set()
        try:
            await release.wait()
            finished.append((group, event.msgid))
        finally:
            active.remove(group)

    dispatcher.register(handle)
    await dispatcher.start()
    try:
        # A busy group's entire backlog is ahead of other groups in the queue.
        for message_id in range(5):
            assert await dispatcher.enqueue(callback("a@chatroom", message_id))
        for group in ("b", "c", "d", "e"):
            assert await dispatcher.enqueue(callback(f"{group}@chatroom", 0))
        await asyncio.wait_for(all_started.wait(), 1)
        assert started == [(f"{group}@chatroom", 0) for group in "abcd"]
        release.set()
        await asyncio.wait_for(dispatcher._queue.join(), 1)
        assert peak == 4
        assert [key for group, key in finished if group == "a@chatroom"] == list(range(5))
        assert len(finished) == 9
    finally:
        release.set()
        await dispatcher.stop()


async def test_roomid_and_conversation_wxid_share_the_same_ordering():
    dispatcher = EventDispatcher(workers=4)
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def handle(event):
        observed.append(event.msgid)
        if event.msgid == 1:
            entered.set()
            await release.wait()

    dispatcher.register(handle)
    await dispatcher.start()
    try:
        await dispatcher.enqueue(callback("a@chatroom", 1))
        await asyncio.wait_for(entered.wait(), 1)
        await dispatcher.enqueue({**callback("a@chatroom", 2), "roomid": ""})
        await dispatcher.enqueue(callback("b@chatroom", 3))
        for _ in range(20):
            if 3 in observed:
                break
            await asyncio.sleep(0)
        assert observed == [1, 3]
        release.set()
        await asyncio.wait_for(dispatcher._queue.join(), 1)
        assert observed == [1, 3, 2]
    finally:
        release.set()
        await dispatcher.stop()


@pytest.mark.parametrize("critical", [True, False])
async def test_handler_failure_does_not_block_next_message_or_other_groups(critical):
    dispatcher = EventDispatcher()
    observed = []

    async def first(event):
        if event.msgid == 1:
            raise RuntimeError("failed archive")

    async def second(event):
        observed.append(event.msgid)

    dispatcher.register(first, critical=critical)
    dispatcher.register(second)
    await dispatcher.start()
    for group, key in [("a", 1), ("a", 2), ("b", 3)]:
        await dispatcher.enqueue(callback(f"{group}@chatroom", key))
    await dispatcher.stop()
    assert sorted(observed) == ([2, 3] if critical else [1, 2, 3])
    assert not dispatcher._queue._busy


async def test_timeout_and_repeated_shutdown_cancellation_finish_all_consumers():
    dispatcher = EventDispatcher(maxsize=4)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started, cleaned = [], []

    async def handle(event):
        started.append(event.msgid)
        if len(started) == 4:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.append(event.msgid)

    dispatcher.register(handle)
    await dispatcher.start()
    supervisor = dispatcher._worker
    try:
        for index in range(4):
            await dispatcher.enqueue(callback(f"g{index}@chatroom", index))
        await asyncio.wait_for(entered.wait(), 1)
        for index in range(4):
            await dispatcher.enqueue(callback(f"g{index}@chatroom", index + 4))
        shutdown = asyncio.create_task(dispatcher.stop(timeout=0.01))
        await asyncio.wait_for(cleaning.wait(), 1)
        shutdown.cancel()
        await asyncio.sleep(0)
        shutdown.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, 1)
        assert supervisor.done() and dispatcher._worker is None
        assert sorted(cleaned) == list(range(4))
        assert not dispatcher._queue._busy and dispatcher._queue.empty()
        await asyncio.wait_for(dispatcher._queue.join(), 1)
        dispatcher._handlers.clear()
        await dispatcher.start()
        assert await dispatcher.enqueue(callback("g0@chatroom", 10))
        await asyncio.wait_for(dispatcher._queue.join(), 1)
    finally:
        release.set()
        await dispatcher.stop(timeout=0.01)


async def test_single_worker_setting_keeps_global_serial_processing():
    dispatcher = EventDispatcher(workers=1)
    observed = []

    async def handle(event):
        await asyncio.sleep(0)
        observed.append(event.msgid)

    dispatcher.register(handle)
    await dispatcher.start()
    for key in range(6):
        await dispatcher.enqueue(callback(f"g{key}@chatroom", key))
    await dispatcher.stop()
    assert observed == list(range(6))


@pytest.mark.parametrize("workers", [0, 5, -1, True, 1.5])
def test_dispatcher_rejects_invalid_concurrency(workers):
    with pytest.raises(ValueError):
        EventDispatcher(workers=workers)


@pytest.mark.parametrize("workers", ["0", "5", "-1", "1.5", "bad"])
def test_environment_rejects_invalid_concurrency(monkeypatch, workers):
    monkeypatch.setenv("WXHOOK_CALLBACK_WORKERS", workers)
    with pytest.raises(ValueError):
        Settings.from_environment()


@pytest.mark.parametrize("workers", [1, 4])
def test_environment_configures_application_dispatcher(monkeypatch, workers):
    monkeypatch.setenv("WXHOOK_CALLBACK_WORKERS", str(workers))
    assert create_app().state.event_dispatcher.workers == workers


async def test_application_overlaps_account_checks_and_archives_all_groups():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def hook(request):
        nonlocal calls
        if request.url.path == "/QueryDB/status":
            return httpx.Response(200, json={"IsLogin": 0})
        assert request.url.path == "/GetSelfProfile"
        calls += 1
        if calls == 4:
            started.set()
        await release.wait()
        return httpx.Response(200, json={"wxid": "robot"})

    app = create_app(hook_transport=httpx.MockTransport(hook), sync_options=SyncOptions(enabled=True))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            for index in range(4):
                response = await client.post("/receiveChatBotMsg", json=callback(f"g{index}@chatroom", index))
                assert response.status_code == 200
            await asyncio.wait_for(started.wait(), 2)
            release.set()
            await asyncio.wait_for(app.state.event_dispatcher._queue.join(), 3)
            for index in range(4):
                result = await app.state.groups.messages(f"g{index}@chatroom")
                assert result["total"] == 1 and result["messages"][0]["msgid"] == str(index)
            assert app.state.message_inbox.snapshot()["total"] == 4
        finally:
            release.set()


async def test_callback_waiting_for_database_rechecks_account_suspension(tmp_path):
    manager = GroupManager(tmp_path / "concurrent-account.sqlite3")
    await manager.start([])
    await manager._lock.acquire()
    pending = asyncio.create_task(manager.receive(CallbackEvent.model_validate(callback("g@chatroom", 1))))
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        manager.suspend_account("account changed")
    finally:
        manager._lock.release()
    with pytest.raises(AccountMismatchError):
        await pending
    assert (await manager.messages("g@chatroom"))["total"] == 0
