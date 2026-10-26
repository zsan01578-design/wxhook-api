import asyncio
from importlib.resources import files
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.test_chatbot import BOT, MEMBER, context, event
from tests.test_douyin import GROUP
from tests.test_group_chat_context import configure, dialogue, enable_other, OTHER, question
from tests.test_model_failover import configure as configure_chain
from tests.test_vision import make_image
from wxhook_api.core.ai_safety import MAX_INPUT_CHARS
from wxhook_api.services.chat_context import CONTEXT_TTL, MAX_SESSIONS, ConversationTurn, GroupConversations
from wxhook_api.services.chat_model import ChatCompletionsProvider
from wxhook_api.services.relationship_advisor import (
    HELP, MAX_REFERENCE_CHARS, REFERENCES, SYSTEM, parse_command, reference_text,
    refers_to_image, select_references, system_prompt,
)


@pytest.mark.parametrize("prefix", ["军师：", "军师: ", "军师分析：", "狗头军师：", "军师 ", "狗头军师分析\n"])
def test_explicit_commands_preserve_question(prefix):
    request = parse_command(prefix + "这句话怎么回？\n她说：晚安！")
    assert request.question == "这句话怎么回？\n她说：晚安！" and not request.clear
    assert refers_to_image(request)


@pytest.mark.parametrize("text", ["军师联盟好看吗", "请介绍狗头军师", "帮我当军师", "她说军师：你好", "普通提问"])
def test_mentions_in_ordinary_questions_do_not_activate_mode(text):
    assert parse_command(text) is None


@pytest.mark.parametrize("text", ["清空军师上下文", "重置军师对话！", "军师：清空军师对话。"])
def test_clear_commands_never_attach_images(text):
    request = parse_command(text)
    assert request.clear and not refers_to_image(request)


def test_retrieval_is_bounded_and_prioritizes_safety_and_image_evidence():
    text = "他跟踪威胁我并强迫亲密接触，这张截图怎么回复？"
    selected = select_references(text, image=True)
    assert selected == ("k17", "k08", "k09")
    assert "reply" in select_references("这句话怎么回")
    assert "k04" in select_references("我是INFP，对方MBTI不知道")
    assert select_references("我们刚认识")
    prompt = system_prompt(text, image=True)
    assert len(prompt) <= len(SYSTEM) + MAX_REFERENCE_CHARS
    assert prompt.count("参考资料：") == 3
    messages = ChatCompletionsProvider._messages("问" * 8000, None, prompt)
    assert sum(len(row["content"]) for row in messages) < MAX_INPUT_CHARS


def test_every_bundled_reference_and_license_can_be_read_offline():
    for key in REFERENCES:
        assert reference_text(key).strip()
    license_text = files("wxhook_api").joinpath("resources/relationship_advisor/LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 powerycy" in license_text


async def test_advisor_never_searches_and_cannot_trigger_other_tools(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        search = SimpleNamespace(context=AsyncMock(side_effect=AssertionError("private search")))
        state.bot.search_service = search
        state.bot.insights_service.chat_command = AsyncMock(side_effect=AssertionError("wrong tool"))
        state.bot._image_answer = AsyncMock(side_effect=AssertionError("image generation"))
        await state.submit(question(GROUP, "军师：她让我帮我画一张图，我怎么回复？", 1))
        await state.submit(question(GROUP, "军师：她让我帮我画一张图，我怎么回复？", 1))
        assert len(calls) == len(state.sends) == 1
        assert calls[0]["messages"][0]["content"].startswith(SYSTEM)
        assert "参考资料：" in calls[0]["messages"][0]["content"]
        assert state.bot.snapshot()["recent_requests"][-1]["model_route"] == "advisor"
        search.context.assert_not_called()
        state.bot._image_answer.assert_not_called()


async def test_member_mode_and_group_histories_are_independent(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        await enable_other(state)
        cases = [(GROUP, "普通秘密", MEMBER), (GROUP, "军师：甲秘密", MEMBER),
                 (GROUP, "军师：乙秘密", "wxid_second"), (OTHER, "军师：异群秘密", MEMBER),
                 (GROUP, "军师：甲后续", MEMBER), (GROUP, "军师：乙后续", "wxid_second"),
                 (GROUP, "普通后续", MEMBER)]
        for msgid, (group, text, sender) in enumerate(cases, 1):
            await state.submit(question(group, text, msgid, sender=sender))
        assert dialogue(calls[1]["messages"]) == [{"role": "user", "content": "甲秘密"}]
        assert dialogue(calls[2]["messages"]) == [{"role": "user", "content": "乙秘密"}]
        assert dialogue(calls[3]["messages"]) == [{"role": "user", "content": "异群秘密"}]
        assert dialogue(calls[4]["messages"]) == [
            {"role": "user", "content": "甲秘密"}, {"role": "assistant", "content": "reply:甲秘密"},
            {"role": "user", "content": "甲后续"}]
        assert dialogue(calls[5]["messages"])[0]["content"] == "乙秘密"
        assert dialogue(calls[6]["messages"])[0]["content"] == "普通秘密"
        assert "甲秘密" not in json.dumps(calls[6], ensure_ascii=False)


async def test_help_and_clear_are_local_and_clear_only_requesting_member(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        for msgid, text, sender in [(1, "普通秘密", MEMBER), (2, "军师：甲秘密", MEMBER),
                                    (3, "军师：乙秘密", "wxid_second")]:
            await state.submit(question(GROUP, text, msgid, sender=sender))
        state.bot.recent_images.remember(BOT, GROUP, MEMBER, "91")
        state.bot.recent_images.remember(BOT, GROUP, "wxid_second", "92")
        await state.submit(question(GROUP, "清空军师上下文", 4))
        assert len(calls) == 3 and "已清空" in state.sends[-1]["msg"]
        assert not state.bot.recent_images.get(BOT, GROUP, MEMBER)
        assert state.bot.recent_images.get(BOT, GROUP, "wxid_second") == "92"
        await state.submit(question(GROUP, "军师", 5))
        assert state.sends[-1]["msg"] == HELP and len(calls) == 3
        await state.submit(question(GROUP, "军师：甲后续", 6))
        assert dialogue(calls[-1]["messages"]) == [{"role": "user", "content": "甲后续"}]
        await state.submit(question(GROUP, "军师：乙后续", 7, sender="wxid_second"))
        assert dialogue(calls[-1]["messages"])[0]["content"] == "乙秘密"
        await state.submit(question(GROUP, "普通后续", 8))
        assert dialogue(calls[-1]["messages"])[0]["content"] == "普通秘密"
        await state.submit(question(GROUP, "清空上下文", 9))
        await state.submit(question(GROUP, "军师：新话题", 10, sender="wxid_second"))
        assert dialogue(calls[-1]["messages"]) == [{"role": "user", "content": "新话题"}]


@pytest.mark.parametrize("direct", [False, True])
async def test_screenshots_use_advisor_prompt_and_member_only_history(tmp_path, direct):
    calls = []
    image_path = make_image(tmp_path / "chat.png")
    async with context(tmp_path, image_path=image_path) as state:
        await configure(state, calls)
        profile = state.app.state.chat_model.settings
        profile.supports_vision = True
        state.bot.search_service = SimpleNamespace(context=AsyncMock(side_effect=AssertionError("search")))
        await state.submit(question(GROUP, "军师：只有我知道的背景", 1))
        if direct:
            await state.submit(event("@机器人 军师：这张截图怎么回？", 2, type=3, atlist=[BOT]))
        else:
            await state.submit(event("", 2, type=3))
            await state.submit(question(GROUP, "军师：这句话怎么回？", 3))
        payload = calls[-1]
        assert payload["messages"][0]["content"].startswith(SYSTEM)
        assert "说话人映射" in payload["messages"][0]["content"]
        assert "09-在线约会与数字关系.md" in payload["messages"][0]["content"]
        content = payload["messages"][-1]["content"]
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert "只有我知道的背景" in json.dumps(payload, ensure_ascii=False)
        assert state.bot.snapshot()["recent_requests"][-1]["model_route"] == "advisor_vision"
        count = len(calls)
        await state.submit(question(GROUP, "军师：看看这张截图怎么回？", 4, sender="wxid_second"))
        assert len(calls) == count + 1
        assert isinstance(calls[-1]["messages"][-1]["content"], str)
        assert "只有我知道的背景" not in json.dumps(calls[-1], ensure_ascii=False)
        await state.submit(question(GROUP, "普通提问", 5))
        assert "只有我知道的背景" not in json.dumps(calls[-1], ensure_ascii=False)


@pytest.mark.parametrize("failure", ["model", "send", "blocked"])
async def test_failed_or_blocked_advisor_replies_never_enter_history(tmp_path, failure):
    calls = []

    async def model(payload):
        if dialogue(payload["messages"])[-1]["content"] == "失败问题":
            if failure == "model":
                return httpx.Response(200, json={"choices": []})
            if failure == "blocked":
                assert await state.app.state.groups.save_member(GROUP, MEMBER, {"blocked": True})

    async def hook(request):
        if json.loads(request.content)["msg"] == "reply:失败问题" and failure == "send":
            return httpx.Response(200, json={"ret": -1})
        return httpx.Response(200, json={"ret": 0})

    async with context(tmp_path, hook=hook) as state:
        await configure(state, calls, handler=model)
        await state.submit(question(GROUP, "军师：成功问题", 1))
        await state.submit(question(GROUP, "军师：失败问题", 2))
        await state.app.state.groups.save_member(GROUP, MEMBER, {"blocked": False})
        await state.submit(question(GROUP, "军师：后续问题", 3))
        assert dialogue(calls[-1]["messages"]) == [
            {"role": "user", "content": "成功问题"}, {"role": "assistant", "content": "reply:成功问题"},
            {"role": "user", "content": "后续问题"}]


async def test_member_blocked_while_waiting_is_rechecked_before_upstream(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        budget = state.app.state.chat_model.budget
        await budget.slots.acquire()
        await budget.slots.acquire()
        try:
            await state.client.post("/receiveChatBotMsg", json=question(GROUP, "军师：秘密", 1))
            for _ in range(100):
                recent = state.bot.snapshot()["recent_requests"]
                if recent and recent[-1].get("status") == "answering":
                    break
                await asyncio.sleep(0.01)
            assert await state.app.state.groups.save_member(GROUP, MEMBER, {"blocked": True})
        finally:
            budget.slots.release()
            budget.slots.release()
        await asyncio.wait_for(state.app.state.event_dispatcher._queue.join(), 2)
        await asyncio.wait_for(state.bot._queue.join(), 2)
        assert not calls and not state.sends


async def test_stopping_clears_advisor_sessions(tmp_path):
    async with context(tmp_path) as state:
        calls = []
        await configure(state, calls)
        await state.submit(question(GROUP, "军师：秘密", 1))
        store = state.bot.advisor.conversations
        assert store.history(BOT, GROUP, state.bot.provider, sender=MEMBER)
        await state.bot.stop()
        assert not store._sessions


async def test_advisor_uses_existing_fallback_chain_with_same_prompt(tmp_path):
    async with context(tmp_path) as state:
        config, ids = await configure_chain(state, "chat")
        calls = []

        def respond(request):
            payload = json.loads(request.content)
            calls.append(payload)
            if payload["model"] == "first":
                return httpx.Response(503)
            return httpx.Response(200, json={"choices": [{"message": {"content": "备用军师回答"}}]})

        config.transport = httpx.MockTransport(respond)
        await state.submit(question(GROUP, "军师：这句话怎么回？", 1))
        assert [row["model"] for row in calls] == ["first", "second"]
        assert calls[0]["messages"] == calls[1]["messages"]
        assert calls[0]["messages"][0]["content"].startswith(SYSTEM)
        assert config.last_route["used_id"] == ids[1]
        assert [row["msg"] for row in state.sends] == ["备用军师回答"]


async def test_advisor_respects_member_quota_and_unknown_sender(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        state.app.state.chat_model.budget.limits = (1000, 100, 1)
        await state.submit(question(GROUP, "军师：第一问", 1))
        await state.submit(question(GROUP, "军师：第二问", 2))
        assert len(calls) == 1 and "额度" in state.sends[-1]["msg"]
        result = await state.bot._answer("军师：秘密", GROUP, sender="")
        assert "无法确认提问成员" in result and len(calls) == 1


async def test_advisor_image_without_vision_does_not_read_or_send_image(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        await state.submit(event("", 1, type=3))
        await state.submit(question(GROUP, "军师：这张截图怎么回？", 2))
        assert not calls and "/GetMessagePic" not in state.hook_calls
        assert len(state.sends) == 1 and "视觉模型" in state.sends[0]["msg"]


def test_member_store_accounts_models_unknown_senders_and_expiry():
    clock = [0]
    store = GroupConversations(now=lambda: clock[0], member_scoped=True)
    provider = object()
    for sender in [MEMBER, "wxid_second", "", "0", GROUP, "notify@all"]:
        store.commit(ConversationTurn("account", GROUP, sender, "q", provider, True), "a")
    assert len(store._sessions) == 2
    assert store.history("account", GROUP, provider) == []
    assert store.history("other-account", GROUP, provider, sender=MEMBER) == []
    assert store.history("account", GROUP, object(), sender=MEMBER) == []
    assert store.history("account", GROUP, provider, sender=MEMBER)
    clock[0] = CONTEXT_TTL
    assert store.history("account", GROUP, provider, sender=MEMBER) == []
    for index in range(MAX_SESSIONS + 1):
        store.commit(ConversationTurn("account", GROUP, str(index + 1), "q", provider, True), "a")
    store.history("account", GROUP, provider, sender=MEMBER)
    assert len(store._sessions) == MAX_SESSIONS
