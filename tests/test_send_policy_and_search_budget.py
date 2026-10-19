import asyncio
from datetime import date, datetime, timedelta
import json
import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.test_chatbot import Answers, MEMBER, context, event
from tests.test_douyin import GROUP
from tests.test_douyin_delivery import scenario
from tests.test_web_search import FakeSearchProvider
from wxhook_api.core.ai_safety import BudgetError, ModelBudget, ai_scope
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.chat_model import ChatCompletionsProvider, ModelSettings
from wxhook_api.services.chat_summary import CHINA
from wxhook_api.services.web_search import SearchSettings, WebSearchConfiguration


def pause_account_check(messenger, monkeypatch, ready=lambda: True):
    entered, release = asyncio.Event(), asyncio.Event()
    original = messenger.account_check

    async def paused():
        if ready():
            entered.set()
            await release.wait()
        return await original() if original is not None else True

    monkeypatch.setattr(messenger, 'account_check', paused)
    return entered, release


@pytest.mark.parametrize('restriction', ['blocked', 'enabled', 'chat_enabled', 'member'])
async def test_chat_rechecks_policy_after_account_rpc(tmp_path, monkeypatch, restriction):
    async with context(tmp_path, provider=Answers()) as state:
        entered, release = pause_account_check(state.bot._messenger, monkeypatch)
        try:
            await state.client.post('/receiveChatBotMsg', json=event())
            await asyncio.wait_for(entered.wait(), 2)
            path = f'/api/manage/groups/{GROUP}'
            if restriction == 'member':
                path += f'/members/{MEMBER}'
                body = {'blocked':True}
            else:
                body = {restriction:restriction == 'blocked'}
            assert (await state.client.patch(path, json=body)).status_code == 200
            release.set()
            await asyncio.wait_for(state.bot._queue.join(), 2)
            assert not state.sends
            assert state.bot.snapshot()['recent_events'][0]['status'] == 'skipped'
        finally:
            release.set()


@pytest.mark.parametrize('restriction', ['blocked', 'auto_download', 'send_results'])
async def test_media_rechecks_policy_after_account_rpc(tmp_path, monkeypatch, restriction):
    async with scenario(tmp_path, monkeypatch, images=1) as state:
        entered, release = pause_account_check(state.service.messenger, monkeypatch,
            ready=lambda: any(file.get('send_status') == 'sending'
                for job in state.service._jobs.values() for file in job['files']))
        pending = asyncio.create_task(state.submit())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if restriction == 'send_results':
                await state.service.configure(state.service.options.settings.model_copy(update={'send_results':False}))
            else:
                await state.app.state.groups.update_group(GROUP, {restriction:restriction == 'blocked'})
            release.set()
            job = await asyncio.wait_for(pending, 3)
            assert not [path for path, _ in state.hook_calls if path in {'/SendImgMsg','/send_file_msg'}]
            assert job['files'][0]['send_status'] == 'skipped'
            assert job['delivery_status'] == 'skipped'
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_notice_rechecks_notification_switch_after_account_rpc(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch, images=1) as state:
        entered, release = pause_account_check(state.service.messenger, monkeypatch)
        pending = asyncio.create_task(state.submit())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await state.service.configure(state.service.options.settings.model_copy(update={'notify_group':False}))
            release.set()
            job = await asyncio.wait_for(pending, 3)
            assert not [path for path, _ in state.hook_calls if path == '/SendTextMsg']
            assert job['notifications']['start']['status'] == 'skipped'
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize('delete', [False, True])
async def test_keyword_rechecks_rule_after_account_rpc(tmp_path, monkeypatch, delete):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        rule = dict(name='audit',keyword='trigger',enabled=True,action='reply',reply='answer',match_mode='exact',cooldown=60)
        rule_id = await tools.save_rule(GROUP, rule)
        entered, release = pause_account_check(state.bot._messenger, monkeypatch)
        pending = asyncio.create_task(tools._process_rule((GROUP,rule_id,'audit',MEMBER,'trigger')))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if delete:
                await tools.delete_rule(GROUP, rule_id)
            else:
                await tools.save_rule(GROUP, {**rule,'enabled':False}, rule_id)
            release.set()
            await asyncio.wait_for(pending, 2)
            assert not state.sends
            assert (await tools.snapshot(GROUP))['hits'][0]['status'] == 'skipped'
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_task_reminder_cancelled_during_account_rpc_is_not_sent(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.submit(event('@机器人 提醒我1小时后开会'))
        task = (await tools.snapshot(GROUP))['tasks'][0]
        sent_before = len(state.sends)
        entered, release = pause_account_check(state.bot._messenger, monkeypatch)
        pending = asyncio.create_task(tools.reminders.due_reminders(datetime.now(CHINA)+timedelta(hours=2)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await tools.task_status(GROUP, task['id'], 'cancelled')
            release.set()
            await asyncio.wait_for(pending, 2)
            assert len(state.sends) == sent_before
            assert (await tools.snapshot(GROUP))['tasks'][0]['reminder_status'] == 'skipped'
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_daily_reminder_rechecks_own_switch_after_account_rpc(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        tools._scheduler.cancel()
        await asyncio.gather(tools._scheduler, return_exceptions=True)
        await tools.save_task(GROUP, dict(title='overdue',owner='',due_date='2020-01-01'))
        await tools.preferences(GROUP, reminders=True)
        entered, release = pause_account_check(state.bot._messenger, monkeypatch)
        pending = asyncio.create_task(tools.due_reminders())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await tools.preferences(GROUP, reminders=False)
            release.set()
            await asyncio.wait_for(pending, 2)
            assert not state.sends
            status = await tools.db(lambda db: db.execute(
                'SELECT status FROM group_tool_reminders WHERE group_id=?', (GROUP,)).fetchone()[0])
            assert status == 'skipped'
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


def model_provider(budget, calls, name='test'):
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'answer'}}]})
    return ChatCompletionsProvider(ModelSettings(base_url='https://model.invalid/v1',model=name),
        httpx.MockTransport(respond), budget=budget)


@pytest.mark.parametrize('limit_scope', ['global', 'group', 'member'])
async def test_exhausted_model_budget_prevents_search_and_counts_once(limit_scope):
    calls = []
    budget = ModelBudget()
    limits = [100,100,100]
    limits[['global','group','member'].index(limit_scope)] = 1
    budget.limits = tuple(limits)
    bot = MentionChatService(ChatbotOptions(), provider=model_provider(budget, calls))
    search = AsyncMock()
    search.context.return_value = 'reference'
    bot.search_service = search
    with ai_scope(GROUP, MEMBER):
        assert await bot._answer('first') == 'answer'
        assert '额度' in await bot._answer('second')
    search.context.assert_awaited_once_with('first')
    assert len(calls) == 1 and 'reference' in calls[0]['messages'][-1]['content']
    with sqlite3.connect(budget.path) as db:
        assert [row[0] for row in db.execute('SELECT used FROM ai_usage')] == [1,1,1]


async def test_concurrent_model_admission_caps_search_too():
    calls = []
    budget = ModelBudget()
    budget.limits = (2,100,100)
    bot = MentionChatService(ChatbotOptions(), provider=model_provider(budget, calls))
    search = AsyncMock()
    search.context.return_value = 'reference'
    bot.search_service = search
    with ai_scope(GROUP, MEMBER):
        await asyncio.gather(*(bot._answer(str(i)) for i in range(8)))
    assert len(calls) == search.context.await_count == 2


async def test_model_budget_storage_failure_skips_search(monkeypatch):
    calls = []
    budget = ModelBudget()
    monkeypatch.setattr(budget, 'reserve', AsyncMock(side_effect=BudgetError('无法保存额度')))
    bot = MentionChatService(ChatbotOptions(), provider=model_provider(budget, calls))
    search = AsyncMock()
    bot.search_service = search
    assert '额度' in await bot._answer('question')
    search.context.assert_not_called()
    assert not calls


async def test_search_failure_keeps_plain_answer_and_one_model_reservation():
    calls = []
    budget = ModelBudget()
    budget.limits = (1,100,100)
    bot = MentionChatService(ChatbotOptions(), provider=model_provider(budget, calls))
    search = AsyncMock()
    search.context.side_effect = RuntimeError('synthetic failure')
    bot.search_service = search
    assert await bot._answer('question') == 'answer'
    assert len(calls) == 1 and calls[0]['messages'] == [{'role':'user','content':'question'}]
    with sqlite3.connect(budget.path) as db:
        assert db.execute("SELECT used FROM ai_usage WHERE kind='global'").fetchone()[0] == 1


async def test_model_switch_during_search_keeps_one_request_on_admitted_provider():
    calls = []
    budget = ModelBudget()
    budget.limits = (1,100,100)
    first = model_provider(budget, calls, 'first')
    second = model_provider(budget, calls, 'second')
    bot = MentionChatService(ChatbotOptions(), provider=first)
    async def switch(question):
        bot.provider = second
        return 'reference'
    search = AsyncMock()
    search.context.side_effect = switch
    bot.search_service = search
    assert await bot._answer('first question') == 'answer'
    assert '额度' in await bot._answer('second question')
    assert len(calls) == 1 and calls[0]['model'] == 'first'
    search.context.assert_awaited_once_with('first question')


@pytest.mark.parametrize('changes', [
    {'used_today':'damaged'}, {'used_today':-1}, {'used_today':True},
    {'used_today':1.5}, {'used_today':None}, {'usage_date':None},
    {'usage_date':'invalid'}, {'usage_date':'2026-10-20'}, {'usage_date':''},
])
async def test_corrupt_usage_keeps_search_disabled_and_preserves_file(tmp_path, changes):
    path = tmp_path / 'search.json'
    raw = dict(enabled=True,base_url='https://search.invalid',api_key='synthetic-only',
        result_count=5,daily_limit=100,usage_date='2026-10-19',used_today=7)
    path.write_text(json.dumps(raw | changes))
    before = path.read_bytes()
    provider = FakeSearchProvider()
    config = WebSearchConfiguration(path, provider=provider, now=lambda: date(2026,10,19))
    assert config.load_error and not config.settings.enabled and not config.snapshot()['configured']
    assert await config.context('question') is None
    assert not provider.queries and path.read_bytes() == before


@pytest.mark.parametrize('missing', ['usage_date','used_today'])
async def test_partial_usage_does_not_reset_quota(tmp_path, missing):
    path = tmp_path / 'search.json'
    raw = dict(enabled=True,base_url='https://search.invalid',api_key='synthetic-only',
        result_count=5,daily_limit=100,usage_date='2026-10-19',used_today=0)
    del raw[missing]
    path.write_text(json.dumps(raw))
    config = WebSearchConfiguration(path, provider=FakeSearchProvider(), now=lambda: date(2026,10,19))
    assert config.load_error and await config.context('question') is None


async def test_search_can_be_explicitly_reconfigured_after_bad_usage(tmp_path):
    path = tmp_path / 'search.json'
    path.write_text(json.dumps(dict(enabled=True,base_url='https://search.invalid',api_key='synthetic-only',
        result_count=5,daily_limit=2,usage_date='2026-10-19',used_today='damaged')))
    provider = FakeSearchProvider()
    config = WebSearchConfiguration(path, provider=provider, now=lambda: date(2026,10,19))
    assert config.load_error
    await config.save(SearchSettings(enabled=True,api_key='synthetic-only',daily_limit=2))
    assert not config.load_error and await config.context('question')
    restored = WebSearchConfiguration(path, now=lambda: date(2026,10,19))
    assert not restored.load_error and restored.used_today == 1
