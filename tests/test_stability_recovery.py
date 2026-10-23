import asyncio
from contextlib import contextmanager
import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tests.test_callbacks import EVENT
from wxhook_api.core.errors import AccountMismatchError
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.callback_store import CallbackStore
from wxhook_api.services.events import EventDispatcher
from wxhook_api.services.groups import GroupManager


GROUP, MEMBER = "stability@chatroom", "member"


def event(msgid=1):
    return CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "sender": MEMBER, "msgid": msgid})


async def drain(dispatcher):
    await asyncio.sleep(0)
    if dispatcher._recovery_task:
        await asyncio.wait_for(dispatcher._recovery_task, 2)
    await asyncio.wait_for(dispatcher._queue.join(), 2)


async def setup_dispatcher(tmp_path):
    groups = GroupManager(tmp_path / "management.sqlite3")
    await groups.start([GROUP])
    store = CallbackStore(groups, 20)
    dispatcher = EventDispatcher(20, store=store)
    dispatcher.retry_delays = (0, 0)
    return groups, store, dispatcher


@pytest.mark.parametrize("kind", ["group", "member", "enabled"])
@pytest.mark.parametrize("restrict", [False, True])
async def test_snapshot_failure_rolls_back_policy_and_audit(tmp_path, monkeypatch, kind, restrict):
    groups = GroupManager(tmp_path / "policies.sqlite3")
    await groups.start([GROUP])
    await groups.save_member(GROUP, MEMBER, {}, create=True)
    column = "enabled" if kind == "enabled" else "blocked"
    value = not restrict if kind == "enabled" else restrict
    change = groups.save_member if kind == "member" else groups.update_group
    async def update(value):
        return await change(GROUP, MEMBER, {column: value}) if kind == "member" else await change(GROUP, {column: value})
    await update(not value)
    previous_allowed = groups.allowed(GROUP, MEMBER)
    before_audit = await groups.audit()
    def fail(db):
        raise sqlite3.OperationalError("private path and secret")
    monkeypatch.setattr(groups, "_read_policies", fail)
    with pytest.raises(sqlite3.OperationalError):
        await update(value)
    assert groups.allowed(GROUP, MEMBER) == previous_allowed
    assert await groups.audit() == before_audit
    with groups._connect() as db:
        sql, args = (f"SELECT {column} FROM members WHERE group_id=? AND id=?", (GROUP, MEMBER)) if kind == "member" else (f"SELECT {column} FROM groups WHERE id=?", (GROUP,))
        assert bool(db.execute(sql, args).fetchone()[0]) == (not value)


async def test_commit_failure_does_not_publish_new_snapshot(tmp_path, monkeypatch):
    groups = GroupManager(tmp_path / "policies.sqlite3")
    await groups.start([GROUP])
    original = groups._connect
    @contextmanager
    def fail():
        with original() as db:
            yield db
            raise sqlite3.OperationalError("synthetic commit failure")
    monkeypatch.setattr(groups, "_connect", fail)
    with pytest.raises(sqlite3.OperationalError):
        await groups.update_group(GROUP, {"blocked": True})
    assert groups.allowed(GROUP, MEMBER)
    with original() as db:
        assert db.execute("SELECT blocked FROM groups WHERE id=?", (GROUP,)).fetchone()[0] == 0


async def test_dead_consumers_reject_http_and_readiness_without_calling_hook():
    hook = AsyncMock(side_effect=AssertionError("must stay local"))
    app = create_app(hook_transport=httpx.MockTransport(hook))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/ready")).status_code == 200
        dispatcher = app.state.event_dispatcher
        dispatcher._worker.cancel()
        await asyncio.gather(dispatcher._worker, return_exceptions=True)
        response = await client.post("/receiveChatBotMsg", json=EVENT)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "callback_unavailable"
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 503
        assert dispatcher._queue.empty()
        assert app.state.callback_status["accepted"] == 0
    hook.assert_not_awaited()


async def test_ingress_storage_error_does_not_ack_or_leak_data(monkeypatch):
    app = create_app()
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        monkeypatch.setattr(app.state.event_dispatcher.store, "add", AsyncMock(side_effect=sqlite3.OperationalError("private path and secret")))
        response = await client.post("/receiveChatBotMsg", json=EVENT)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "callback_storage_unavailable"
        status = await client.get("/api/callback/status")
        assert "private path" not in status.text and "secret" not in status.text
        assert status.json()["processing"]["last_error"]["stage"] == "ingress"
        assert status.json()["accepted"] == 0


async def test_transient_archive_failure_retries_before_automation(tmp_path):
    groups, store, dispatcher = await setup_dispatcher(tmp_path)
    attempts, automation = [], []
    async def archive(item):
        attempts.append(item.msgid)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("temporary storage error")
        await groups.receive(item)
    async def handle(item):
        with groups._connect() as db:
            assert db.execute("SELECT state FROM callback_receipts WHERE receipt_key=?", (item._receipt_key,)).fetchone()[0] == "dispatched"
        automation.append(item.msgid)
    dispatcher.register(archive, critical=True)
    dispatcher.register(handle)
    await dispatcher.start()
    try:
        assert await dispatcher.enqueue(event())
        await drain(dispatcher)
        assert attempts == [1, 1] and automation == [1]
        assert (await groups.messages(GROUP))["total"] == 1
        status = dispatcher.snapshot()
        assert status["processed"] == 1 and status["failed"] == 0
        assert status["archive_retries"] == 1 and status["handler_failures"] == 1
        with groups._connect() as db:
            row = db.execute("SELECT state,event_json,attempts FROM callback_receipts").fetchone()
            assert tuple(row) == ("done", "", 2)
    finally:
        await dispatcher.stop()


@pytest.mark.parametrize("blocked", [False, True])
async def test_permanent_failure_is_bounded_and_not_replayed(tmp_path, blocked):
    groups, store, dispatcher = await setup_dispatcher(tmp_path)
    archive = AsyncMock(side_effect=AccountMismatchError("other account") if blocked else sqlite3.OperationalError("temporary error"))
    automation = AsyncMock()
    dispatcher.register(archive, critical=True)
    dispatcher.register(automation)
    await dispatcher.start()
    assert await dispatcher.enqueue(event())
    await drain(dispatcher)
    assert archive.await_count == (1 if blocked else 3)
    automation.assert_not_awaited()
    assert dispatcher.snapshot()["failed"] == 1
    await dispatcher.stop()
    await dispatcher.start()
    try:
        await drain(dispatcher)
        assert archive.await_count == (1 if blocked else 3)
        with groups._connect() as db:
            assert db.execute("SELECT state FROM callback_receipts").fetchone()[0] == ("blocked" if blocked else "failed")
    finally:
        await dispatcher.stop()


async def test_restart_replays_pending_receipt_but_never_dispatched_receipt(tmp_path):
    groups, store, dispatcher = await setup_dispatcher(tmp_path)
    await store.start()
    pending = await store.add(event(1))
    uncertain = await store.add(event(2))
    await store.finish(uncertain, "dispatched")
    automation = AsyncMock()
    dispatcher.register(groups.receive, critical=True)
    dispatcher.register(automation)
    await dispatcher.start()
    try:
        await drain(dispatcher)
        assert [call.args[0].msgid for call in automation.await_args_list] == [1]
        assert dispatcher.snapshot()["recovered"] == 1
        assert dispatcher.snapshot()["interrupted"] == 1
        assert (await groups.messages(GROUP))["total"] == 1
        with groups._connect() as db:
            states = {row["receipt_key"]: row["state"] for row in db.execute("SELECT receipt_key,state FROM callback_receipts")}
        assert states == {pending.key: "done", uncertain.key: "unknown"}
    finally:
        await dispatcher.stop()


@pytest.mark.parametrize("msgid", [0, 1])
async def test_cancelled_archive_recovery_is_idempotent_even_without_message_id(tmp_path, msgid):
    groups, store, dispatcher = await setup_dispatcher(tmp_path)
    archived = asyncio.Event()
    async def archive(item):
        await groups.receive(item)
        archived.set()
        await asyncio.Event().wait()
    dispatcher.register(archive, critical=True)
    await dispatcher.start()
    assert await dispatcher.enqueue(event(msgid))
    await asyncio.wait_for(archived.wait(), 2)
    await dispatcher.stop(timeout=0.01)
    assert (await groups.messages(GROUP))["total"] == 1
    # Use new instances, as a fresh process would, and reload durable state.
    other_groups, _, other = await setup_dispatcher(tmp_path)
    automation = AsyncMock()
    other.register(other_groups.receive, critical=True)
    other.register(automation)
    await other.start()
    try:
        await drain(other)
        assert (await other_groups.messages(GROUP))["total"] == 1
        automation.assert_awaited_once()
        assert "_receipt_key" not in automation.await_args.args[0].extra
    finally:
        await other.stop()


async def test_automation_cancel_is_unknown_and_never_repeated(tmp_path):
    groups, store, dispatcher = await setup_dispatcher(tmp_path)
    automation = AsyncMock(side_effect=asyncio.CancelledError)
    dispatcher.register(groups.receive, critical=True)
    dispatcher.register(automation)
    await dispatcher.start()
    assert await dispatcher.enqueue(event())
    await asyncio.wait_for(asyncio.gather(dispatcher._worker, return_exceptions=True), 2)
    assert not await dispatcher.enqueue(event(2))
    await dispatcher.stop(timeout=0.01)
    await dispatcher.start()
    try:
        await drain(dispatcher)
        automation.assert_awaited_once()
        assert dispatcher.snapshot()["interrupted"] >= 1
    finally:
        await dispatcher.stop()


async def test_recovery_survives_smaller_queue_and_cancelled_feeder_shutdown(tmp_path):
    groups, store, _ = await setup_dispatcher(tmp_path)
    await store.start()
    for msgid in range(5):
        assert await store.add(event(msgid))
    dispatcher = EventDispatcher(1, workers=1, store=store)
    entered = asyncio.Event()
    async def block(item):
        entered.set()
        await asyncio.Event().wait()
    dispatcher.register(block, critical=True)
    await dispatcher.start()
    await asyncio.wait_for(entered.wait(), 2)
    assert dispatcher.snapshot()["recovering"]
    dispatcher._worker.cancel()
    await asyncio.gather(dispatcher._worker, return_exceptions=True)
    await dispatcher.stop(timeout=0.01)
    # A cancelled recovery feeder must not prevent clean stop, or discard
    # accepted deliveries when a deployment lowers its queue capacity.
    dispatcher = EventDispatcher(1, workers=1, store=store)
    dispatcher.register(groups.receive, critical=True)
    await dispatcher.start()
    try:
        await drain(dispatcher)
        assert (await groups.messages(GROUP))["total"] == 5
    finally:
        await dispatcher.stop()
