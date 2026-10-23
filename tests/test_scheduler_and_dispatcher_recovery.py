import asyncio
from datetime import datetime
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_callbacks import EVENT
from wxhook_api.services.chat_summary import CHINA, ChatSummaryService
from wxhook_api.services.events import EventDispatcher


async def test_summary_scheduler_recovers_after_transient_group_list_failure(monkeypatch):
    instant = datetime(2026, 10, 21, 19, tzinfo=CHINA)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    groups = SimpleNamespace(summary_group_ids=AsyncMock(side_effect=[
        sqlite3.OperationalError("private database path"), ["review@chatroom"],
    ]))
    service = ChatSummaryService(groups, None)
    service.automatic_allowed = lambda group: True
    service.get = AsyncMock()
    errors = []

    async def next_cycle(delay):
        assert delay == 30
        errors.append(service.last_error)
        if len(errors) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("wxhook_api.services.chat_summary.datetime", Clock)
    monkeypatch.setattr("wxhook_api.services.chat_summary.asyncio.sleep", next_cycle)
    with pytest.raises(asyncio.CancelledError):
        await service._run()
    assert errors[0] and "private database path" not in errors[0]
    assert errors[1] == ""
    service.get.assert_awaited_once_with("review@chatroom", summary_date="2026-10-21", automatic=True)
    assert service.last_run_date == "2026-10-21"


async def test_summary_scheduler_does_not_swallow_shutdown_cancellation(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 21, 19, tzinfo=CHINA)

    groups = SimpleNamespace(summary_group_ids=AsyncMock(side_effect=asyncio.CancelledError))
    service = ChatSummaryService(groups, None)
    monkeypatch.setattr("wxhook_api.services.chat_summary.datetime", Clock)
    with pytest.raises(asyncio.CancelledError):
        await service._run()
    assert groups.summary_group_ids.await_count == 1


@pytest.mark.parametrize("capacity", [1, 10])
async def test_dispatcher_timeout_discards_queued_events_and_sentinel_before_restart(capacity):
    dispatcher = EventDispatcher(maxsize=capacity)
    started = asyncio.Event()
    processed = []

    async def handler(event):
        processed.append(event.msgid)
        if event.msgid == 1:
            started.set()
            await asyncio.Event().wait()

    dispatcher.register(handler)
    await dispatcher.start()
    try:
        assert await dispatcher.enqueue({**EVENT, "msgid": 1})
        await asyncio.wait_for(started.wait(), 1)
        assert await dispatcher.enqueue({**EVENT, "msgid": 2})
        await dispatcher.stop(timeout=0.01)
        assert dispatcher._queue.empty()
        await asyncio.wait_for(dispatcher._queue.join(), 1)
        await dispatcher.start()
        assert await dispatcher.enqueue({**EVENT, "msgid": 3})
        await asyncio.wait_for(dispatcher._queue.join(), 1)
        assert processed == [1, 3]
    finally:
        await dispatcher.stop(timeout=0.01)


async def test_cancelling_dispatcher_stop_waits_for_worker_and_queue_cleanup():
    dispatcher = EventDispatcher(maxsize=1)
    entered, stopping = asyncio.Event(), asyncio.Event()

    async def handler(event):
        entered.set()
        await asyncio.Event().wait()

    original_put = dispatcher._queue.put

    async def observe_put(item):
        stopping.set()
        await original_put(item)

    dispatcher._queue.put = observe_put
    dispatcher.register(handler)
    await dispatcher.start()
    worker = dispatcher._worker
    try:
        await dispatcher.enqueue(EVENT)
        await asyncio.wait_for(entered.wait(), 1)
        await dispatcher.enqueue(EVENT)
        shutdown = asyncio.create_task(dispatcher.stop(timeout=0.05))
        await asyncio.wait_for(stopping.wait(), 1)
        shutdown.cancel()
        await asyncio.sleep(0)
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, 1)
        assert worker.done()
        assert dispatcher._worker is None and not dispatcher._accepting
        assert dispatcher._queue.empty()
        await asyncio.wait_for(dispatcher._queue.join(), 1)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_graceful_dispatcher_stop_finishes_every_accepted_event():
    dispatcher = EventDispatcher(maxsize=3)
    processed = []

    async def handler(event):
        await asyncio.sleep(0)
        processed.append(event.msgid)

    dispatcher.register(handler)
    await dispatcher.start()
    for message_id in range(3):
        assert await dispatcher.enqueue({**EVENT, "msgid": message_id})
    await dispatcher.stop(timeout=1)
    await asyncio.wait_for(dispatcher._queue.join(), 1)
    assert processed == [0, 1, 2]
    assert dispatcher._worker is None


async def test_dispatcher_stop_cleans_up_an_already_cancelled_worker():
    dispatcher = EventDispatcher()
    await dispatcher.start()
    dispatcher._worker.cancel()
    await asyncio.gather(dispatcher._worker, return_exceptions=True)
    assert not await dispatcher.enqueue(EVENT)
    await dispatcher.stop(timeout=1)
    assert dispatcher._worker is None and dispatcher._queue.empty()
    await asyncio.wait_for(dispatcher._queue.join(), 1)
