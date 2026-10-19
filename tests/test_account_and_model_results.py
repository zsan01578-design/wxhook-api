import asyncio
from datetime import datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.test_chatbot import BOT, Answers, context, event
from tests.test_douyin import GROUP, SHORT, options
from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.chat_model import ChatCompletionsProvider, ModelError, ModelProfile
from wxhook_api.services.chat_summary import CHINA, local_date
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.douyin_delivery import DeliveryError, DouyinMessenger
from wxhook_api.services.events import EventDispatcher
from wxhook_api.services.group_tools import report_period
from wxhook_api.services.web_search import WebSearchConfiguration
from wxhook_api.services.wechat_sync import SyncOptions


async def test_sync_account_rejection_stops_entire_callback_and_all_group_automation(tmp_path):
    requests = []
    def respond(request):
        requests.append(request.url.path)
        assert request.url.path == '/GetSelfProfile'
        return httpx.Response(200, json={'wxid':'wxid_other', 'nickname':'机器人'})
    answers = Answers()
    bot = MentionChatService(ChatbotOptions(True, BOT, ('机器人',)), provider=answers)
    async with HookClient('http://hook.invalid', transport=httpx.MockTransport(respond)) as hook:
        app = create_app(hook_client=hook, chat_service=bot,
            douyin_service=DouyinService(options(tmp_path)), sync_options=SyncOptions(enabled=True))
        groups, dispatcher = app.state.groups, app.state.event_dispatcher
        await groups.start([GROUP])
        await groups.update_group(GROUP, {'chat_enabled':True})
        await groups.claim_account(BOT)
        app.state.wechat_sync.client = hook
        observed = AsyncMock()
        dispatcher.register(observed)
        await bot.start(hook)
        await dispatcher.start()
        try:
            await dispatcher.enqueue(event())
            await asyncio.wait_for(dispatcher._queue.join(), 2)
            await asyncio.wait_for(bot._queue.join(), 2)
            observed.assert_not_called()
            assert not answers.questions and requests == ['/GetSelfProfile']
            assert (await groups.messages(GROUP))['total'] == 0
            assert groups.account_error and not groups.allowed(GROUP)
            assert not app.state.douyin.allowed(GROUP)
            assert not bot.snapshot()['identity_known']
        finally:
            await dispatcher.stop()
            await bot.stop()


async def test_critical_callback_failure_does_not_break_next_event():
    dispatcher = EventDispatcher()
    guard = AsyncMock(side_effect=[ValueError('synthetic failure'), None])
    after = AsyncMock()
    dispatcher.register(guard, critical=True)
    dispatcher.register(after)
    await dispatcher.start()
    try:
        await dispatcher.enqueue(event(msgid=1))
        await dispatcher.enqueue(event(msgid=2))
        await asyncio.wait_for(dispatcher._queue.join(), 2)
        assert guard.await_count == 2 and after.await_count == 1
        assert str(after.call_args.args[0].msgid) == '2'
    finally:
        await dispatcher.stop()


async def test_fixed_identity_is_verified_even_with_synchronization_disabled(tmp_path):
    answers = Answers()
    async with context(tmp_path, provider=answers,
                       profile={'wxid':'wxid_other', 'nickname':'机器人'}) as state:
        await state.submit()
        assert not answers.questions and not state.sends
        assert state.hook_calls == ['/GetSelfProfile']
        assert state.app.state.groups.account_error
        assert not state.media.allowed(GROUP)
        assert (await state.app.state.groups.messages(GROUP))['total'] == 0


async def test_account_switch_during_answer_blocks_send_and_queued_question(tmp_path):
    profile = {'wxid':BOT, 'nickname':'机器人'}
    entered, release = asyncio.Event(), asyncio.Event()
    class Paused(Answers):
        async def answer(self, question):
            self.questions.append(question)
            entered.set()
            await release.wait()
            return 'answer that must not be sent'
    answers = Paused()
    async with context(tmp_path, provider=answers, profile=profile) as state:
        try:
            await state.client.post('/receiveChatBotMsg', json=event())
            await asyncio.wait_for(entered.wait(), 2)
            await state.client.post('/receiveChatBotMsg', json=event(msgid=2))
            await asyncio.wait_for(state.app.state.event_dispatcher._queue.join(), 2)
            profile['wxid'] = 'wxid_other'
            release.set()
            await asyncio.wait_for(state.bot._queue.join(), 2)
            assert len(answers.questions) == 1 and not state.sends
            assert state.app.state.groups.account_error
            # Switching back must not silently resume queued work.
            profile['wxid'] = BOT
            assert not await state.bot.verify_account()
        finally:
            release.set()


@pytest.mark.parametrize('kind', ['summary', 'report', 'tasks'])
async def test_group_ai_checks_current_account_before_outbound_request(tmp_path, kind):
    profile = {'wxid':BOT, 'nickname':'机器人'}
    async with context(tmp_path, profile=profile) as state:
        groups, config = state.app.state.groups, state.app.state.chat_model
        await groups.claim_account(BOT)
        timestamp = int(datetime.now(CHINA).timestamp())
        if kind == 'report':
            start, _ = report_period('week')
            timestamp = int(datetime.combine(start, datetime.min.time(), CHINA).timestamp())
        await groups.import_history(GROUP, [dict(msgid=1,timestamp=timestamp,type=1,sender='member',content='private record')])
        saved = ModelProfile(id='test',base_url='https://model.invalid/v1',model='test')
        config.models, config.active_id = {'test':saved}, 'test'
        outbound = []
        config.transport = httpx.MockTransport(lambda request: outbound.append(request))
        profile['wxid'] = 'wxid_other'
        with pytest.raises(ModelError):
            if kind == 'summary':
                await state.app.state.chat_summary.get(GROUP, force=True)
            elif kind == 'report':
                await state.app.state.group_tools.report(GROUP, 'week')
            else:
                await state.app.state.group_tools.extract_tasks(GROUP)
        assert not outbound and groups.account_error


async def test_media_job_rechecks_account_after_queue_wait(tmp_path):
    profile = {'wxid':BOT, 'nickname':'机器人'}
    async with context(tmp_path, profile=profile) as state:
        async with state.media._maintenance_lock:
            await state.media.receive(CallbackEvent.model_validate(event(content=SHORT)))
            profile['wxid'] = 'wxid_other'
        await asyncio.wait_for(state.media._queue.join(), 2)
        assert not state.media_calls and not state.sends
        assert state.media.snapshot()['jobs'][0]['status'] == 'failed'
        assert state.app.state.groups.account_error


@pytest.mark.parametrize('mode', ['image', 'file'])
async def test_media_send_checks_account_before_hook_send(tmp_path, mode):
    async with context(tmp_path, profile={'wxid':'wxid_other','nickname':'机器人'}) as state:
        messenger = DouyinMessenger(state.hook, account_check=state.bot.verify_account)
        with pytest.raises(DeliveryError):
            await messenger.send(GROUP, Path('unused'), mode)
        assert state.hook_calls == ['/GetSelfProfile']


@pytest.mark.parametrize('reason', ['length', 'max_tokens', 'content_filter', 'tool_calls'])
async def test_incomplete_model_output_is_not_accepted_or_echoed(reason):
    profile = ModelProfile(id='test',base_url='https://model.invalid/v1',model='test')
    provider = ChatCompletionsProvider(profile, httpx.MockTransport(lambda request:
        httpx.Response(200, json={'choices':[{'finish_reason':reason,'message':{'content':'private partial output'}}]})))
    with pytest.raises(ModelError) as error:
        await provider.answer('question')
    assert 'private partial output' not in str(error.value)


@pytest.mark.parametrize('choice', [None, 1, 'invalid', []])
async def test_malformed_choice_remains_a_safe_model_error(choice):
    profile = ModelProfile(id='test',base_url='https://model.invalid/v1',model='test')
    provider = ChatCompletionsProvider(profile, httpx.MockTransport(lambda request:
        httpx.Response(200, json={'choices':[choice]})))
    with pytest.raises(ModelError):
        await provider.answer('question')


async def test_truncated_model_test_does_not_save_configuration(tmp_path):
    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(lambda request: httpx.Response(200,
            json={'choices':[{'finish_reason':'length','message':{'content':'partial'}}]}))
        response = await state.client.post('/api/manage/chat-model', json={'base_url':'https://model.invalid/v1','model':'test'})
        assert not response.json()['saved']
        assert not config.path.exists() and state.bot.provider is None


async def test_truncated_answer_is_not_sent_as_a_normal_reply(tmp_path):
    profile = ModelProfile(id='test',base_url='https://model.invalid/v1',model='test')
    provider = ChatCompletionsProvider(profile, httpx.MockTransport(lambda request:
        httpx.Response(200, json={'choices':[{'finish_reason':'length','message':{'content':'private partial output'}}]})))
    async with context(tmp_path, provider=provider) as state:
        await state.submit()
        assert len(state.sends) == 1
        assert 'private partial output' not in state.sends[0]['msg']
        assert '截断' in state.sends[0]['msg']


async def test_truncated_summary_does_not_overwrite_good_cache(tmp_path):
    async with context(tmp_path) as state:
        groups, config = state.app.state.groups, state.app.state.chat_model
        await groups.import_history(GROUP, [dict(msgid=1,timestamp=int(datetime.now(CHINA).timestamp()),
            type=1,sender='member',content='record')])
        await groups.save_summary(GROUP, local_date(), 'previous good summary', 1)
        saved = ModelProfile(id='test',base_url='https://model.invalid/v1',model='test')
        config.models, config.active_id = {'test':saved}, 'test'
        config.transport = httpx.MockTransport(lambda request: httpx.Response(200,
            json={'choices':[{'finish_reason':'length','message':{'content':'partial summary'}}]}))
        with pytest.raises(ModelError):
            await state.app.state.chat_summary.get(GROUP, force=True)
        assert (await groups.get_summary(GROUP, local_date()))['summary'] == 'previous good summary'


@pytest.mark.parametrize('key', [None, '', '   ', '\t \n'])
@pytest.mark.parametrize('enabled', [False, True])
async def test_blank_legacy_search_key_does_not_break_startup(tmp_path, monkeypatch, key, enabled):
    path = tmp_path / 'legacy-search.json'
    path.write_text(json.dumps(dict(enabled=enabled,base_url='https://api.deepseek.com/v1',
        api_key=key,result_count=5,daily_limit=100,usage_date='2026-10-19',used_today=7)))
    monkeypatch.setenv('WXHOOK_SEARCH_CONFIG', str(path))
    app = create_app()
    config = app.state.chat_search
    assert not config.load_error and not config.snapshot()['has_api_key']
    assert config.used_today == 7 and config.settings.enabled is enabled
    assert await config.context('question') is None
    assert not WebSearchConfiguration(path).load_error
