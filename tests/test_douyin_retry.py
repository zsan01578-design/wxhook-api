import asyncio

import httpx

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, SHORT, options
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services import douyin as douyin_module
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.douyin_parser import DouyinError, ParsedMedia


MP4 = b"\0\0\0\x18ftypisom" + b"\0" * 40
URL = "https://v3.douyinvod.com/retry.mp4"


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


class FailingParser:
    async def parse(self, source):
        raise DouyinError("primary parser failed")


class FakeFallback:
    def __init__(self, url):
        self.url = url
        self.calls = 0

    async def parse(self, source):
        self.calls += 1
        return ParsedMedia("id", "兜底标题", "作者", "video", [{"kind": "video", "urls": [self.url]}], "douyin")


class TogglingParser:
    def __init__(self):
        self.calls = 0

    async def parse(self, source):
        self.calls += 1
        if self.calls == 1:
            raise DouyinError("primary parser failed")
        return ParsedMedia("id", "主解析标题", "作者", "video", [{"kind": "video", "urls": [URL]}], "douyin")


async def test_retry_restores_primary_provider_after_a_fallback_attempt(tmp_path, monkeypatch):
    primary = TogglingParser()
    fallback = FakeFallback("https://v3.douyinvod.com/fallback.mp4")
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: primary)
    monkeypatch.setattr(douyin_module, "load_dudunas_parser", lambda **kwargs: fallback)
    service = DouyinService(options(tmp_path, parser_provider="parse_video"))
    job = {"source_url": SHORT, "platform": "douyin"}
    config = service.options.settings
    await service._resolve_media(job, config)
    assert job["parser_provider"] == "dudunas" and job["parser_fallback"] is True
    await service._resolve_media(job, config)
    assert job["parser_provider"] == "parse_video" and job["parser_fallback"] is False


async def test_dudunas_fallback_is_used_when_primary_parser_fails(tmp_path, monkeypatch):
    fallback = FakeFallback("https://v3.douyinvod.com/fallback.mp4")
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: FailingParser())
    monkeypatch.setattr(douyin_module, "load_dudunas_parser", lambda **kwargs: fallback)

    def respond(request):
        if request.url.host == "v3.douyinvod.com":
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
        assert job["parser_provider"] == "dudunas" and job["parser_fallback"] is True
        assert fallback.calls == 1
        assert len(job["files"]) == 1
    finally:
        await service.stop()
