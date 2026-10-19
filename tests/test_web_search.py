import json
from datetime import date

import httpx
import pytest
from asgi_lifespan import LifespanManager

from wxhook_api.main import create_app
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.services.web_search import (
    DeepSeekSearchProvider,
    SearchError,
    SearchResult,
    SearchSettings,
    WebSearchConfiguration,
    format_search_context,
)


TODAY = date(2026, 10, 19)


class FakeSearchProvider:
    def __init__(self, results=None, error=None):
        self.results = results if results is not None else [SearchResult(title="标题", snippet="摘要")]
        self.error = error
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results


def search_config(tmp_path, provider, today=TODAY):
    return WebSearchConfiguration(tmp_path / "search.local.json", provider=provider, now=lambda: today)


@pytest.mark.asyncio
async def test_deepseek_provider_uses_bearer_and_limits_query_and_count():
    seen = {}

    async def handler(request):
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [{"title": "<b>标题</b>", "url": "https://example.com", "snippet": "A &amp; B"}]})

    provider = DeepSeekSearchProvider(
        "https://api.deepseek.com/v1",
        "sk-test",
        result_count=3,
        transport=httpx.MockTransport(handler),
    )
    results = await provider.search("长" * 200)

    assert seen["path"] == "/v1/tools/search"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["payload"]["count"] == 3
    assert len(seen["payload"]["query"]) == 128
    assert results[0].title == "标题"
    assert results[0].snippet == "A & B"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(401, json={}), httpx.Response(500, text="boom")])
async def test_deepseek_provider_maps_http_errors(response):
    provider = DeepSeekSearchProvider(
        "https://api.deepseek.com/v1",
        "sk-test",
        transport=httpx.MockTransport(lambda request: response),
    )
    with pytest.raises(SearchError):
        await provider.search("问题")


@pytest.mark.asyncio
async def test_deepseek_provider_maps_timeout_and_non_json():
    async def timeout(request):
        raise httpx.ReadTimeout("slow")

    provider = DeepSeekSearchProvider("https://api.deepseek.com/v1", "sk-test", transport=httpx.MockTransport(timeout))
    with pytest.raises(SearchError):
        await provider.search("问题")

    provider = DeepSeekSearchProvider(
        "https://api.deepseek.com/v1",
        "sk-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not json")),
    )
    with pytest.raises(SearchError):
        await provider.search("问题")


def test_search_context_has_titles_and_snippets_but_no_links():
    context = format_search_context(
        [
            SearchResult(title="标题一", snippet="摘要一", url="https://example.com/1"),
            SearchResult(title="标题二", snippet="摘要二", url="https://example.com/2"),
        ]
    )
    assert "标题一" in context and "摘要一" in context
    assert "https://" not in context


@pytest.mark.asyncio
async def test_daily_limit_counts_attempts(tmp_path):
    provider = FakeSearchProvider()
    config = search_config(tmp_path, provider)
    await config.save(SearchSettings(enabled=True, api_key="sk-test", daily_limit=2))

    assert await config.context("问题一")
    assert await config.context("问题二")
    assert await config.context("问题三") is None
    assert config.snapshot()["used_today"] == 2
    assert len(provider.queries) == 2


@pytest.mark.asyncio
async def test_failed_search_consumes_quota(tmp_path):
    provider = FakeSearchProvider(error=SearchError("failed"))
    config = search_config(tmp_path, provider)
    await config.save(SearchSettings(enabled=True, api_key="sk-test", daily_limit=1))

    assert await config.context("问题") is None
    assert await config.context("问题二") is None
    assert config.snapshot()["used_today"] == 1
    assert len(provider.queries) == 1


@pytest.mark.asyncio
async def test_usage_resets_on_a_new_day_and_persists(tmp_path):
    provider = FakeSearchProvider()
    config = search_config(tmp_path, provider)
    await config.save(SearchSettings(enabled=True, api_key="sk-test", daily_limit=2))
    assert await config.context("今天")

    restarted = search_config(tmp_path, provider, today=date(2026, 10, 20))
    assert restarted.snapshot()["used_today"] == 0
    assert await restarted.context("明天")
    assert restarted.snapshot()["used_today"] == 1


@pytest.mark.asyncio
async def test_snapshot_never_exposes_the_key_and_blank_key_is_reused(tmp_path):
    config = search_config(tmp_path, FakeSearchProvider())
    await config.save(SearchSettings(enabled=True, api_key="sk-secret", daily_limit=1000))
    await config.save(SearchSettings(enabled=True, base_url="https://api.deepseek.com/v1", daily_limit=900))

    snapshot = config.snapshot()
    assert "api_key" not in snapshot
    assert snapshot["has_api_key"] is True
    assert snapshot["daily_limit"] == 900


@pytest.mark.asyncio
async def test_chatbot_passes_search_context_to_the_model():
    class ContextProvider:
        def __init__(self):
            self.calls = []

        async def answer(self, question, context=None):
            self.calls.append((question, context))
            return "回答"

    class SearchService:
        async def context(self, question):
            return "搜索参考"

    provider = ContextProvider()
    bot = MentionChatService(ChatbotOptions(True, "wxid_bot", ("机器人",)), provider=provider)
    bot.search_service = SearchService()

    assert await bot._answer("问题") == "回答"
    assert provider.calls == [("问题", "搜索参考")]

    bot.search_service = None
    assert await bot._answer("问题") == "回答"
    assert provider.calls[-1] == ("问题", None)


@pytest.mark.asyncio
async def test_management_search_settings_route(monkeypatch, tmp_path):
    monkeypatch.setenv("WXHOOK_SEARCH_CONFIG", str(tmp_path / "search.local.json"))
    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/manage/chat-search")
        assert response.status_code == 200
        assert response.json()["daily_limit"] == 1000

        response = await client.post(
            "/api/manage/chat-search",
            json={"enabled": True, "base_url": "https://api.deepseek.com/v1", "api_key": "sk-test", "result_count": 5, "daily_limit": 1000},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["saved"] is True
        assert body["settings"]["has_api_key"] is True
        assert "api_key" not in body["settings"]
