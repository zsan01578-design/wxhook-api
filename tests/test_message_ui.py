import asyncio

import httpx
from asgi_lifespan import LifespanManager

from tests.test_callbacks import EVENT
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.inbox import MessageInbox


async def test_page_assets_and_inbox_do_not_call_hook():
    calls = []
    app = create_app(hook_transport=httpx.MockTransport(lambda request: calls.append(request)))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/")
        script = await client.get("/ui/assets/app.js")
        style = await client.get("/ui/assets/style.css")
        inbox = await client.get("/ui/messages")
    assert page.status_code == script.status_code == style.status_code == inbox.status_code == 200
    assert "微信消息" in page.text
    assert inbox.json()["messages"] == []
    assert inbox.headers["cache-control"] == "no-store"
    assert calls == []


async def test_callback_is_visible_and_app_instances_do_not_share_messages():
    app = create_app()
    processed = asyncio.Event()

    async def observe(event):
        processed.set()

    app.state.event_dispatcher.register(observe)
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.post("/receiveChatBotMsg", json=EVENT)
        await asyncio.wait_for(processed.wait(), timeout=1)
        response = await client.get("/ui/messages")
    assert accepted.status_code == 200
    assert response.json()["total"] == 1
    message = response.json()["messages"][0]
    assert message["msgid"] == "9007199254740993"
    assert message["content"] == "hello"
    assert message["sender"] == EVENT["sender"]
    assert create_app().state.message_inbox.snapshot()["messages"] == []


async def test_inbox_is_bounded_keeps_newest_and_marks_truncated_content():
    inbox = MessageInbox(capacity=2, content_limit=5)
    for index in range(3):
        await inbox.receive(CallbackEvent.model_validate({**EVENT, "msgid": index, "content": "long message"}))
    result = inbox.snapshot()
    assert result["total"] == 3
    assert [message["msgid"] for message in result["messages"]] == ["2", "1"]
    assert result["messages"][0]["content"] == "long "
    assert result["messages"][0]["truncated"] is True
