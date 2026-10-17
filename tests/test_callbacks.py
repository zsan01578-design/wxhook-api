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


@pytest.mark.parametrize("roomid", ["missing", None])
async def test_group_callback_without_roomid_can_use_conversation_wxid(roomid):
    observed = []
    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    payload = {**EVENT, "wxid": "123456789@chatroom"}
    if roomid == "missing":
        payload.pop("roomid")
    else:
        payload["roomid"] = roomid

    async def capture(event):
        observed.append(event)

    app.state.event_dispatcher.register(capture)
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/receiveChatBotMsg", json=payload)
        await asyncio.wait_for(app.state.event_dispatcher._queue.join(), 1)
        status = (await client.get("/api/callback/status")).json()
    assert response.status_code == 200 and observed[0].roomid == ""
    assert observed[0].wxid == "123456789@chatroom"
    assert status["accepted"] == 1 and status["rejected"] == 0


async def test_rejected_callback_reports_field_and_type_without_body_or_values(caplog):
    app = create_app()
    payload = {**EVENT, "timestamp": "private-invalid-value", "content": "private-message-text", "wxid": "private-account"}
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/receiveChatBotMsg", json=payload)
        status_response = await client.get("/api/callback/status")
        status = status_response.json()
        inbox = (await client.get("/ui/messages")).json()
    assert response.status_code == 422 and inbox["total"] == 0
    assert response.json()["error"]["fields"] == [{"field": "timestamp", "error": "int_parsing", "input_type": "string"}]
    assert status["accepted"] == 0 and status["rejected"] == 1
    assert status["last_rejection"]["shape"]["content"] == "string"
    assert status_response.headers["cache-control"] == "no-store"
    for text in [response.text, status_response.text, caplog.text]:
        assert all(value not in text for value in ["private-invalid-value", "private-message-text", "private-account"])
    assert "timestamp:int_parsing" in caplog.text


async def test_rejected_callback_invalid_json_keeps_400_and_diagnostic():
    app = create_app()
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/receiveChatBotMsg", content=b'{"content":', headers={"content-type": "application/json"})
        status = (await client.get("/api/callback/status")).json()
    assert response.status_code == 400 and response.json() == {"ret": -1, "msg": "invalid json"}
    assert status["rejected"] == 1 and status["last_rejection"]["http_status"] == 400
