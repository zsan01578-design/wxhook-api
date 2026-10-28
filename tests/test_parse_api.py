import asyncio
import os
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import httpx
from asgi_lifespan import LifespanManager
from fastapi import HTTPException, Response
import pytest

from tests.test_parse_video import XHS, XHS_VIDEO, sdk_response
from wxhook_api.core.douyin_config import GroupSettings
from wxhook_api.main import create_app
from wxhook_api.routes import parse as parse_route
from wxhook_api.schemas.requests import ParseLinkRequest
from wxhook_api.services.douyin_parser import DouyinError, ParsedMedia
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


MEDIA_BASE = "http://127.0.0.1:8051"
FALLBACK_VIDEO = "https://v3.douyinvod.com/fallback.mp4"
FALLBACK_SHORT = "https://v.douyin.com/fallback/"


def media_payload(request):
    if request.url.host == "127.0.0.1" and request.url.path == "/api/v1/parse":
        return httpx.Response(200, json={
            "retcode": 200, "retdesc": "成功", "succ": True,
            "data": {"title": "兜底标题", "author": {"nickname": "作者"}, "video_url": FALLBACK_VIDEO},
        })
    return httpx.Response(404)


class FailingPrimary:
    transport = httpx.MockTransport(media_payload)

    async def parse(self, source):
        raise DouyinError("primary failed")


class VideoPrimary:
    transport = httpx.MockTransport(lambda request: pytest.fail("fallback must not run after success"))

    async def parse(self, source):
        return ParsedMedia("id", "主解析", "作者", "video",
                           [{"kind": "video", "urls": ["https://v3.douyinvod.com/primary.mp4"]}], "douyin")


def parse_state(parser, media_url):
    settings = GroupSettings(media_parser_url=media_url)
    return SimpleNamespace(state=SimpleNamespace(
        parse_video_parser=parser,
        parse_slots=asyncio.Semaphore(2),
        parse_rate=deque(),
        douyin=SimpleNamespace(options=SimpleNamespace(settings=settings)),
    ))


async def test_api_parse_falls_back_to_media_parser():
    request = SimpleNamespace(app=parse_state(FailingPrimary(), MEDIA_BASE))
    result = await parse_route.parse_link(ParseLinkRequest(url=FALLBACK_SHORT), request, Response())
    assert result["kind"] == "video"
    assert result["title"] == "兜底标题"
    assert result["assets"] == [{"kind": "video", "urls": [FALLBACK_VIDEO]}]


async def test_api_parse_does_not_call_fallback_after_success():
    request = SimpleNamespace(app=parse_state(VideoPrimary(), MEDIA_BASE))
    result = await parse_route.parse_link(ParseLinkRequest(url=FALLBACK_SHORT), request, Response())
    assert result["title"] == "主解析"


async def test_api_parse_without_media_url_keeps_primary_error():
    request = SimpleNamespace(app=parse_state(FailingPrimary(), ""))
    with pytest.raises(HTTPException) as error:
        await parse_route.parse_link(ParseLinkRequest(url=FALLBACK_SHORT), request, Response())
    assert error.value.status_code == 400
    assert "primary failed" in error.value.detail


async def test_api_parse_combines_primary_and_fallback_errors():
    def fail_media(request):
        return httpx.Response(400, json={
            "retcode": 400, "retdesc": "提取失败", "succ": False,
            "error_code": "MEDIA_NOT_FOUND", "data": None,
        })

    class BrokenFallback:
        transport = httpx.MockTransport(fail_media)

        async def parse(self, source):
            raise DouyinError("primary failed")

    request = SimpleNamespace(app=parse_state(BrokenFallback(), MEDIA_BASE))
    with pytest.raises(HTTPException) as error:
        await parse_route.parse_link(ParseLinkRequest(url=FALLBACK_SHORT), request, Response())
    assert "primary failed" in error.value.detail
    assert "media-parser 兜底也失败" in error.value.detail
    assert "MEDIA_NOT_FOUND" in error.value.detail
