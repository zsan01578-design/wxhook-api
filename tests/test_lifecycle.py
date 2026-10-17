import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from wxhook_api.main import create_app


@pytest.mark.asyncio
async def test_health_reports_local_service_without_calling_hook():
    calls = []

    async def hook_handler(request: httpx.Request):
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    app = create_app(hook_transport=httpx.MockTransport(hook_handler))
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "hook_base_url": "http://127.0.0.1:30001", "hook_port": 30001, "runtime_revision": app.state.runtime_revision}
    assert len(app.state.runtime_revision) == 64
    assert calls == []


@pytest.mark.asyncio
async def test_body_limit_rejects_large_content_length():
    app = create_app()
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/receiveChatBotMsg",
            content=b"x",
            headers={"content-type": "application/json", "content-length": "99999999"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"
