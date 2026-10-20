import asyncio
from contextlib import contextmanager
import threading

import pytest

from tests.test_chatbot import BOT, MEMBER, event
from tests.test_douyin import GROUP
from wxhook_api.core.errors import AccountMismatchError
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.groups import GroupManager


def pause_first_connection(manager, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = manager._connect

    @contextmanager
    def paused():
        first = not entered.is_set()
        try:
            with original() as db:
                if first:
                    entered.set()
                    assert release.wait(5)
                yield db
        finally:
            if first:
                finished.set()

    monkeypatch.setattr(manager, "_connect", paused)
    return entered, release, finished


async def test_cancelled_account_mismatch_still_suspends_until_restart(tmp_path, monkeypatch):
    groups = GroupManager(tmp_path / "archive.sqlite3")
    await groups.start([GROUP])
    await groups.claim_account(BOT)
    entered, release, finished = pause_first_connection(groups, monkeypatch)
    pending = asyncio.create_task(groups.claim_account("wxid_different_account"))
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        following = asyncio.create_task(groups.claim_account(BOT))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        assert await asyncio.to_thread(finished.wait, 3)
        assert groups.account_error and not groups.allowed(GROUP, MEMBER)
        with pytest.raises(AccountMismatchError):
            await asyncio.wait_for(following, 3)
        with pytest.raises(AccountMismatchError):
            await groups.claim_account(BOT)
        restarted = GroupManager(groups.path)
        await restarted.start([])
        await restarted.claim_account(BOT)
        assert restarted.allowed(GROUP, MEMBER)
    finally:
        release.set()
        await asyncio.gather(*(task for task in (pending, following) if task), return_exceptions=True)
        await asyncio.to_thread(finished.wait, 3)


@pytest.mark.parametrize("kind", ["callback", "history", "contacts", "checkpoint", "failure", "summary", "reply_claim", "reply_status"])
async def test_cancelled_archive_write_settles_before_following_write(tmp_path, monkeypatch, kind):
    groups = GroupManager(tmp_path / "archive.sqlite3")
    await groups.start([GROUP])
    await groups.claim_chat_reply(GROUP, "reply")
    entered, release, finished = pause_first_connection(groups, monkeypatch)
    operations = {
        "callback": lambda: groups.receive(CallbackEvent.model_validate(event("archived"))),
        "history": lambda: groups.import_history(GROUP, [dict(msgid=2, sender=MEMBER, content="history", type=1, timestamp=1800000000)]),
        "contacts": lambda: groups.sync_contacts([dict(id=GROUP, name="first")], {}),
        "checkpoint": lambda: groups.history_checkpoint(GROUP, next_page=2, complete=False),
        "failure": lambda: groups.history_failure(GROUP, "first failure"),
        "summary": lambda: groups.save_summary(GROUP, "2026-10-20", "first summary", 1),
        "reply_claim": lambda: groups.claim_chat_reply(GROUP, "new reply"),
        "reply_status": lambda: groups.finish_chat_reply(GROUP, "reply", "sent"),
    }
    pending = asyncio.create_task(operations[kind]())
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        following = asyncio.create_task(groups.history_checkpoint(GROUP, next_page=9, complete=True))
        await asyncio.sleep(0)
        assert groups._lock.locked() and not pending.done() and not following.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        await asyncio.wait_for(following, 3)
        assert finished.is_set()
        with groups._connect() as db:
            row = db.execute("SELECT history_next_page,history_complete,history_error FROM groups WHERE id=?", (GROUP,)).fetchone()
            assert tuple(row) == (9, 1, "")
            if kind in {"callback", "history"}:
                assert db.execute("SELECT count(*) FROM chat_messages WHERE group_id=?", (GROUP,)).fetchone()[0] == 1
            elif kind == "summary":
                assert db.execute("SELECT summary FROM chat_summaries WHERE group_id=?", (GROUP,)).fetchone()[0] == "first summary"
            elif kind == "reply_status":
                assert db.execute("SELECT status FROM chat_reply_claims WHERE group_id=? AND message_key='reply'", (GROUP,)).fetchone()[0] == "sent"
            elif kind == "reply_claim":
                assert db.execute("SELECT count(*) FROM chat_reply_claims WHERE group_id=? AND message_key='new reply'", (GROUP,)).fetchone()[0] == 1
            elif kind == "contacts":
                assert db.execute("SELECT wechat_name FROM groups WHERE id=?", (GROUP,)).fetchone()[0] == "first"
    finally:
        release.set()
        await asyncio.gather(*(task for task in (pending, following) if task), return_exceptions=True)
        await asyncio.to_thread(finished.wait, 3)


async def test_cancelled_archive_initialization_finishes_schema_and_policy_reload(tmp_path, monkeypatch):
    groups = GroupManager(tmp_path / "archive.sqlite3")
    entered, release, finished = pause_first_connection(groups, monkeypatch)
    pending = asyncio.create_task(groups.start([GROUP]))
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        following = asyncio.create_task(groups.update_group(GROUP, {"blocked": True}))
        await asyncio.sleep(0)
        assert groups._lock.locked() and not pending.done() and not following.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        assert await asyncio.wait_for(following, 3)
        assert not groups.allowed(GROUP, MEMBER)
        with groups._connect() as db:
            assert db.execute("SELECT blocked FROM groups WHERE id=?", (GROUP,)).fetchone()[0] == 1
    finally:
        release.set()
        await asyncio.gather(*(task for task in (pending, following) if task), return_exceptions=True)
        await asyncio.to_thread(finished.wait, 3)
