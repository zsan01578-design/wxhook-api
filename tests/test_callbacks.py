import asyncio

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from wxhook_api.main import create_app
from wxhook_api.services.events import EventDispatcher


EVENT = {
    "event_type": 1001,
    "msgid": 9007199254740993,
    "type": 1,
    "timestamp": 1710000000,
    "wxid": "wxid_xxx",
    "sender": "wxid_xxx",
    "roomid": "",
    "content": "hello",
    "source": "test",
}


@pytest.mark.asyncio
async def test_callback_is_accepted_and_preserves_large_id_and_extensions():
    received = asyncio.Event()
    events = []

    async def handler(event):
        events.append(event)
        received.set()

    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    app.state.event_dispatcher.register(handler)
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/receiveChatBotMsg", json=EVENT)
        await asyncio.wait_for(received.wait(), timeout=1)

    assert response.status_code == 200
    assert response.json() == {"ret": 0, "retmsg": "accepted"}
    assert events[0].msgid == 9007199254740993
    assert events[0].extra == {"source": "test"}


@pytest.mark.asyncio
async def test_callback_queue_full_returns_503_without_dropping_event():
    dispatcher = EventDispatcher(maxsize=1)
    blocker = asyncio.Event()
    started = asyncio.Event()

    async def handler(event):
        started.set()
        await blocker.wait()

    dispatcher.register(handler)
    await dispatcher.start()
    first = await dispatcher.enqueue(EVENT)
    await asyncio.wait_for(started.wait(), timeout=1)
    second = await dispatcher.enqueue(EVENT)
    third = await dispatcher.enqueue(EVENT)
    blocker.set()
    await dispatcher.stop(timeout=1)

    assert first is True
    assert second is True
    assert third is False


@pytest.mark.asyncio
async def test_callback_rejects_msg_without_hitting_hook():
    calls = []

    async def hook_handler(request: httpx.Request):
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    app = create_app(hook_transport=httpx.MockTransport(hook_handler))
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/receiveChatBotMsg", json={"msgid": 0})

    assert response.status_code == 422
    assert calls == []


@pytest.mark.asyncio
async def test_callback_url_is_restricted_before_forwarding():
    calls = []

    async def hook_handler(request: httpx.Request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ret": 0})

    app = create_app(hook_transport=httpx.MockTransport(hook_handler))
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/set_callback", json={"url": "http://evil.example/callback"})

    assert response.status_code == 422
    assert calls == []
