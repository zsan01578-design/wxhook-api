import asyncio
import os
from pathlib import Path

import httpx
from asgi_lifespan import LifespanManager
import pytest

from tests.test_parse_video import XHS, XHS_VIDEO, sdk_response
from wxhook_api.main import create_app
from wxhook_api.routes import parse as parse_route
from wxhook_api.services.douyin_parser import ParsedMedia
from wxhook_api.services.parse_video import ParseVideoParser


def mocked_parser(respond):
    return ParseVideoParser(transport=httpx.MockTransport(respond))


async def request_parse(client, url):
    return await client.post("/api/parse", json={"url": url})


async def test_api_returns_direct_media_links_without_cookie():
    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    app.state.parse_video_parser = mocked_parser(sdk_response)
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await request_parse(client, XHS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["platform"] == "xiaohongshu"
    assert data["kind"] == "video"
    assert data["assets"] == [{"kind": "video", "urls": [XHS_VIDEO]}]
    assert "cookie" not in response.text.lower()


async def test_api_rejects_unsupported_link_without_network():
    def no_network(request):
        pytest.fail("unsupported links must be rejected before any request")

    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    app.state.parse_video_parser = mocked_parser(no_network)
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await request_parse(client, "https://example.com/not-a-share-link")
    assert response.status_code == 400
    assert "不受支持" in response.json()["error"]["message"]


async def test_api_error_does_not_echo_configured_cookie():
    Path(os.environ["WXHOOK_PARSE_VIDEO_CONFIG"]).write_text("xiaohongshu-cookie: 'a1=private-test-value'", encoding="utf-8")

    def login_redirect(request):
        return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/login"})

    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    app.state.parse_video_parser = mocked_parser(login_redirect)
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await request_parse(client, XHS)
    assert response.status_code == 400
    assert "parse-video.local.yml" in response.json()["error"]["message"]
    assert "private-test-value" not in response.text


async def test_api_rate_limits_burst_requests(monkeypatch):
    class InstantParser:
        async def parse(self, url):
            return ParsedMedia("", "标题", "作者", "video", [{"kind": "video", "urls": ["https://v.example/1.mp4"]}])

    monkeypatch.setattr(parse_route, "_RATE_LIMIT", 2)
    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    app.state.parse_video_parser = InstantParser()
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await request_parse(client, XHS)
        second = await request_parse(client, XHS)
        third = await request_parse(client, XHS)
    assert first.status_code == 200 and second.status_code == 200
    assert third.status_code == 429
    assert "频繁" in third.json()["error"]["message"]


async def test_api_limits_concurrent_parses(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class BlockingParser:
        async def parse(self, url):
            started.set()
            await release.wait()
            return ParsedMedia("", "标题", "作者", "video", [{"kind": "video", "urls": ["https://v.example/1.mp4"]}])

    monkeypatch.setattr(parse_route, "_QUEUE_TIMEOUT", 0.05)
    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    app.state.parse_video_parser = BlockingParser()
    app.state.parse_slots = asyncio.Semaphore(1)
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(request_parse(client, XHS))
        await started.wait()
        second = await request_parse(client, XHS)
        release.set()
        assert (await first).status_code == 200
    assert second.status_code == 429
    assert "排队" in second.json()["error"]["message"]
