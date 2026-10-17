import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

from asgi_lifespan import LifespanManager
import httpx
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, PNG, SHORT, image_item, options, page
from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.chatbot_config import ChatbotOptions, load_chatbot_options
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.groups import GroupManager


BOT = "wxid_robot"
MEMBER = "wxid_member"


class Answers:
    def __init__(self, result="这是回答", error=None):
        self.result, self.error, self.questions = result, error, []

    async def answer(self, question):
        self.questions.append(question)
        if self.error:
            raise self.error
        return self.result


def event(content="@机器人\u2005你好", msgid=1, **extra):
    return {**EVENT, "roomid": GROUP, "wxid": GROUP, "sender": MEMBER, "msgid": msgid, "content": content, **extra}


@asynccontextmanager
async def context(tmp_path, *, provider=None, bot_options=None, hook=None, profile=None):
    state = SimpleNamespace(sends=[], hook_calls=[], media_calls=[])

    async def hook_response(request):
        body = json.loads(request.content)
        state.hook_calls.append(request.url.path)
        if request.url.path == "/GetSelfProfile":
            return httpx.Response(200, json=profile or {"wxid": BOT, "nickname": "机器人"})
        assert request.url.path == "/SendTextMsg", "Unexpected outbound Hook operation"
        state.sends.append(body)
        return await hook(request) if hook else httpx.Response(200, json={"ret": 0})

    def media_response(request):
        state.media_calls.append(str(request.url))
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": "https://www.douyin.com/note/123456789"})
        if request.url.host == "www.douyin.com":
            item = image_item(1)
            item["aweme_id"] = "123456789"
            return httpx.Response(200, text=page(item))
        return httpx.Response(200, content=PNG)

    state.media = DouyinService(options(tmp_path), transport=httpx.MockTransport(media_response))
    state.bot = MentionChatService(bot_options or ChatbotOptions(True, BOT, ("机器人",)), provider=provider)
    state.hook = HookClient("http://hook.test", transport=httpx.MockTransport(hook_response))
    state.app = create_app(douyin_service=state.media, hook_client=state.hook, chat_service=state.bot,
                           group_manager=GroupManager(tmp_path / "management.sqlite3"))
    try:
        async with LifespanManager(state.app), httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app), base_url="http://test") as client:
            state.client = client

            async def submit(payload=None):
                response = await client.post("/receiveChatBotMsg", json=payload or event())
                assert response.status_code == 200
                await asyncio.wait_for(state.app.state.event_dispatcher._queue.join(), 2)
                await asyncio.wait_for(state.bot._queue.join(), 2)
                await asyncio.wait_for(state.media._queue.join(), 2)

            state.submit = submit
            yield state
    finally:
        await state.hook.aclose()


async def test_no_provider_gives_one_honest_configuration_message(tmp_path):
    async with context(tmp_path) as state:
        await state.submit()
        await state.submit()  # Same callback must not repeat even the fallback.
        assert state.sends == [{"wxidorgid": GROUP, "msg": "问答接口尚未配置，请联系管理员。"}]
        assert state.bot.snapshot()["answer_configured"] is False


async def test_each_new_message_gets_one_answer_and_no_progress_messages(tmp_path):
    provider = Answers()
    async with context(tmp_path, provider=provider) as state:
        await state.submit()
        await state.submit(event("@机器人\u2005第二个问题", 2))
        assert provider.questions == ["你好", "第二个问题"]
        assert [body["msg"] for body in state.sends] == ["这是回答", "这是回答"]
        assert state.hook_calls == ["/SendTextMsg", "/SendTextMsg"]


@pytest.mark.parametrize("payload", [event("普通聊天"), event("@其他人\u2005你好"), event(roomid="", wxid=MEMBER), event(sender=BOT), event(is_self=True), event(type=49)])
async def test_plain_chat_other_mentions_private_self_and_nontext_are_ignored(tmp_path, payload):
    provider = Answers()
    async with context(tmp_path, provider=provider) as state:
        await state.submit(payload)
        assert not provider.questions and not state.sends


@pytest.mark.parametrize("metadata", [{"atlist": [BOT]}, {"at_list": BOT}, {"atuserlist": json.dumps([BOT])}, {"source": f"<msgsource><atuserlist><![CDATA[{BOT}]]></atuserlist></msgsource>"}])
async def test_structured_mentions_work_with_group_specific_display_names(tmp_path, metadata):
    provider = Answers()
    async with context(tmp_path, provider=provider) as state:
        await state.submit(event("@群内别名\u2005你好", **metadata))
        assert len(provider.questions) == 1 and len(state.sends) == 1


@pytest.mark.parametrize("ids", [["notify@all"], ["wxid_other"]])
async def test_structured_ids_override_a_display_name_in_the_text(tmp_path, ids):
    async with context(tmp_path, provider=Answers()) as state:
        await state.submit(event(atlist=ids))
        assert state.sends == []


async def test_multiple_mentions_and_long_answer_still_emit_one_message(tmp_path):
    provider = Answers("答" * 400)
    async with context(tmp_path, provider=provider, bot_options=ChatbotOptions(True, BOT, ("机器人",), 100)) as state:
        await state.submit(event("@机器人\u2005@机器人\u2005你好", atlist=[BOT, BOT]))
        assert provider.questions == ["你好"]
        assert len(state.sends) == 1 and len(state.sends[0]["msg"]) == 100
        assert state.sends[0]["msg"].endswith("…")


async def test_profile_is_discovered_once_and_missing_roomid_uses_wxid(tmp_path):
    provider = Answers()
    async with context(tmp_path, provider=provider, bot_options=ChatbotOptions()) as state:
        payload = event()
        payload.pop("roomid")
        await state.submit(payload)
        await state.submit(event(msgid=2))
        assert state.hook_calls.count("/GetSelfProfile") == 1
        assert len(state.sends) == 2


async def test_failed_identity_check_cannot_be_reused_as_verified_identity(tmp_path):
    async with context(tmp_path, provider=Answers(), bot_options=ChatbotOptions(True, BOT), profile={"wxid": "wxid_other", "nickname": "机器人"}) as state:
        await state.submit(event(atlist=[BOT]))
        await state.submit(event(msgid=2, atlist=[BOT]))
        assert not state.sends
        assert state.hook_calls == ["/GetSelfProfile"]


async def test_mention_containing_media_url_does_not_trigger_both_features(tmp_path):
    async with context(tmp_path, provider=Answers()) as state:
        await state.submit(event("@机器人\u2005解释这个链接 " + SHORT))
        assert len(state.sends) == 1
        assert state.media.snapshot()["jobs"] == [] and state.media_calls == []
        # Even a model answer with another @ and share URL cannot recurse.
        await state.submit(event("@机器人\u2005" + SHORT, msgid=99, sender=BOT))
        assert len(state.sends) == 1 and state.media_calls == []


async def test_group_controls_and_blacklists_apply_to_chat_independently(tmp_path):
    async with context(tmp_path, provider=Answers()) as state:
        group = f"/api/manage/groups/{GROUP}"
        await state.client.patch(group, json={"chat_enabled": False})
        await state.submit(event(msgid=1))
        assert not state.sends
        await state.client.patch(group, json={"chat_enabled": True, "douyin_enabled": False})
        await state.submit(event(msgid=2))
        assert len(state.sends) == 1
        await state.client.patch(group, json={"enabled": False})
        await state.submit(event(msgid=3))
        await state.client.patch(group, json={"enabled": True, "blocked": True})
        await state.submit(event(msgid=4))
        await state.client.patch(group, json={"blocked": False})
        await state.client.patch(group + f"/members/{MEMBER}", json={"blocked": True})
        await state.submit(event(msgid=5))
        assert len(state.sends) == 1


async def test_policy_is_checked_again_after_model_finishes(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Paused:
        async def answer(self, question):
            entered.set()
            await release.wait()
            return "不应发送"

    async with context(tmp_path, provider=Paused()) as state:
        await state.client.post("/receiveChatBotMsg", json=event())
        await asyncio.wait_for(entered.wait(), 1)
        await state.client.patch(f"/api/manage/groups/{GROUP}", json={"chat_enabled": False})
        release.set()
        await asyncio.wait_for(state.bot._queue.join(), 1)
        assert not state.sends


@pytest.mark.parametrize("failure", [ValueError("secret provider response"), TimeoutError("secret")])
async def test_provider_failure_only_gets_one_fallback(tmp_path, failure):
    async with context(tmp_path, provider=Answers(error=failure)) as state:
        await state.submit()
        assert len(state.sends) == 1 and "暂时无法回答" in state.sends[0]["msg"]
        assert "secret" not in json.dumps(state.bot.snapshot())


async def test_send_timeout_does_not_retry_or_send_another_failure_message(tmp_path):
    async def timeout(request):
        raise httpx.ReadTimeout("possibly delivered")

    async with context(tmp_path, provider=Answers(), hook=timeout) as state:
        await state.submit()
        await state.submit()
        assert len(state.sends) == 1
        assert "不会自动重发" in state.bot.last_error


async def test_restart_and_concurrent_workers_share_durable_claims(tmp_path):
    provider = Answers()
    async with context(tmp_path, provider=provider) as state:
        second = MentionChatService(ChatbotOptions(True, BOT, ("机器人",)), provider=provider)
        second.groups = state.app.state.groups
        second.default_group_enabled = state.media.allowed
        await second.start(state.hook)
        try:
            payload = CallbackEvent.model_validate(event())
            await asyncio.gather(state.bot.receive(payload), second.receive(payload))
            await asyncio.wait_for(asyncio.gather(state.bot._queue.join(), second._queue.join()), 2)
            assert len(state.sends) == 1 and len(provider.questions) == 1
        finally:
            await second.stop()
    async with context(tmp_path, provider=provider) as state:
        await state.submit()
        assert state.sends == [] and len(provider.questions) == 1


async def test_zero_ids_are_independent_new_messages_and_queue_work_does_not_block_ack(tmp_path):
    async with context(tmp_path, provider=Answers()) as state:
        await state.submit(event(msgid=0))
        await state.submit(event(msgid=0))
        assert len(state.sends) == 2


async def test_empty_at_only_message_gets_one_instruction_without_model_call(tmp_path):
    provider = Answers()
    async with context(tmp_path, provider=provider) as state:
        await state.submit(event("@机器人\u2005"))
        assert len(state.sends) == 1 and provider.questions == []


def test_local_configuration_and_environment_switch(tmp_path, monkeypatch):
    path = tmp_path / "chatbot.local.yml"
    path.write_text("enabled: true\nbot-wxid: wxid_robot\nmention-names: ['群内机器人']\nmax-reply-chars: 1000\n", encoding="utf-8")
    monkeypatch.setenv("WXHOOK_CHATBOT_CONFIG", str(path))
    monkeypatch.delenv("WXHOOK_CHATBOT_ENABLED")
    assert load_chatbot_options() == ChatbotOptions(True, BOT, ("群内机器人",), 1000)
    monkeypatch.setenv("WXHOOK_CHATBOT_ENABLED", "false")
    assert not load_chatbot_options().enabled


async def test_empty_hook_nickname_can_be_fixed_from_web_without_restart(tmp_path):
    provider = Answers()
    profile = {"ret": 0, "data": {"wxid": BOT, "nickname": "", "selfInfoMatched": False}}
    async with context(tmp_path, provider=provider, bot_options=ChatbotOptions(), profile=profile) as state:
        await state.submit(event("@群内助手\u2005你是谁", roomid=""))
        assert not state.sends and not provider.questions
        report = state.bot.snapshot()["recent_events"][-1]
        assert report["status"] == "not_addressed" and "昵称为空" in report["message"]
        response = await state.client.post("/api/manage/chatbot/mentions", json={"mention_names": ["群内助手"]})
        assert response.status_code == 200 and response.json()["saved"]
        assert state.bot.snapshot()["configured_mention_names"] == ["群内助手"]
        assert load_chatbot_options().mention_names == ("群内助手",)
        await state.submit(event("@群内助手\u2005你是谁", msgid=2, roomid=""))
        assert provider.questions == ["你是谁"] and len(state.sends) == 1
        report = state.bot.snapshot()["recent_events"][-1]
        assert report["status"] == "sent" and report["group_id"] == GROUP
        assert "你是谁" not in json.dumps(report, ensure_ascii=False)


async def test_empty_nickname_uses_synced_name_of_the_bot_member(tmp_path):
    provider = Answers()
    async with context(tmp_path, provider=provider, bot_options=ChatbotOptions(), profile={"wxid": BOT, "nickname": ""}) as state:
        await state.app.state.groups.sync_contacts([{"id": GROUP, "name": "测试群"}],
            {GROUP: [{"id": BOT, "name": "群内助手"}, {"id": MEMBER, "name": "其他人"}]})
        await state.submit(event(MEMBER + ":\n@群内助手\u2005你好", roomid=""))
        assert provider.questions == ["你好"] and len(state.sends) == 1
        assert state.bot.snapshot()["recent_events"][-1]["mention_names"] == ["群内助手"]
        await state.submit(event("@其他人\u2005你好", msgid=2, roomid=""))
        assert len(state.sends) == 1


async def test_saved_display_name_never_overrides_explicit_other_mention_id(tmp_path):
    async with context(tmp_path, provider=Answers()) as state:
        await state.client.post("/api/manage/chatbot/mentions", json={"mention_names": ["群内助手"]})
        await state.submit(event("@群内助手\u2005你好", atlist=["wxid_other"]))
        assert not state.sends
        assert state.bot.snapshot()["recent_events"][-1]["status"] == "not_addressed"


async def test_mention_name_save_failure_keeps_existing_runtime_names(tmp_path, monkeypatch):
    async def fail(*args):
        raise PermissionError("do not expose storage details")

    async with context(tmp_path, provider=Answers()) as state:
        monkeypatch.setattr("wxhook_api.services.chatbot.save_mention_names", fail)
        response = await state.client.post("/api/manage/chatbot/mentions", json={"mention_names": ["新名字"]})
        assert response.status_code == 500 and "storage details" not in response.text
        assert state.bot.snapshot()["configured_mention_names"] == ["机器人"]
        await state.submit()
        assert len(state.sends) == 1


@pytest.mark.parametrize("names", [[""], ["x" * 81], ["机器人"] * 21])
async def test_invalid_mention_names_get_actionable_validation(tmp_path, names):
    async with context(tmp_path) as state:
        response = await state.client.post("/api/manage/chatbot/mentions", json={"mention_names": names})
        assert response.status_code == 422 and "显示名" in response.text


async def test_diagnostics_explain_policy_and_self_message_skips(tmp_path):
    async with context(tmp_path, provider=Answers()) as state:
        await state.client.patch(f"/api/manage/groups/{GROUP}", json={"chat_enabled": False})
        await state.submit()
        assert state.bot.snapshot()["recent_events"][-1]["status"] == "policy"
        await state.submit(event(sender=BOT, msgid=2))
        assert state.bot.snapshot()["recent_events"][-1]["status"] == "self"
