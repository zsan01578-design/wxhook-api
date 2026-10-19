import asyncio
from datetime import date, datetime
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.test_chatbot import context
from tests.test_douyin import GROUP
from tests.test_group_tools import Models
from tests.test_web_search import FakeSearchProvider
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.chat_model import (ChatModelConfiguration,
                                          ModelError, ModelProfile, ModelSettings)
from wxhook_api.services.chat_summary import ChatSummaryService, CHINA
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.group_tools import GroupToolsService, report_period
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.web_search import SearchError, SearchSettings, WebSearchConfiguration


async def seed_report(tools, kind):
    start, _ = report_period(kind)
    stamp = int(datetime.combine(start, datetime.min.time(), CHINA).timestamp())
    await tools.groups.import_history(GROUP, [dict(msgid=kind, timestamp=stamp,
        type=1, sender='member', content='report record')])


async def test_disabling_reports_cancels_already_selected_scheduler_jobs(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        tools._scheduler.cancel()
        await asyncio.gather(tools._scheduler, return_exceptions=True)
        await tools.groups.add_group(GROUP, 'audit')
        await tools.preferences(GROUP, True)
        await seed_report(tools, 'week')
        await seed_report(tools, 'month')
        models = Models()
        tools.models = models
        selected = asyncio.Event()
        original_db = tools.db

        async def observed(action):
            result = await original_db(action)
            if result == [GROUP]:
                selected.set()
            return result

        monkeypatch.setattr(tools, 'db', observed)
        async with tools._generation_lock:
            pending = asyncio.create_task(tools.scheduled_reports())
            try:
                await asyncio.wait_for(selected.wait(), 2)
                await tools.preferences(GROUP, False)
            except BaseException:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                raise
        await asyncio.wait_for(pending, 2)
        assert not models.calls
        assert (await tools.snapshot(GROUP))['auto_reports'] is False


async def test_automatic_report_permission_restored_after_restart(tmp_path):
    async with context(tmp_path) as state:
        original = state.app.state.group_tools
        await original.groups.add_group(GROUP, 'audit')
        await original.preferences(GROUP, True)
        await original.stop()
        restored = GroupToolsService(original.groups, original.models, state.bot)
        try:
            await restored.start()
            assert restored.automatic_report_allowed(GROUP)
            await restored.preferences(GROUP, False)
            assert not restored.automatic_report_allowed(GROUP)
        finally:
            await restored.stop()


@pytest.mark.parametrize('kind', ['week', 'month'])
async def test_reports_recheck_own_switch_after_model_budget_wait(tmp_path, monkeypatch, kind):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        tools._scheduler.cancel()
        await asyncio.gather(tools._scheduler, return_exceptions=True)
        await tools.groups.add_group(GROUP, 'audit')
        await tools.preferences(GROUP, True)
        await seed_report(tools, kind)
        config = state.app.state.chat_model
        profile = ModelProfile(id='test', model='test', base_url='https://model.invalid/v1')
        config.models, config.active_id = {'test': profile}, 'test'
        outbound = []

        def respond(request):
            outbound.append(request)
            return httpx.Response(200, json={'choices':[{'message':{'content':'report'}}]})

        config.transport = httpx.MockTransport(respond)

        async def disable_while_waiting():
            await tools.preferences(GROUP, False)

        monkeypatch.setattr(config.budget, 'reserve', disable_while_waiting)
        with pytest.raises(ModelError):
            await tools.report(GROUP, kind, automatic=True)
        assert not outbound
        # An explicit manual refresh still works with automatic reports off.
        monkeypatch.setattr(config.budget, 'reserve', AsyncMock())
        assert (await tools.report(GROUP, kind))['content'] == 'report'
        assert len(outbound) == 1


@pytest.mark.parametrize('error', [SearchError('timeout'), RuntimeError('failure'), asyncio.CancelledError()])
async def test_failed_or_cancelled_search_cannot_refund_its_attempt(tmp_path, error):
    provider = FakeSearchProvider(error=error)
    path = tmp_path / 'search.json'
    config = WebSearchConfiguration(path, provider=provider, now=lambda: date(2026, 10, 19))
    await config.save(SearchSettings(enabled=True, api_key='synthetic-only', daily_limit=1))
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await config.context('first')
    else:
        assert await config.context('first') is None
    assert await config.context('second') is None
    restarted = WebSearchConfiguration(path, provider=provider, now=lambda: date(2026, 10, 19))
    assert await restarted.context('after restart') is None
    assert restarted.snapshot()['used_today'] == 1
    assert provider.queries == ['first']


async def test_concurrent_failed_searches_stay_within_daily_limit(tmp_path):
    provider = FakeSearchProvider(error=SearchError('timeout'))
    config = WebSearchConfiguration(tmp_path / 'search.json', provider=provider)
    await config.save(SearchSettings(enabled=True, api_key='synthetic-only', daily_limit=2))
    results = await asyncio.gather(*(config.context(str(i)) for i in range(10)))
    assert results == [None] * 10
    assert len(provider.queries) == config.snapshot()['used_today'] == 2


@pytest.mark.parametrize('automatic', [False, True])
async def test_empty_summary_rechecks_new_realtime_messages(tmp_path, automatic):
    groups = GroupManager(tmp_path / 'groups.sqlite3')
    await groups.start([])
    await groups.add_group(GROUP, 'audit')
    await groups.update_group(GROUP, {'chat_enabled':True, 'summary_enabled':True})
    bot = MentionChatService(ChatbotOptions(True, 'robot', ('robot',)))
    bot.groups = groups
    models = SimpleNamespace(chatbot=bot, settings=True, answer=AsyncMock(return_value='new summary'))
    summary = ChatSummaryService(groups, models)
    empty = await summary.get(GROUP, automatic=automatic)
    assert empty['message_count'] == 0
    assert await summary.get(GROUP, automatic=automatic) == empty
    models.answer.assert_not_called()
    await groups.receive(CallbackEvent(event_type=1001, msgid=1, type=1,
        timestamp=int(datetime.now(CHINA).timestamp()), wxid=GROUP, roomid=GROUP,
        sender='member', content='new real-time message'))
    updated = await summary.get(GROUP, automatic=automatic)
    assert updated['message_count'] == 1 and updated['summary'] == 'new summary'
    assert await summary.get(GROUP, automatic=automatic) == updated
    models.answer.assert_awaited_once()


@pytest.mark.parametrize('kind', ['model', 'search'])
@pytest.mark.parametrize('write_fails', [False, True])
@pytest.mark.parametrize('following_save', [False, True])
async def test_cancelled_save_settles_before_next_save(tmp_path, monkeypatch, kind, write_fails, following_save):
    if kind == 'model':
        config = ChatModelConfiguration(MentionChatService(ChatbotOptions()))
        config.transport = httpx.MockTransport(lambda request: httpx.Response(200,
            json={'choices':[{'message':{'content':'test'}}]}))

        async def save(value):
            return await config.configure(ModelSettings(base_url='https://model.invalid/v1', model=value))

        def current(): return config.settings.model
        def disk(): return json.loads(config.path.read_text())['models'][0]['model']
        writer_name = '_save'
    else:
        config = WebSearchConfiguration(tmp_path / 'search.json')

        async def save(value):
            return await config.save(SearchSettings(base_url='https://' + value + '.invalid/v1'))

        def current(): return config.settings.base_url
        def disk(): return json.loads(config.path.read_text())['base_url']
        writer_name = '_write'

    await save('old')
    before = current()
    entered, release = threading.Event(), threading.Event()
    original = getattr(config, writer_name)
    writes = []

    def slow_write(payload):
        writes.append(payload)
        if len(writes) == 1:
            entered.set()
            if not release.wait(5):
                raise AssertionError('test failed to release write')
            if write_fails:
                raise OSError('synthetic write failure')
        else:
            assert current() == disk()
            if write_fails:
                assert current() == before
            else:
                assert 'first' in current()
        original(payload)

    monkeypatch.setattr(config, writer_name, slow_write)
    first = asyncio.create_task(save('first'))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()  # Repeated cancellation must not release the commit lock.
        if following_save:
            second = asyncio.create_task(save('second'))
        await asyncio.sleep(0)
        assert not first.done() and (second is None or not second.done())
        assert config._lock.locked() and current() == disk() == before
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 2)
        if second is not None:
            await asyncio.wait_for(second, 2)
        assert len(writes) == (2 if following_save else 1)
        assert current() == disk()
        expected = 'second' if following_save else 'old' if write_fails else 'first'
        assert expected in current()
        if kind == 'model':
            assert config.chatbot.provider.settings.model == expected
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
