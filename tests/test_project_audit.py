import json
import sqlite3

from asgi_lifespan import LifespanManager
import httpx
import pytest

from tests.test_douyin import ITEM, image_item
from wxhook_api.core.ai_safety import BudgetError, ModelBudget
from wxhook_api.core.config import load_settings
from wxhook_api.main import create_app
from wxhook_api.services.douyin_parser import DouyinError, parse_item
from wxhook_api.services.wechat_sync import SyncOptions


@pytest.mark.parametrize("url", ["http://[invalid/receiveChatBotMsg", "http://[::1/receiveChatBotMsg"])
async def test_malformed_callback_url_is_rejected_before_hook(url):
    calls = []
    app = create_app(hook_transport=httpx.MockTransport(lambda request: calls.append(request)))
    async with LifespanManager(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
    ) as client:
        response = await client.post("/set_callback", json={"url": url})
    assert response.status_code == 422 and not calls


@pytest.mark.parametrize("field,status", [("page", 422), ("size", 200)])
@pytest.mark.parametrize("value", ["1e999", "-1e999"])
async def test_overflowing_history_numbers_keep_validation_contract(field, status, value):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ret": 0})

    app = create_app(hook_transport=httpx.MockTransport(respond))
    async with LifespanManager(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
    ) as client:
        response = await client.post("/Message/history", content='{"wxid":"wxid_test","' + field + '":' + value + '}',
                                     headers={"Content-Type": "application/json"})
    assert response.status_code == status
    assert calls == ([{"wxid": "wxid_test", "size": 500, "page": 1}] if field == "size" else [])


@pytest.mark.parametrize("name", ["WXHOOK_CONNECT_TIMEOUT", "WXHOOK_READ_TIMEOUT", "WXHOOK_SHUTDOWN_TIMEOUT", "WXHOOK_SYNC_INTERVAL"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_nonfinite_timeouts_are_rejected_during_configuration(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        if name == "WXHOOK_SYNC_INTERVAL":
            SyncOptions.from_environment()
        else:
            load_settings()


@pytest.mark.parametrize("broken", [{}, None, {"url_list": ["https://example.invalid/missing.jpg"]}])
def test_builtin_parser_does_not_silently_drop_gallery_entries(broken):
    item = image_item(1)
    item["images"].append(broken)
    item["video"] = {"play_addr": {"url_list": ["https://v3.douyinvod.com/cover.mp4"]}}
    with pytest.raises(DouyinError, match="图集"):
        parse_item(item, ITEM)


@pytest.mark.parametrize("outcome", ["success", "limit", "storage"])
async def test_budget_connections_close_on_every_outcome(monkeypatch, outcome):
    budget = ModelBudget()
    budget.limits = (1, 100, 30)
    original = sqlite3.connect
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            return super().close()

        def execute(self, sql, *args):
            if outcome == "storage" and sql.startswith("INSERT INTO ai_usage"):
                raise sqlite3.OperationalError("synthetic usage write failure")
            return super().execute(sql, *args)

    def connect(*args, **kwargs):
        # Retain handles for observation and cleanup from the test's thread.
        kwargs["check_same_thread"] = False
        connection = original(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        if outcome == "storage":
            with pytest.raises(BudgetError):
                await budget.reserve()
        else:
            await budget.reserve()
            if outcome == "limit":
                with pytest.raises(BudgetError):
                    await budget.reserve()
        assert connections and all(connection.closed for connection in connections)
    finally:
        for connection in connections:
            connection.close()
