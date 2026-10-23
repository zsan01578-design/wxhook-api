import asyncio
from contextlib import contextmanager
import json
import sqlite3
import threading

import httpx
import pytest

from tests.test_chatbot import BOT, MEMBER, context
from tests.test_douyin import GROUP
from tests.test_group_chat_context import configure, question, question_text
from tests.test_send_policy_and_search_budget import pause_account_check
from wxhook_api.services.groups import GroupManager


@pytest.mark.parametrize('kind', ['group', 'member'])
@pytest.mark.parametrize('stage', ['write', 'reload'])
async def test_cancelled_blacklist_settles_cache_before_releasing_lock(tmp_path, monkeypatch, kind, stage):
    groups = GroupManager(tmp_path / 'policies.sqlite3')
    await groups.start([GROUP])
    await groups.save_member(GROUP, MEMBER, {}, create=True)
    entered, release = threading.Event(), threading.Event()

    def pause():
        entered.set()
        assert release.wait(5)

    if stage == 'write':
        original = groups._connect

        @contextmanager
        def paused_connect():
            with original() as db:
                yield db
                pause()

        monkeypatch.setattr(groups, '_connect', paused_connect)
    else:
        original = groups._read_policies

        def paused_reload(db):
            pause()
            return original(db)

        monkeypatch.setattr(groups, '_read_policies', paused_reload)

    pending = asyncio.create_task(groups.update_group(GROUP, {'blocked': True}) if kind == 'group'
        else groups.save_member(GROUP, MEMBER, {'blocked': True}))
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        following = asyncio.create_task(groups.update_group(GROUP, {'note': 'following request'}))
        await asyncio.sleep(0)
        assert not pending.done() and groups._lock.locked()
        assert not following.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        assert await asyncio.wait_for(following, 3)
        with sqlite3.connect(groups.path) as db:
            sql = 'SELECT blocked FROM groups WHERE id=?' if kind == 'group' else 'SELECT blocked FROM members WHERE group_id=? AND id=?'
            args = (GROUP,) if kind == 'group' else (GROUP, MEMBER)
            assert db.execute(sql, args).fetchone()[0] == 1
        assert not groups.allowed(GROUP, MEMBER)
    finally:
        release.set()
        await asyncio.gather(*(task for task in (pending, following) if task), return_exceptions=True)


@pytest.mark.parametrize('kind', ['group', 'member'])
async def test_cancelled_failed_blacklist_rolls_back_without_stale_cache(tmp_path, monkeypatch, kind):
    groups = GroupManager(tmp_path / 'policies.sqlite3')
    await groups.start([GROUP])
    await groups.save_member(GROUP, MEMBER, {}, create=True)
    entered, release = threading.Event(), threading.Event()
    original = groups._connect

    @contextmanager
    def failing_connect():
        with original() as db:
            yield db
            entered.set()
            assert release.wait(5)
            raise sqlite3.OperationalError('synthetic transaction failure')

    monkeypatch.setattr(groups, '_connect', failing_connect)
    pending = asyncio.create_task(groups.update_group(GROUP, {'blocked': True}) if kind == 'group'
        else groups.save_member(GROUP, MEMBER, {'blocked': True}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        assert not groups._lock.locked() and groups.allowed(GROUP, MEMBER)
        with sqlite3.connect(groups.path) as db:
            sql = 'SELECT blocked FROM groups WHERE id=?' if kind == 'group' else 'SELECT blocked FROM members WHERE group_id=? AND id=?'
            args = (GROUP,) if kind == 'group' else (GROUP, MEMBER)
            assert db.execute(sql, args).fetchone()[0] == 0
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize('stage', ['generation', 'account', 'acknowledgement'])
async def test_history_blacklist_rechecked_before_send_and_commit(tmp_path, monkeypatch, stage):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def model(payload):
        if question_text(payload['messages'][-1]) == 'repeat old information':
            assert 'private_history_marker' in json.dumps(payload)
            if stage == 'generation':
                entered.set()
                await release.wait()
            return httpx.Response(200, json={'choices': [{'message': {'content': 'private_history_marker'}}]})

    async def hook(request):
        if stage == 'acknowledgement' and json.loads(request.content)['msg'] == 'private_history_marker':
            entered.set()
            await release.wait()
        return httpx.Response(200, json={'ret': 0})

    async with context(tmp_path, hook=hook) as state:
        await configure(state, calls, handler=model)
        await state.submit(question(GROUP, 'private_history_marker', 1))
        state.sends.clear()
        if stage == 'account':
            entered, release = pause_account_check(state.bot._messenger, monkeypatch)
        try:
            await state.client.post('/receiveChatBotMsg', json=question(GROUP, 'repeat old information', 2, sender='member_B'))
            await asyncio.wait_for(entered.wait(), 3)
            assert await state.app.state.groups.save_member(GROUP, MEMBER, {'blocked': True})
            release.set()
            await asyncio.wait_for(state.bot._queue.join(), 3)
            # The Hook call already began in the acknowledgement case; it cannot be recalled.
            assert len(state.sends) == (1 if stage == 'acknowledgement' else 0)
            stored = state.bot.conversations.history(BOT, GROUP, state.bot.provider)
            assert 'repeat old information' not in json.dumps(stored)
            history = state.bot.conversations.history(BOT, GROUP, state.bot.provider,
                allowed_sender=lambda member: state.app.state.groups.allowed(GROUP, member))
            assert history == []
            if stage != 'acknowledgement':
                assert state.bot.snapshot()['recent_events'][0]['status'] == 'skipped'
            await state.submit(question(GROUP, 'fresh question', 3, sender='member_C'))
            assert 'private_history_marker' not in json.dumps(calls[-1])
        finally:
            release.set()


async def test_blocked_author_also_excludes_already_saved_derived_answers(tmp_path):
    calls = []

    async def model(payload):
        if question_text(payload['messages'][-1]) == 'repeat old information':
            return httpx.Response(200, json={'choices': [{'message': {'content': 'private_history_marker'}}]})

    async with context(tmp_path) as state:
        await configure(state, calls, handler=model)
        await state.submit(question(GROUP, 'private_history_marker', 1))
        await state.submit(question(GROUP, 'repeat old information', 2, sender='member_B'))
        assert state.sends[-1]['msg'] == 'private_history_marker'
        await state.app.state.groups.save_member(GROUP, MEMBER, {'blocked': True})
        await state.submit(question(GROUP, 'fresh question', 3, sender='member_C'))
        assert 'private_history_marker' not in json.dumps(calls[-1])
        assert 'repeat old information' not in json.dumps(calls[-1])


async def test_unrelated_member_blacklist_preserves_valid_inflight_answer(tmp_path):
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(payload):
        if question_text(payload['messages'][-1]) == 'valid question':
            entered.set()
            await release.wait()

    async with context(tmp_path) as state:
        await configure(state, calls, handler=model)
        await state.submit(question(GROUP, 'valid history', 1))
        try:
            await state.client.post('/receiveChatBotMsg', json=question(GROUP, 'valid question', 2, sender='member_B'))
            await asyncio.wait_for(entered.wait(), 3)
            await state.app.state.groups.save_member(GROUP, 'unrelated_member', {'blocked': True}, create=True)
            release.set()
            await asyncio.wait_for(state.bot._queue.join(), 3)
            assert state.sends[-1]['msg'] == 'reply:valid question'
            await state.submit(question(GROUP, 'next question', 3))
            assert 'valid history' in json.dumps(calls[-1]) and 'valid question' in json.dumps(calls[-1])
        finally:
            release.set()
