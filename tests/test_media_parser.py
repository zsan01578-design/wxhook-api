import json

import httpx
import pytest

from wxhook_api.services.douyin_parser import DouyinError
from wxhook_api.services.media_parser import MediaParserClient


BASE = "http://127.0.0.1:8051"
SHORT = "https://v.douyin.com/mediaparser/"
XHS = "https://www.xiaohongshu.com/explore/abcdef1234567890"
VIDEO = "https://v3.douyinvod.com/media.mp4"
IMAGE = "https://p3.douyinpic.com/media.png"
XHS_IMAGE = "https://sns-img.xhscdn.com/media.png"


def success(**data):
    return httpx.Response(200, json={"retcode": 200, "retdesc": "成功", "succ": True, "data": data})


def client(handler, **kwargs):
    return MediaParserClient(BASE, transport=httpx.MockTransport(handler), **kwargs)


async def test_video_payload_maps_to_parsed_media():
    media = await client(lambda request: success(
        video_id="123456", title="标题", desc="完整文案", video_url=VIDEO,
        platform="抖音", author={"nickname": "作者"},
    )).parse(SHORT)
    assert media.kind == "video"
    assert media.item_id == "123456"
    assert media.title == "标题"
    assert media.author == "作者"
    assert media.platform == "douyin"
    assert media.assets == [{"kind": "video", "urls": [VIDEO]}]


async def test_request_uses_post_json_to_v1_parse():
    calls = []

    def respond(request):
        calls.append(request)
        return success(video_url=VIDEO)

    await client(respond).parse(SHORT)
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{BASE}/api/v1/parse"
    assert json.loads(calls[0].content) == {"url": SHORT}


async def test_image_payload_ignores_live_photo_video_and_uses_source_platform():
    media = await client(lambda request: success(
        title="图文", platform="抖音",
        author={"nickname": "作者"},
        image_list=[IMAGE, {"url": XHS_IMAGE, "live_photo_url": "https://sns-video.xhscdn.com/live.mp4"}],
    )).parse(XHS)
    assert media.kind == "images"
    assert media.platform == "xiaohongshu"
    assert [asset["urls"] for asset in media.assets] == [[IMAGE], [XHS_IMAGE]]


async def test_title_falls_back_to_desc_then_default():
    media = await client(lambda request: success(desc="只有文案", video_url=VIDEO)).parse(SHORT)
    assert media.title == "只有文案"
    media = await client(lambda request: success(video_url=VIDEO)).parse(SHORT)
    assert media.title == "未命名作品"


@pytest.mark.parametrize("payload, expected", [
    ({"retcode": 400, "retdesc": "需要登录", "succ": False,
      "error_code": "XIAOHONGSHU_COOKIE_REQUIRED", "data": None}, "parse-video.local.yml"),
    ({"retcode": 400, "retdesc": "提取媒体内容失败", "succ": False,
      "error_code": "MEDIA_NOT_FOUND", "data": None}, "MEDIA_NOT_FOUND"),
])
async def test_business_errors_are_reported_without_http_status(payload, expected):
    with pytest.raises(DouyinError) as error:
        await client(lambda request: httpx.Response(400, json=payload)).parse(XHS)
    assert expected in str(error.value)


async def test_non_json_error_uses_http_status():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: httpx.Response(500, text="<html>oops</html>")).parse(SHORT)
    assert "HTTP 500" in str(error.value)


async def test_invalid_json_success_is_rejected():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: httpx.Response(200, text="not-json")).parse(SHORT)
    assert "不是有效 JSON" in str(error.value)


async def test_connection_failure_is_explained():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DouyinError) as error:
        await client(refuse).parse(SHORT)
    assert "本地服务连接失败" in str(error.value)


async def test_oversized_response_is_rejected():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: httpx.Response(200, content=b" " * 2048), max_bytes=1024).parse(SHORT)
    assert "响应过大" in str(error.value)


async def test_untrusted_media_url_is_rejected():
    with pytest.raises(DouyinError):
        await client(lambda request: success(image_list=["http://127.0.0.1/private.png"])).parse(SHORT)


async def test_more_than_100_images_is_rejected():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: success(image_list=[IMAGE] * 101)).parse(SHORT)
    assert "超过 100" in str(error.value)


async def test_missing_media_is_rejected():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: success(title="空作品")).parse(SHORT)
    assert "没有返回视频或图片地址" in str(error.value)
