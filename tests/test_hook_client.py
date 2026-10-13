import httpx
import pytest

from wxhook_api.clients.hook_client import HookClient, HookTimeoutError, HookTransportError
from wxhook_api.schemas.requests import FileStage, HistoryRequest, TextMessageRequest


@pytest.mark.asyncio
async def test_send_text_posts_original_payload_without_retries():
    seen = []

    async def handler(request: httpx.Request):
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"ret": 0, "retmsg": "success", "extra": True})

    async with HookClient("http://127.0.0.1:30001", transport=httpx.MockTransport(handler)) as client:
        response = await client.send_text(TextMessageRequest(wxidorgid="room@chatroom", msg="hello", atlist=["wxid_a"]))

    assert response.data == {"ret": 0, "retmsg": "success", "extra": True}
    assert response.status_code == 200
    assert seen == [("POST", "/SendTextMsg", b'{"wxidorgid":"room@chatroom","msg":"hello","atlist":["wxid_a"]}')]


@pytest.mark.asyncio
async def test_client_keeps_array_response():
    async def handler(request: httpx.Request):
        return httpx.Response(200, json=[{"dbName": "MicroMsg.db"}])

    async with HookClient("http://127.0.0.1:30001", transport=httpx.MockTransport(handler)) as client:
        response = await client.get_all_db_names()

    assert response.data == [{"dbName": "MicroMsg.db"}]
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_non_json_upstream_is_a_transport_error():
    async def handler(request: httpx.Request):
        return httpx.Response(200, text="not json")

    async with HookClient("http://127.0.0.1:30001", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HookTransportError, match="JSON"):
            await client.get_status()


@pytest.mark.asyncio
async def test_timeout_is_mapped_separately():
    async def handler(request: httpx.Request):
        raise httpx.ReadTimeout("slow")

    async with HookClient("http://127.0.0.1:30001", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HookTimeoutError):
            await client.get_status()


def test_request_models_apply_document_rules():
    assert TextMessageRequest(wxidorgid="wxid", msg="test", atlist="wxid_a").model_dump(exclude_none=True) == {
        "wxidorgid": "wxid",
        "msg": "test",
        "atlist": "wxid_a",
    }
    assert FileStage.send.value == "send"
    assert HistoryRequest(wxid="wxid", size=999, page=2).size == 500
    assert HistoryRequest(wxid="wxid", size=0).size == 500
