import asyncio
from dataclasses import replace

import httpx
import pytest
from asgi_lifespan import LifespanManager

from wxhook_api.core.config import load_settings
from wxhook_api.main import create_app
from wxhook_api.services.events import EventDispatcher
from tests.test_callbacks import EVENT


@pytest.mark.parametrize("status", [201, 302, 429, 503])
async def test_upstream_json_status_is_preserved(status):
    payload = {"ret": -8, "details": ["unchanged"]}
    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload)))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/QueryDB/status")
    assert response.status_code == status
    assert response.json() == payload


async def test_chunked_body_is_rejected_before_forwarding():
    calls = []
    settings = replace(load_settings(), max_body_bytes=100)
    app = create_app(settings, hook_transport=httpx.MockTransport(lambda request: calls.append(request)))

    async def chunks():
        yield b'{"wxidorgid":"wxid","msg":"'
        yield b'x' * 150
        yield b'"}'

    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/SendTextMsg", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert calls == []


async def test_full_queue_shutdown_cancels_worker_within_deadline():
    dispatcher = EventDispatcher(maxsize=1)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    blocker = asyncio.Event()

    async def handler(event):
        started.set()
        try:
            await blocker.wait()
        finally:
            cancelled.set()

    dispatcher.register(handler)
    await dispatcher.start()
    assert await dispatcher.enqueue(EVENT)
    await asyncio.wait_for(started.wait(), 1)
    assert await dispatcher.enqueue(EVENT)
    await asyncio.wait_for(dispatcher.stop(timeout=0.01), 1)
    assert cancelled.is_set()
    assert not await dispatcher.enqueue(EVENT)


async def test_handler_errors_never_log_message_content(caplog):
    dispatcher = EventDispatcher()
    called = asyncio.Event()

    async def handler(event):
        called.set()
        raise RuntimeError("secret-message-content")

    dispatcher.register(handler)
    await dispatcher.start()
    assert await dispatcher.enqueue(EVENT)
    await asyncio.wait_for(called.wait(), 1)
    await dispatcher.stop()
    assert "secret-message-content" not in caplog.text
    assert "callback handler failed" in caplog.text


@pytest.mark.parametrize("path", ["/SendTextMsg", "/GetSelfProfile"])
async def test_invalid_utf8_is_invalid_json(path):
    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, content=b'\xff', headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"ret": -1, "msg": "invalid json"}


@pytest.mark.parametrize("host, upstream", [("127.0.0.1", "http://localhost:8000"), ("0.0.0.0", "http://127.0.0.1:8000"), ("localhost", "http://127.0.0.1:8000")])
def test_self_proxy_aliases_are_rejected(monkeypatch, host, upstream):
    monkeypatch.setenv("WXHOOK_HOST", host)
    monkeypatch.setenv("WXHOOK_HOOK_BASE_URL", upstream)
    with pytest.raises(ValueError, match="same address"):
        load_settings()
