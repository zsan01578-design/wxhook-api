import asyncio

import httpx
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, SHORT, options
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services import douyin as douyin_module
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.douyin_parser import DouyinError, ParsedMedia


MP4 = b"\0\0\0\x18ftypisom" + b"\0" * 40
URL = "https://v3.douyinvod.com/retry.mp4"
IMAGE = "https://p3.douyinpic.com/retry.png"


class FakeParser:
    def __init__(self, urls):
        self.urls = urls
        self.calls = []

    async def parse(self, source):
        self.calls.append(source)
        url = self.urls[min(len(self.calls) - 1, len(self.urls) - 1)]
        return ParsedMedia("id", "标题", "作者", "video", [{"kind": "video", "urls": [url]}], "douyin")


async def test_download_failure_reparses_with_a_fresh_url(tmp_path, monkeypatch):
    parser = FakeParser(["https://v3.douyinvod.com/bad.mp4", "https://v3.douyinvod.com/good.mp4"])
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: parser)
    monkeypatch.setattr(douyin_module, "DOWNLOAD_RETRY_SECONDS", 0)
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if request.url.host == "v3.douyinvod.com":
            if "bad" in str(request.url):
                return httpx.Response(404)
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    service = DouyinService(options(tmp_path, parser_provider="parse_video", send_results=False),
                            transport=httpx.MockTransport(respond))
    await service.start()
    try:
        await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "content": SHORT}))
        await asyncio.wait_for(service._queue.join(), 5)
        job = service.snapshot()["jobs"][0]
        assert job["status"] == "completed", job
        assert len(parser.calls) == 2
        assert job["download_attempt"] == 1
        assert len(job["files"]) == 1
        assert any("good.mp4" in url for url in calls)
    finally:
        await service.stop()


MEDIA_BASE = "http://127.0.0.1:8051"
MEDIA_VIDEO = "https://v3.douyinvod.com/media-fallback.mp4"


def media_success(request):
    if request.url.host == "127.0.0.1" and request.url.path == "/api/v1/parse":
        return httpx.Response(200, json={
            "retcode": 200, "retdesc": "成功", "succ": True,
            "data": {"title": "兜底标题", "author": {"nickname": "作者"}, "video_url": MEDIA_VIDEO},
        })
    return httpx.Response(404)


class FailingParser:
    async def parse(self, source):
        raise DouyinError("primary parser failed")


class VideoParser:
    async def parse(self, source):
        return ParsedMedia("id", "主解析标题", "作者", "video",
                           [{"kind": "video", "urls": ["https://v3.douyinvod.com/primary.mp4"]}], "douyin")


async def test_primary_failure_uses_media_parser_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: FailingParser())
    service = DouyinService(
        options(tmp_path, parser_provider="parse_video", parser_api_url="", media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(media_success))
    job = {"source_url": SHORT, "platform": "douyin"}
    parsed = await service._resolve_media(job, service.options.settings)
    assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is True
    assert parsed.assets == [{"kind": "video", "urls": [MEDIA_VIDEO]}]


async def test_primary_success_never_calls_media_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: VideoParser())
    service = DouyinService(
        options(tmp_path, parser_provider="parse_video", media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(lambda request: pytest.fail("media-parser must not be called")))
    job = {"source_url": SHORT, "platform": "douyin"}
    parsed = await service._resolve_media(job, service.options.settings)
    assert job["parser_provider"] == "parse_video" and job["parser_fallback"] is False
    assert parsed.assets == [{"kind": "video", "urls": ["https://v3.douyinvod.com/primary.mp4"]}]


async def test_media_parser_primary_supports_xiaohongshu(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: pytest.fail("primary must not run"))
    service = DouyinService(
        options(tmp_path, parser_provider="media_parser", parser_api_url="", media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(media_success))
    job = {"source_url": "https://www.xiaohongshu.com/explore/abcdef123456", "platform": "xiaohongshu"}
    parsed = await service._resolve_media(job, service.options.settings)
    assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is False
    assert parsed.platform == "xiaohongshu"


async def test_media_parser_primary_failure_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: pytest.fail("reverse fallback is forbidden"))

    def broken(request):
        return httpx.Response(400, json={"retcode": 400, "retdesc": "失败", "succ": False,
                                         "error_code": "MEDIA_NOT_FOUND", "data": None})

    service = DouyinService(
        options(tmp_path, parser_provider="media_parser", media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(broken))
    job = {"source_url": SHORT, "platform": "douyin"}
    with pytest.raises(DouyinError) as error:
        await service._resolve_media(job, service.options.settings)
    assert "MEDIA_NOT_FOUND" in str(error.value)


async def test_api_provider_failure_uses_media_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "DouyinAPIParser", lambda *args, **kwargs: FailingParser())
    service = DouyinService(
        options(tmp_path, parser_provider="api", parser_api_url="https://jk.y-l.cc/api/dsp/demo/user/",
                media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(media_success))
    job = {"source_url": SHORT, "platform": "douyin"}
    await service._resolve_media(job, service.options.settings)
    assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is True


class RawHttp403Parser:
    async def parse(self, source):
        request = httpx.Request("GET", "https://www.douyin.com/video/123")
        raise httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))


async def test_builtin_raw_http_error_uses_media_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "DouyinParser", lambda *args, **kwargs: RawHttp403Parser())
    service = DouyinService(
        options(tmp_path, parser_provider="builtin", media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(media_success))
    job = {"source_url": SHORT, "platform": "douyin"}
    parsed = await service._resolve_media(job, service.options.settings)
    assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is True
    assert parsed.assets == [{"kind": "video", "urls": [MEDIA_VIDEO]}]


async def test_empty_media_url_keeps_primary_error(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: FailingParser())
    service = DouyinService(
        options(tmp_path, parser_provider="parse_video", media_parser_url=""),
        transport=httpx.MockTransport(lambda request: pytest.fail("fallback must stay disabled")))
    job = {"source_url": SHORT, "platform": "douyin"}
    with pytest.raises(DouyinError) as error:
        await service._resolve_media(job, service.options.settings)
    assert str(error.value) == "primary parser failed"


async def test_media_parser_fallback_downloads_and_reports_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: FailingParser())

    def respond(request):
        if request.url.host == "127.0.0.1":
            return media_success(request)
        if request.url.host == "v3.douyinvod.com":
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    service = DouyinService(
        options(tmp_path, parser_provider="parse_video", media_parser_url=MEDIA_BASE, send_results=False),
        transport=httpx.MockTransport(respond))
    await service.start()
    try:
        await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "content": SHORT}))
        await asyncio.wait_for(service._queue.join(), 5)
        job = service.snapshot()["jobs"][0]
        assert job["status"] == "completed", job
        assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is True
        assert len(job["files"]) == 1
    finally:
        await service.stop()
