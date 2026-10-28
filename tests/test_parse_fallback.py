import asyncio
from collections import deque
from types import SimpleNamespace

from fastapi import Response
import httpx
import pytest

from wxhook_api.routes import parse as parse_routes
from wxhook_api.schemas.requests import ParseLinkRequest
from wxhook_api.services.douyin_parser import DouyinError, ParsedMedia
from wxhook_api.services.parse_fallback import DudunasParser, load_dudunas_parser


VIDEO = "https://v3.douyinvod.com/fallback.mp4"
IMAGE = "https://p3.douyinpic.com/fallback.png"
SHORT = "https://v.douyin.com/fallback/"


def payload(**data):
    return {"code": 200, "status": 101, "data": data}


def transport_for(responses):
    calls = []

    def respond(request):
        calls.append(request)
        result = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(result, Exception):
            raise result
        return result

    return httpx.MockTransport(respond), calls


async def test_video_fallback_returns_parsed_media():
    transport, calls = transport_for([httpx.Response(200, json=payload(title="兜底视频", video_url=VIDEO))])
    media = await DudunasParser("secret", transport=transport, attempts=1, interval=0).parse(SHORT)
    assert media.kind == "video"
    assert media.title == "兜底视频"
    assert media.assets == [{"kind": "video", "urls": [VIDEO]}]
    assert calls[0].url.params["AppSecret"] == "secret"
    assert calls[0].url.params["text"] == SHORT


async def test_image_fallback_returns_ordered_gallery():
    transport, _ = transport_for([httpx.Response(200, json=payload(images=[IMAGE, {"url": "https://p3.douyinpic.com/two.png"}]))])
    media = await DudunasParser("secret", transport=transport, attempts=1, interval=0).parse(SHORT)
    assert media.kind == "images"
    assert [asset["urls"] for asset in media.assets] == [[IMAGE], ["https://p3.douyinpic.com/two.png"]]


async def test_fallback_works_for_xiaohongshu_without_a_douyin_item_id():
    source = "https://www.xiaohongshu.com/explore/abcdef123456"
    transport, _ = transport_for([httpx.Response(200, json=payload(images=[IMAGE]))])
    media = await DudunasParser("secret", transport=transport, attempts=1, interval=0).parse(source)
    assert media.platform == "xiaohongshu" and media.item_id == ""
    assert media.assets == [{"kind": "image", "urls": [IMAGE]}]


async def test_fallback_retries_transient_failure_then_succeeds():
    transport, calls = transport_for([
        httpx.Response(503),
        httpx.Response(200, json=payload(video_url=VIDEO)),
    ])
    media = await DudunasParser("secret", transport=transport, attempts=2, interval=0).parse(SHORT)
    assert media.kind == "video"
    assert len(calls) == 2


async def test_fallback_rejects_unsupported_media_urls_and_business_errors():
    transport, _ = transport_for([httpx.Response(200, json=payload(images=["http://127.0.0.1/private"]))])
    with pytest.raises(DouyinError):
        await DudunasParser("secret", transport=transport, attempts=1, interval=0).parse(SHORT)
    transport, _ = transport_for([httpx.Response(200, json={"code": 108, "msg": "会员接口不存在"})])
    with pytest.raises(DouyinError):
        await DudunasParser("secret", transport=transport, attempts=1, interval=0).parse(SHORT)


async def test_fallback_business_error_does_not_echo_third_party_message():
    secret = "top-secret"
    message = f"AppSecret={secret} https://example.test/?token=private"
    transport, _ = transport_for([httpx.Response(200, json={"code": 108, "msg": message})])
    with pytest.raises(DouyinError) as error:
        await DudunasParser(secret, transport=transport, attempts=1, interval=0).parse(SHORT)
    text = str(error.value)
    assert secret not in text and "token" not in text and "http" not in text


async def test_fallback_requires_a_configured_secret(tmp_path, monkeypatch):
    config = tmp_path / "parse-video.local.yml"
    monkeypatch.setenv("WXHOOK_PARSE_VIDEO_CONFIG", str(config))
    monkeypatch.delenv("WXHOOK_DUDUNAS_SECRET", raising=False)
    assert load_dudunas_parser() is None
    config.write_text("dudunas-secret: ''\n", encoding="utf-8")
    assert load_dudunas_parser() is None
    config.write_text("dudunas-secret: local-secret\n", encoding="utf-8")
    assert load_dudunas_parser().secret == "local-secret"
    monkeypatch.setenv("WXHOOK_DUDUNAS_SECRET", "env-secret")
    assert load_dudunas_parser().secret == "env-secret"


class FailingPrimary:
    transport = None

    async def parse(self, source):
        raise DouyinError("primary failed")


class FakeFallback:
    async def parse(self, source):
        return ParsedMedia("id", "兜底标题", "作者", "video", [{"kind": "video", "urls": [VIDEO]}], "douyin")


async def test_api_parse_uses_fallback_after_primary_failure(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace(
        parse_video_parser=FailingPrimary(), parse_slots=asyncio.Semaphore(2), parse_rate=deque()))
    request = SimpleNamespace(app=app)
    monkeypatch.setattr(parse_routes, "load_dudunas_parser", lambda **kwargs: FakeFallback())
    result = await parse_routes.parse_link(ParseLinkRequest(url=SHORT), request, Response())
    assert result["kind"] == "video"
    assert result["assets"] == [{"kind": "video", "urls": [VIDEO]}]
