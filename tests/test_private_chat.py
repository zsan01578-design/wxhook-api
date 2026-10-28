import asyncio
from contextlib import asynccontextmanager
import json
import threading
from types import SimpleNamespace

from asgi_lifespan import LifespanManager
import httpx
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import options as douyin_options
from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.main import create_app
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.private_chat import SYSTEM_CONTACTS


BOT = "wxid_robot"
FRIEND = "wxid_friend"
OTHER = "wxid_other"
GROUP = "123456789@chatroom"


class Provider:
    def __init__(self, result="收到"):
        self.result = result
        self.calls = []

    async def answer(self, question, context=None, *, system=None, history=None, **kwargs):
        self.calls.append({"question": question, "system": system, "history": history})
        return self.result


def private_event(content="你好", msgid=1, **extra):
    return {**EVENT, "roomid": "", "wxid": FRIEND, "sender": FRIEND, "msgid": msgid, "type": 1, "content": content, **extra}


@asynccontextmanager
async def context(tmp_path, provider=None, *, bot_options=None, hook=None):
    state = SimpleNamespace(sends=[], hook_calls=[])

    async def hook_response(request):
        body = json.loads(request.content)
        state.hook_calls.append(request.url.path)
        if request.url.path == "/GetSelfProfile":
            return httpx.Response(200, json={"wxid": BOT, "nickname": "机器人"})
        assert request.url.path == "/SendTextMsg", "Unexpected outbound Hook operation"
        state.sends.append(body)
        return await hook(request) if hook else httpx.Response(200, json={"ret": 0})

    state.bot = MentionChatService(bot_options or ChatbotOptions(True, BOT, ("机器人",)), provider=provider)
    state.hook = HookClient("http://hook.test", transport=httpx.MockTransport(hook_response))
    state.media = DouyinService(douyin_options(tmp_path), transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    state.app = create_app(douyin_service=state.media, hook_client=state.hook, chat_service=state.bot,
                           group_manager=GroupManager(tmp_path / "management.sqlite3"))
    try:
        async with LifespanManager(state.app), httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app), base_url="http://test") as client:
            state.client = client

            async def submit(payload):
                response = await client.post("/receiveChatBotMsg", json=payload)
                assert response.status_code == 200
                await asyncio.wait_for(state.app.state.event_dispatcher._queue.join(), 2)
                await asyncio.wait_for(state.app.state.private_chat._queue.join(), 2)

            state.submit = submit
            yield state
    finally:
        await state.hook.aclose()


async def enable(state, contact=FRIEND, personality=""):
    await state.app.state.private_chat.configure(enabled=True, personality=personality)
    assert await state.app.state.private_chat.update_contact(contact, {"enabled": True})


async def test_private_chat_is_off_by_default_and_only_records_the_contact(tmp_path):
    provider = Provider()
    async with context(tmp_path, provider) as state:
        await state.submit(private_event())
        assert not provider.calls and not state.sends
        snapshot = state.app.state.private_chat.snapshot()
        assert snapshot["enabled"] is False
        assert [(item["id"], item["enabled"]) for item in snapshot["contacts"]] == [(FRIEND, False)]


async def test_enabled_contact_gets_one_reply_to_the_friend(tmp_path):
    provider = Provider("你好呀")
    async with context(tmp_path, provider) as state:
        await state.submit(private_event())
        await enable(state)
        await state.submit(private_event("在吗", 2))
        assert provider.calls[0]["question"] == "在吗"
        assert state.sends == [{"wxidorgid": FRIEND, "msg": "你好呀"}]


async def test_personality_and_context_reach_the_model(tmp_path):
    provider = Provider("第一句")
    async with context(tmp_path, provider) as state:
        await state.submit(private_event())
        await enable(state, personality="你说话温柔耐心")
        await state.submit(private_event("你好", 2))
        await state.submit(private_event("再说一句", 3))
        assert len(provider.calls) == 2
        assert "你说话温柔耐心" in provider.calls[0]["system"]
        assert provider.calls[0]["history"] == []
        assert provider.calls[1]["history"] == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "第一句"},
        ]


@pytest.mark.parametrize("payload", [
    private_event(msgid=10, roomid=GROUP, wxid=GROUP),
    private_event(msgid=11, sender=BOT, wxid=FRIEND),
    private_event(msgid=12, type=3),
])
async def test_group_self_and_nontext_messages_are_ignored(tmp_path, payload):
    provider = Provider()
    async with context(tmp_path, provider) as state:
        await state.submit(private_event())
        await enable(state)
        await state.submit(payload)
        assert not provider.calls and not state.sends


async def test_only_enabled_contacts_are_answered(tmp_path):
    provider = Provider()
    async with context(tmp_path, provider) as state:
        await state.submit(private_event())
        await state.submit(private_event(msgid=2, wxid=OTHER, sender=OTHER))
        await enable(state)
        await state.submit(private_event(msgid=3))
        await state.submit(private_event(msgid=4, wxid=OTHER, sender=OTHER))
        assert [call["question"] for call in provider.calls] == ["你好"]
        assert [body["wxidorgid"] for body in state.sends] == [FRIEND]


async def test_clear_context_command_resets_history(tmp_path):
    provider = Provider("好的")
    async with context(tmp_path, provider) as state:
        await state.submit(private_event())
        await enable(state)
        await state.submit(private_event("你好", 2))
        await state.submit(private_event("清空上下文", 3))
        await state.submit(private_event("重新开始", 4))
        assert [call["question"] for call in provider.calls] == ["你好", "重新开始"]
        assert provider.calls[1]["history"] == []
        assert [body["msg"] for body in state.sends] == ["好的", "私聊对话上下文已清空。", "好的"]


async def test_duplicate_callback_does_not_repeat_the_answer(tmp_path):
    provider = Provider()
    async with context(tmp_path, provider) as state:
        await state.submit(private_event())
        await enable(state)
        await state.submit(private_event("你好", 5))
        await state.submit(private_event("你好", 5))
        assert len(provider.calls) == 1
        assert len(state.sends) == 1


async def test_missing_model_gives_an_honest_configuration_message(tmp_path):
    async with context(tmp_path, None) as state:
        await state.submit(private_event())
        await enable(state)
        await state.submit(private_event("在吗", 2))
        assert state.sends == [{"wxidorgid": FRIEND, "msg": "私聊问答接口尚未配置，请联系管理员。"}]


async def test_config_api_updates_personality_and_contact_toggle(tmp_path):
    async with context(tmp_path, Provider()) as state:
        await state.submit(private_event())
        response = await state.client.post("/api/private/config", json={"enabled": True, "personality": "幽默风趣"})
        assert response.status_code == 200
        assert response.json()["state"]["enabled"] is True
        assert response.json()["state"]["personality"] == "幽默风趣"
        response = await state.client.patch(f"/api/private/contacts/{FRIEND}", json={"enabled": True, "name": "朋友"})
        assert response.status_code == 200
        contact = response.json()["state"]["contacts"][0]
        assert contact["enabled"] is True and contact["name"] == "朋友"
        assert contact["last_message"] == "等待新的私聊消息"
        response = await state.client.get("/api/private/state")
        assert response.status_code == 200 and response.json()["contacts"][0]["name"] == "朋友"
        page = await state.client.get("/private")
        assert page.status_code == 200 and "私聊 AI 问答" in page.text


async def test_invalid_personality_and_group_contact_are_rejected(tmp_path):
    async with context(tmp_path, Provider()) as state:
        response = await state.client.post("/api/private/config", json={"personality": "x" * 2001})
        assert response.status_code == 422
        response = await state.client.patch("/api/private/contacts/123456789@chatroom", json={"enabled": True})
        assert response.status_code == 400


@pytest.mark.parametrize("sender_fallback", [False, True])
async def test_service_accounts_are_not_registered_or_answered(tmp_path, sender_fallback):
    provider = Provider()
    accounts = sorted(SYSTEM_CONTACTS | {"gh_43f2581f6fd6", "gh_example", "NEWSAPP", "GH_EXAMPLE"})
    async with context(tmp_path, provider) as state:
        await state.app.state.private_chat.configure(enabled=True)
        for msgid, account in enumerate(accounts, 100):
            await state.submit(private_event(msgid=msgid, wxid=BOT if sender_fallback else account, sender=account))
        response = await state.client.get("/api/private/state")
        assert response.json()["contacts"] == []
        with state.app.state.private_chat._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM private_contacts").fetchone()[0] == 0
        assert not provider.calls and not state.sends


@pytest.mark.parametrize("contact", [FRIEND, "alice_1988", "didi1v6.qq.com", "newsapp_friend"])
async def test_personal_usernames_still_receive_replies(tmp_path, contact):
    async with context(tmp_path, Provider()) as state:
        await state.submit(private_event(wxid=contact, sender=contact))
        await enable(state, contact)
        await state.submit(private_event(msgid=2, wxid=contact, sender=contact))
        assert state.sends == [{"wxidorgid": contact, "msg": "收到"}]


async def test_preexisting_enabled_service_accounts_are_excluded_after_restart(tmp_path):
    blocked = ["newsapp", "gh_43f2581f6fd6", "fmessage", "weixin", "filehelper"]
    async with context(tmp_path, Provider()) as state:
        await state.submit(private_event())
        await enable(state, personality="温和耐心")
        await state.app.state.private_chat.update_contact(FRIEND, {"name": "朋友"})
        with state.app.state.private_chat._connect() as db:
            db.executemany(
                "INSERT INTO private_contacts (id, name, enabled, created_at) VALUES (?, '', 1, '2026-01-01')",
                [(account,) for account in blocked],
            )

    provider = Provider()
    async with context(tmp_path, provider) as state:
        response = await state.client.get("/api/private/state")
        snapshot = response.json()
        assert snapshot["enabled"] and snapshot["personality"] == "温和耐心"
        assert [(row["id"], row["name"], row["enabled"]) for row in snapshot["contacts"]] == [(FRIEND, "朋友", True)]
        for msgid, account in enumerate(blocked, 100):
            response = await state.client.patch(f"/api/private/contacts/{account}", json={"enabled": True})
            assert response.status_code == 404
            await state.submit(private_event(msgid=msgid, wxid=account, sender=account))
        assert not provider.calls and not state.sends
        await state.submit(private_event(msgid=200))
        assert state.sends == [{"wxidorgid": FRIEND, "msg": "收到"}]


async def test_stale_enabled_service_account_cannot_bypass_reply_checks(tmp_path):
    provider = Provider()
    async with context(tmp_path, provider) as state:
        service = state.app.state.private_chat
        await service.configure(enabled=True)
        service._contacts["newsapp"] = {"enabled": 1}
        assert not await service.update_contact("newsapp", {"enabled": True})
        assert service.snapshot()["contacts"] == []
        await service._process({"contact": "newsapp", "question": "你好"})
        assert not await service._send("newsapp", "你好")
        assert not provider.calls and not state.sends


@pytest.mark.parametrize("scope", ["global", "contact"])
async def test_cancelled_disable_still_updates_reply_policy(tmp_path, monkeypatch, scope):
    async with context(tmp_path, Provider()) as state:
        await state.submit(private_event())
        await enable(state)
        service = state.app.state.private_chat
        original = service._run_write
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def delayed_write(action):
            try:
                result = original(action)
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test write gate was not released")
                return result
            finally:
                finished.set()

        monkeypatch.setattr(service, "_run_write", delayed_write)
        operation = service.configure(enabled=False) if scope == "global" else service.update_contact(FRIEND, {"enabled": False})
        task = asyncio.create_task(operation)
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(finished.wait, 3)
            monkeypatch.setattr(service, "_run_write", original)

        await state.submit(private_event("停用后不应回答", 301))
        assert state.sends == []
        snapshot = (await state.client.get("/api/private/state")).json()
        assert not (snapshot["enabled"] if scope == "global" else snapshot["contacts"][0]["enabled"])


async def test_cold_start_ignores_self_message_after_identity_check(tmp_path):
    async with context(tmp_path) as state:
        await state.submit(private_event())
        await enable(state)
    provider = Provider()
    async with context(tmp_path, provider, bot_options=ChatbotOptions(True, "", ())) as state:
        assert state.bot.identity == ""
        await state.submit(private_event("机器人本人手动发送", 302, sender=BOT))
        assert provider.calls == [] and state.sends == []
        await state.submit(private_event("好友的新提问", 303))
        assert [call["question"] for call in provider.calls] == ["好友的新提问"]
        assert [body["wxidorgid"] for body in state.sends] == [FRIEND]


async def test_private_duplicate_is_not_answered_after_restart(tmp_path):
    payload = private_event("这条消息只能回复一次", 304)
    async with context(tmp_path, Provider()) as state:
        await state.submit(private_event())
        await enable(state)
        await state.submit(payload)
        assert len(state.sends) == 1
    provider = Provider()
    async with context(tmp_path, provider) as state:
        await state.submit(payload)
        assert provider.calls == [] and state.sends == []
        await state.submit(private_event("新消息仍可回复", 305))
        assert len(state.sends) == 1


async def test_uncertain_private_send_is_not_retried_after_restart(tmp_path):
    async def timeout(request):
        raise httpx.ReadTimeout("reply result unknown", request=request)

    payload = private_event("发送超时也不能重发", 306)
    async with context(tmp_path, Provider(), hook=timeout) as state:
        await state.submit(private_event())
        await enable(state)
        await state.submit(payload)
        assert len(state.sends) == 1
        assert state.app.state.private_chat.sent == 0
    provider = Provider()
    async with context(tmp_path, provider) as state:
        await state.submit(payload)
        assert provider.calls == [] and state.sends == []


async def test_private_duplicate_keys_are_separate_for_each_contact(tmp_path):
    async with context(tmp_path, Provider()) as state:
        for contact in (FRIEND, OTHER):
            await state.submit(private_event(wxid=contact, sender=contact))
            await enable(state, contact)
            await state.submit(private_event("相同编号不同联系人", 307, wxid=contact, sender=contact))
        assert [body["wxidorgid"] for body in state.sends] == [FRIEND, OTHER]
    async with context(tmp_path, Provider()) as state:
        for contact in (FRIEND, OTHER):
            await state.submit(private_event("相同编号不同联系人", 307, wxid=contact, sender=contact))
        assert state.sends == []


async def test_private_messages_without_ids_remain_independent(tmp_path):
    async with context(tmp_path, Provider()) as state:
        await state.submit(private_event())
        await enable(state)
        await state.submit(private_event("没有编号的第一条", 0))
        await state.submit(private_event("没有编号的第二条", 0))
        assert len(state.sends) == 2
