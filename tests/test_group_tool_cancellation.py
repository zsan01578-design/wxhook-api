import asyncio
from datetime import datetime, timedelta
import sqlite3

import pytest

from tests.test_chatbot import MEMBER, context
from tests.test_douyin import GROUP
from wxhook_api.services.chat_summary import CHINA


KINDS = ["keyword", "digest", "member"]


async def prepare(state, kind):
    tools = state.app.state.group_tools
    await tools.stop()
    if kind == "keyword":
        rule = dict(name="故障反馈", keyword="故障", match_mode="contains",
                    action="reply", reply="请提供发生时间。", enabled=True, cooldown=10)
        rule_id = await tools.save_rule(GROUP, rule)
        return lambda: tools._process_rule((GROUP, rule_id, "cancel-test", MEMBER, "系统故障"))
    if kind == "digest":
        await tools.save_task(GROUP, dict(title="提交报告", owner="", due_at=(
            datetime.now(CHINA) - timedelta(minutes=1)).isoformat()))
        await tools.preferences(GROUP, reminders=True)
        return tools.due_reminders
    await tools.reminders.chat_command("提醒一分钟后喝水", GROUP, MEMBER, "cancel-test")
    due = datetime.now(CHINA) + timedelta(minutes=2)
    return lambda: tools.reminders.due_reminders(due)


async def saved_status(tools, kind):
    table = {"keyword": "keyword_hits", "digest": "group_tool_reminders",
             "member": "member_reminders"}[kind]
    return await tools.db(lambda db: db.execute(
        f"SELECT status FROM {table} WHERE group_id=?", (GROUP,)).fetchone()[0])


@pytest.mark.parametrize("kind", KINDS)
async def test_cancelled_send_records_unknown_and_does_not_retry(tmp_path, monkeypatch, kind):
    async with context(tmp_path) as state:
        run = await prepare(state, kind)
        tools = state.app.state.group_tools
        entered = asyncio.Event()
        original_text = state.bot._messenger.text

        async def interrupted_send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(state.bot._messenger, "text", interrupted_send)
        pending = asyncio.create_task(run())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
            assert await saved_status(tools, kind) == "unknown"
            monkeypatch.setattr(state.bot._messenger, "text", original_text)
            await tools.db(lambda db: db.execute("UPDATE keyword_rules SET last_triggered=''"))
            await run()
            assert not state.sends, "An uncertain send must never be retried automatically"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("kind", KINDS)
async def test_cancellation_after_claim_commit_still_finalizes_claim(tmp_path, monkeypatch, kind):
    async with context(tmp_path) as state:
        run = await prepare(state, kind)
        tools = state.app.state.group_tools
        committed, release = asyncio.Event(), asyncio.Event()
        original_db = tools.db

        async def delayed_result(action):
            result = await original_db(action)
            if action.__name__ == "claim":
                committed.set()
                await release.wait()
            return result

        monkeypatch.setattr(tools, "db", delayed_result)
        pending = asyncio.create_task(run())
        try:
            await asyncio.wait_for(committed.wait(), 2)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
            assert await saved_status(tools, kind) == "unknown"
            assert not state.sends
            # A reminder must also become editable again without restarting.
            if kind == "member":
                task = (await tools.snapshot(GROUP))["tasks"][0]
                await tools.save_task(GROUP, dict(title="稍后喝水", owner="", due_at=(
                    datetime.now(CHINA) + timedelta(hours=1)).isoformat()), task["id"])
                assert await saved_status(tools, kind) == "pending"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("kind", KINDS)
async def test_cancelling_final_status_wait_preserves_confirmed_send(tmp_path, monkeypatch, kind):
    async with context(tmp_path) as state:
        run = await prepare(state, kind)
        tools = state.app.state.group_tools
        confirmed = asyncio.Event()

        async def confirmed_send(*args, **kwargs):
            # Another request may own the database lock when a send returns.
            await tools.groups._lock.acquire()
            confirmed.set()

        monkeypatch.setattr(state.bot._messenger, "text", confirmed_send)
        pending = asyncio.create_task(run())
        try:
            await asyncio.wait_for(confirmed.wait(), 2)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            tools.groups._lock.release()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
            assert await saved_status(tools, kind) == "sent"
        finally:
            if tools.groups._lock.locked():
                tools.groups._lock.release()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("kind", KINDS)
async def test_finalization_database_failure_does_not_swallow_cancellation(tmp_path, monkeypatch, kind):
    async with context(tmp_path) as state:
        run = await prepare(state, kind)
        tools = state.app.state.group_tools
        entered = asyncio.Event()
        original_db = tools.db
        fail_finalization = False

        async def interrupted_send(*args, **kwargs):
            nonlocal fail_finalization
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                fail_finalization = True

        async def failed_write(action):
            if fail_finalization:
                raise sqlite3.OperationalError("private database path")
            return await original_db(action)

        monkeypatch.setattr(state.bot._messenger, "text", interrupted_send)
        monkeypatch.setattr(tools, "db", failed_write)
        pending = asyncio.create_task(run())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
            assert tools.last_error and "private database path" not in tools.last_error
        finally:
            monkeypatch.setattr(tools, "db", original_db)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("kind", ["keyword", "digest"])
async def test_startup_recovers_only_unconfirmed_sending_status(tmp_path, kind):
    async with context(tmp_path) as state:
        run = await prepare(state, kind)
        await run()
        tools = state.app.state.group_tools
        assert await saved_status(tools, kind) == "sent"
        table = "keyword_hits" if kind == "keyword" else "group_tool_reminders"
        await tools.db(lambda db: db.execute(f"UPDATE {table} SET status='sending'"))
        terminal_statuses = ["sent", "skipped", "unknown"]
        if kind == "keyword":
            terminal_statuses.append("recorded")
            for status in terminal_statuses:
                await tools.db(lambda db: db.execute("""INSERT INTO keyword_hits
                    (group_id,rule_id,rule_name,message_key,sender,content,status,created_at)
                    SELECT group_id,rule_id,rule_name,?,sender,content,?,created_at
                    FROM keyword_hits WHERE message_key='cancel-test'""", (status, status)))
        else:
            for status in terminal_statuses:
                await tools.db(lambda db: db.execute(
                    "INSERT INTO group_tool_reminders VALUES (?,? ,?)",
                    (status, datetime.now(CHINA).date().isoformat(), status)))
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.stop()
        assert await saved_status(tools, kind) == "unknown"
        assert not state.sends
        key = "message_key" if kind == "keyword" else "group_id"
        statuses = await tools.db(lambda db: dict(db.execute(f"SELECT {key},status FROM {table}")))
        assert all(statuses[status] == status for status in terminal_statuses)


@pytest.mark.parametrize("kind", KINDS)
async def test_failed_claim_is_reported_and_can_retry_without_sending(tmp_path, monkeypatch, kind):
    async with context(tmp_path) as state:
        run = await prepare(state, kind)
        tools = state.app.state.group_tools
        original_db = tools.db
        fail = True

        async def transient_failure(action):
            nonlocal fail
            if action.__name__ == "claim" and fail:
                fail = False
                raise sqlite3.OperationalError("synthetic claim failure")
            return await original_db(action)

        monkeypatch.setattr(tools, "db", transient_failure)
        with pytest.raises(sqlite3.OperationalError):
            await run()
        assert not state.sends
        await run()
        assert await saved_status(tools, kind) == "sent"
        assert len(state.sends) == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_duplicate_runner_cannot_finalize_another_senders_claim(tmp_path, monkeypatch, kind):
    async with context(tmp_path) as state:
        run = await prepare(state, kind)
        tools = state.app.state.group_tools
        entered, release = asyncio.Event(), asyncio.Event()
        original_text = state.bot._messenger.text

        async def delayed_send(*args, **kwargs):
            entered.set()
            await release.wait()
            await original_text(*args, **kwargs)

        monkeypatch.setattr(state.bot._messenger, "text", delayed_send)
        pending = asyncio.create_task(run())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await run()
            assert await saved_status(tools, kind) == "sending"
            assert not state.sends
            release.set()
            await asyncio.wait_for(pending, 2)
            assert await saved_status(tools, kind) == "sent"
            assert len(state.sends) == 1
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_stopping_rules_discards_pending_queue_before_restart(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.stop()
        entered = asyncio.Event()
        processed = []

        async def process(job):
            processed.append(job)
            if job == "interrupted":
                entered.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(tools, "_process_rule", process)
        await tools.start()
        try:
            tools._queue.put_nowait("interrupted")
            await asyncio.wait_for(entered.wait(), 2)
            tools._queue.put_nowait("stale")
            await tools.stop()
            assert tools._queue.empty()
            await asyncio.wait_for(tools._queue.join(), 1)
            await tools.start()
            tools._queue.put_nowait("fresh")
            await asyncio.wait_for(tools._queue.join(), 2)
            assert processed == ["interrupted", "fresh"]
        finally:
            await tools.stop()


async def test_cancelled_stop_finishes_all_workers_before_propagating(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        workers = [tools.reminders._worker, tools._worker, tools._scheduler]
        entered, release = asyncio.Event(), asyncio.Event()
        original_stop = tools.reminders.stop

        async def delayed_stop():
            entered.set()
            await release.wait()
            await original_stop()

        monkeypatch.setattr(tools.reminders, "stop", delayed_stop)
        pending = asyncio.create_task(tools.stop())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
            assert all(worker.done() for worker in workers)
            assert tools.reminders._worker is tools._worker is tools._scheduler is None
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await tools.stop()
