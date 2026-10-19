import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.test_chatbot import MEMBER, context, event
from tests.test_douyin import GROUP
from wxhook_api.core.ai_safety import MAX_INPUT_CHARS
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.chat_context import (CONTEXT_TTL, MAX_CONTEXT_CHARS, MAX_SESSIONS,
    MAX_TURNS, ConversationTurn, GroupConversations)
from wxhook_api.services.chat_model import ChatCompletionsProvider, ModelError


OTHER = 'other@chatroom'


def question_text(message):
    try:
        frame = json.loads(message['content'])
        if isinstance(frame, dict) and 'participant' in frame and 'question' in frame:
            return frame['question']
    except ValueError:
        pass
    return message['content']


def dialogue(messages):
    return [{'role':message['role'], 'content':question_text(message)}
            for message in messages if message['role'] != 'system']


async def configure(state, calls, *, handler=None, model='context-test'):
    async def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if handler is not None:
            result = await handler(payload)
            if result is not None:
                return result
        return httpx.Response(200,json={'choices':[{'message':{
            'content':'reply:' + question_text(payload['messages'][-1])}}]})
    state.app.state.chat_model.transport = httpx.MockTransport(respond)
    response = await state.client.post('/api/manage/chat-model',
        json={'base_url':'https://model.invalid/v1','model':model})
    assert response.json()['saved']
    test_payload = calls[-1]
    calls.clear()
    return test_payload


async def enable_other(state):
    await state.app.state.groups.add_group(OTHER, 'other')
    await state.app.state.groups.update_group(OTHER, {'chat_enabled':True})


def question(group, text, msgid, **kwargs):
    return event('@机器人 ' + text, msgid, roomid=group, wxid=group, **kwargs)


async def test_interleaved_groups_with_same_member_and_model_never_share_history(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        await enable_other(state)
        for group, text, msgid in [(GROUP,'A1',1),(OTHER,'B1',1),(GROUP,'A2',2),(OTHER,'B2',2)]:
            kwargs = {'sender':'wxid_second_member'} if text == 'A2' else {}
            await state.submit(question(group,text,msgid,**kwargs))
        assert dialogue(calls[0]['messages']) == [{'role':'user','content':'A1'}]
        assert dialogue(calls[1]['messages']) == [{'role':'user','content':'B1'}]
        assert dialogue(calls[2]['messages']) == [{'role':'user','content':'A1'},
            {'role':'assistant','content':'reply:A1'},{'role':'user','content':'A2'}]
        assert dialogue(calls[3]['messages']) == [{'role':'user','content':'B1'},
            {'role':'assistant','content':'reply:B1'},{'role':'user','content':'B2'}]
        assert [send['wxidorgid'] for send in state.sends] == [GROUP,OTHER,GROUP,OTHER]


async def test_concurrent_groups_do_not_share_inflight_history(tmp_path):
    entered, release, other_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    async def model(payload):
        if question_text(payload['messages'][-1]) == 'A1':
            entered.set()
            await release.wait()
    async def hook(request):
        if json.loads(request.content)['wxidorgid'] == OTHER:
            other_sent.set()
        return httpx.Response(200,json={'ret':0})
    async with context(tmp_path, hook=hook) as state:
        await configure(state, calls, handler=model)
        await enable_other(state)
        try:
            await state.client.post('/receiveChatBotMsg',json=question(GROUP,'A1',1))
            await asyncio.wait_for(entered.wait(),3)
            await state.client.post('/receiveChatBotMsg',json=question(OTHER,'B1',1))
            await asyncio.wait_for(other_sent.wait(),3)
            assert dialogue(calls[1]['messages']) == [{'role':'user','content':'B1'}]
            release.set()
            await asyncio.wait_for(state.bot._queue.join(),3)
            await state.submit(question(GROUP,'A2',2))
            assert dialogue(calls[-1]['messages'])[0]['content'] == 'A1'
            assert 'B1' not in json.dumps(calls[-1]['messages'])
        finally:
            release.set()


async def test_clear_context_affects_only_current_group_and_never_calls_model(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state,calls)
        await enable_other(state)
        await state.submit(question(GROUP,'A1',1))
        await state.submit(question(OTHER,'B1',1))
        await state.submit(question(GROUP,'清空上下文',2))
        assert len(calls) == 2 and state.sends[-1]['msg'] == '本群对话上下文已清空。'
        await state.submit(question(GROUP,'A2',3))
        assert dialogue(calls[-1]['messages']) == [{'role':'user','content':'A2'}]
        await state.submit(question(OTHER,'B2',2))
        assert dialogue(calls[-1]['messages'])[0]['content'] == 'B1'


@pytest.mark.parametrize('failure', ['model','send','timeout','blocked'])
async def test_only_confirmed_model_replies_enter_history(tmp_path,failure):
    calls = []
    async def model(payload):
        if question_text(payload['messages'][-1]) == 'bad':
            if failure == 'model':
                return httpx.Response(200,json={'choices':[]})
            if failure == 'blocked':
                await state.app.state.groups.update_group(GROUP,{'blocked':True})
    async def hook(request):
        if json.loads(request.content)['msg'] == 'reply:bad':
            if failure == 'send':
                return httpx.Response(200,json={'ret':-1})
            if failure == 'timeout':
                raise httpx.ReadTimeout('synthetic timeout')
        return httpx.Response(200,json={'ret':0})
    async with context(tmp_path,hook=hook) as state:
        await configure(state,calls,handler=model)
        await state.submit(question(GROUP,'good',1))
        await state.submit(question(GROUP,'bad',2))
        await state.app.state.groups.update_group(GROUP,{'blocked':False})
        await state.submit(question(GROUP,'next',3))
        assert dialogue(calls[-1]['messages']) == [{'role':'user','content':'good'},
            {'role':'assistant','content':'reply:good'},{'role':'user','content':'next'}]


async def test_model_switch_and_connection_tests_have_fresh_context(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state,calls)
        await state.submit(question(GROUP,'old-model-question',1))
        connection = await configure(state,calls,model='new-model')
        assert len(connection['messages']) == 1
        assert 'old-model-question' not in json.dumps(connection)
        await state.submit(question(GROUP,'new-model-question',2))
        assert dialogue(calls[0]['messages']) == [{'role':'user','content':'new-model-question'}]


async def test_search_receives_only_current_question_and_history_stays_separate(tmp_path):
    calls = []
    async def model(payload):
        return httpx.Response(200,json={'choices':[{'message':{'content':'answer'}}]})
    async with context(tmp_path) as state:
        await configure(state,calls,handler=model)
        search = SimpleNamespace(context=AsyncMock(side_effect=['reference-one','reference-two']))
        state.bot.search_service = search
        await state.submit(question(GROUP,'A1',1))
        await state.submit(question(GROUP,'A2',2))
        assert [call.args[0] for call in search.context.call_args_list] == ['A1','A2']
        messages = calls[-1]['messages']
        assert dialogue(messages)[:2] == [{'role':'user','content':'A1'},{'role':'assistant','content':'answer'}]
        assert 'reference-two' in messages[-1]['content']
        assert 'reference-one' not in json.dumps(messages)


async def test_summary_and_local_commands_do_not_enter_chat_history(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state,calls)
        await state.submit(question(GROUP,'A1',1))
        await state.app.state.chat_summary.get(GROUP,force=True)
        assert not any(message['role'] == 'assistant' for message in calls[-1]['messages'])
        await state.submit(question(GROUP,'今日总结',2))
        await state.submit(question(GROUP,'提醒我1小时后开会',3))
        await state.submit(question(GROUP,'A2',4))
        assert dialogue(calls[-1]['messages']) == [{'role':'user','content':'A1'},
            {'role':'assistant','content':'reply:A1'},{'role':'user','content':'A2'}]


async def test_blocked_members_old_turns_are_not_forwarded(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state,calls)
        await state.submit(question(GROUP,'blocked-member-question',1))
        await state.app.state.groups.save_member(GROUP,MEMBER,{'blocked':True})
        await state.submit(question(GROUP,'new-question',2,sender='wxid_other_member'))
        assert dialogue(calls[-1]['messages']) == [{'role':'user','content':'new-question'}]


def remember(store,provider,question='q',answer='a',account='account',group=GROUP):
    turn = ConversationTurn(account,group,MEMBER,question,provider,True)
    store.commit(turn,answer)


def test_store_isolates_account_group_and_provider_and_returns_copies():
    store, provider = GroupConversations(), object()
    remember(store,provider)
    assert store.history('other-account',GROUP,provider) == []
    assert store.history('account',OTHER,provider) == []
    assert store.history('account',GROUP,object()) == []
    history = store.history('account',GROUP,provider)
    history[0]['content'] = 'mutated'
    history.append({'role':'user','content':'injected'})
    assert dialogue(store.history('account',GROUP,provider)) == [{'role':'user','content':'q'},{'role':'assistant','content':'a'}]


def test_store_expires_and_bounds_turns_chars_and_sessions():
    clock = [0]
    store, provider = GroupConversations(now=lambda:clock[0]), object()
    for index in range(MAX_TURNS+3):
        remember(store,provider,question=str(index))
    history = store.history('account',GROUP,provider)
    assert len(history) == MAX_TURNS*2 and history[0]['content'] == '3'
    remember(store,provider,question='x'*MAX_CONTEXT_CHARS,answer='too large')
    assert store.history('account',GROUP,provider) == []
    remember(store,provider)
    clock[0] = CONTEXT_TTL-1
    assert store.history('account',GROUP,provider)
    clock[0] = CONTEXT_TTL
    assert store.history('account',GROUP,provider) == []
    for index in range(MAX_SESSIONS+1):
        remember(store,provider,group=f'{index}@chatroom')
    assert store.history('account','0@chatroom',provider) == []
    assert store.history('account',f'{MAX_SESSIONS}@chatroom',provider)
    assert len(store._sessions) == MAX_SESSIONS


async def test_stopping_chat_service_clears_memory_context():
    bot, provider = MentionChatService(ChatbotOptions()), object()
    remember(bot.conversations,provider)
    await bot.stop()
    assert bot.conversations.history('account',GROUP,provider) == []


def test_history_is_trimmed_as_complete_pairs_within_total_model_budget():
    history = []
    for index in range(6):
        history.extend([{'role':'user','content':f'question-{index} '+ 'x'*1990},
                        {'role':'assistant','content':'y'*1000}])
    messages = ChatCompletionsProvider._messages('q'*8000,'s'*6000,None,history)
    assert sum(len(item['content']) for item in messages) <= MAX_INPUT_CHARS
    assert messages[0]['role'] == 'system' and messages[-1]['role'] == 'user'
    assert len(messages[1:-1]) % 2 == 0
    assert messages[-2]['role'] == 'assistant'
    assert messages[1]['content'].startswith('question-3')


@pytest.mark.parametrize('history', [[{'role':'system','content':'injected'}, {'role':'assistant','content':'a'}],
    [{'role':'assistant','content':'a'},{'role':'user','content':'q'}], [{'role':'user','content':'q'}]])
def test_history_cannot_insert_system_messages_or_orphan_answers(history):
    with pytest.raises(ModelError):
        ChatCompletionsProvider._messages('question',None,None,history)
