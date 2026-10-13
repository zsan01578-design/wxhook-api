import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from wxhook_api.main import create_app


@pytest.mark.asyncio
async def test_all_documented_routes_forward_to_hook_without_rewriting_response():
    calls = []

    async def hook_handler(request: httpx.Request):
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/QueryDB/GetAllDBName":
            return httpx.Response(200, json=[{"dbName": "MicroMsg.db"}])
        return httpx.Response(200, json={"ret": 0, "retmsg": "ok", "proiv": "x"})

    app = create_app(hook_transport=httpx.MockTransport(hook_handler))
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        requests = [
            ("GET", "/GetFileSavePath", None),
            ("POST", "/GetDecodeKey", {}),
            ("POST", "/GetMessagePic", {"msgid": "9007199254740993"}),
            ("POST", "/SendVoiceMsg", {"wxid": "wxid", "path": "a.silk"}),
            ("POST", "/SendTextMsg", {"wxidorgid": "room@chatroom", "msg": "hello", "atlist": ["wxid_a"]}),
            ("POST", "/SendImgMsg", {"wxidorgid": "wxid", "path": "a.jpg"}),
            ("POST", "/Decode_Pic", {"src_path": "a.dat", "dst_path": "a.jpg"}),
            ("POST", "/send_file_msg", {"wxid": "wxid", "filepath": "a.zip"}),
            ("POST", "/set_callback", {"url": "http://127.0.0.1:8000/receiveChatBotMsg"}),
            ("POST", "/GetSelfProfile", {}),
            ("POST", "/QueryDB/execute", {"optDbName": "MicroMsg.db", "SQL": "SELECT 1"}),
            ("POST", "/QueryDB/GetAllDBName", {}),
            ("GET", "/QueryDB/status", None),
            ("POST", "/Contact/search", {"keywords": ["alice"]}),
            ("POST", "/Message/history", {"wxid": "wxid", "size": 1, "page": 1}),
        ]
        responses = []
        for method, path, body in requests:
            response = await client.request(method, path, json=body)
            responses.append(response)

    assert all(response.status_code == 200 for response in responses)
    assert responses[11].json() == [{"dbName": "MicroMsg.db"}]
    assert len(calls) == 15
    assert calls[7][2] == b'{"wxid":"wxid","filepath":"a.zip","stage":"send"}'


@pytest.mark.asyncio
async def test_invalid_json_uses_documented_error_shape():
    async def hook_handler(request: httpx.Request):
        raise AssertionError("invalid JSON must not reach Hook")

    app = create_app(hook_transport=httpx.MockTransport(hook_handler))
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/SendTextMsg", content=b"not json", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json() == {"ret": -1, "msg": "invalid json"}


@pytest.mark.asyncio
async def test_hook_timeout_becomes_gateway_timeout():
    async def hook_handler(request: httpx.Request):
        raise httpx.ReadTimeout("slow")

    app = create_app(hook_transport=httpx.MockTransport(hook_handler))
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/QueryDB/status")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "hook_timeout"


@pytest.mark.asyncio
async def test_startup_does_not_call_hook():
    calls = []

    async def hook_handler(request: httpx.Request):
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    app = create_app(hook_transport=httpx.MockTransport(hook_handler))
    async with LifespanManager(app):
        pass

    assert calls == []
