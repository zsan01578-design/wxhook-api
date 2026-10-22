import asyncio

from asgi_lifespan import LifespanManager
import httpx
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP
from wxhook_api.clients.hook_client import HookClient, HookTransportError
from wxhook_api.main import create_app


def unexpected_hook(request):
    raise AssertionError("Callback validation must not call Hook")


@pytest.mark.parametrize("value", [2**63, -(2**63) - 1])
async def test_callback_type_outside_sqlite_range_is_rejected_before_acceptance(value):
    app = create_app(hook_transport=httpx.MockTransport(unexpected_hook))
    async with LifespanManager(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/receiveChatBotMsg", json={
            **EVENT, "roomid": GROUP, "type": value,
        })
        await asyncio.wait_for(app.state.event_dispatcher._queue.join(), 2)
        assert response.status_code == 422
        status = (await client.get("/api/callback/status")).json()
        assert status["accepted"] == 0 and status["rejected"] == 1
        assert status["last_rejection"]["fields"][0]["field"] == "type"
        assert app.state.message_inbox.snapshot()["total"] == 0
        assert (await app.state.groups.messages(GROUP))["total"] == 0


@pytest.mark.parametrize("value", [-(2**63), 2**63 - 1, 1])
async def test_callback_integer_boundaries_preserve_large_message_ids(value):
    app = create_app(hook_transport=httpx.MockTransport(unexpected_hook))
    message_id = 2**80 + 1
    async with LifespanManager(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/receiveChatBotMsg", json={
            **EVENT, "roomid": GROUP, "type": value, "msgid": message_id,
        })
        await asyncio.wait_for(app.state.event_dispatcher._queue.join(), 2)
        assert response.status_code == 200
        messages = await app.state.groups.messages(GROUP)
        assert messages["total"] == 1
        assert messages["messages"][0]["type"] == value
        assert messages["messages"][0]["msgid"] == str(message_id)


INVALID_NUMBERS = ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"]


@pytest.mark.parametrize("number", INVALID_NUMBERS)
async def test_hook_client_rejects_nested_nonfinite_numbers(number):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text='{"ret":0,"extra":[{"value":' + number + '}]}',
    ))
    async with HookClient("http://hook.test", transport=transport) as client:
        with pytest.raises(HookTransportError, match="JSON"):
            await client.get_status()


@pytest.mark.parametrize("number", INVALID_NUMBERS)
async def test_nonfinite_hook_response_returns_gateway_error_without_retry(number):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, text='{"ret":0,"private":"secret-value","extra":' + number + '}')

    app = create_app(hook_transport=httpx.MockTransport(respond))
    async with LifespanManager(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
    ) as client:
        response = await client.get("/QueryDB/status")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "hook_transport_error"
    assert "secret-value" not in response.text
    assert calls == ["/QueryDB/status"]


async def test_finite_hook_numbers_and_literal_strings_are_preserved():
    payload = {"id": 2**80 + 1, "small": 1e-300, "large": 1e300,
               "values": [None, "NaN", "Infinity", "-Infinity", "1e999"]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with HookClient("http://hook.test", transport=transport) as client:
        assert (await client.get_status()).data == payload
