"""Live Photo companions survive parsing, download, delivery and cache reuse."""

from pathlib import Path

import httpx
import pytest

from tests.test_douyin import PNG, SHORT, image_item, page
from tests.test_douyin_delivery import scenario
from tests.test_parse_video import MP4, VIDEO, XHS, XHS_VIDEO, sdk_response
from wxhook_api.services.douyin_parser import parse_item


def live_item():
    item = image_item(2)
    item["images"][0]["video"] = {"play_addr": {"url_list": [VIDEO]}}
    return item


def test_public_share_page_preserves_live_photo_next_to_still_image():
    media = parse_item(live_item(), "id")
    assert media.kind == "images"
    assert media.assets == [
        {"kind": "image", "urls": ["https://p3.douyinpic.com/0.png"]},
        {"kind": "video", "urls": [VIDEO]},
        {"kind": "image", "urls": ["https://p3.douyinpic.com/1.png"]},
    ]


@pytest.mark.parametrize("provider,source", [
    ("parse_video", SHORT), ("parse_video", XHS),
    ("media_parser", SHORT), ("media_parser", XHS), ("builtin", SHORT),
])
async def test_live_photo_download_return_and_cache(tmp_path, monkeypatch, provider, source):
    video_url = XHS_VIDEO if source == XHS else VIDEO
    downloads = []

    def respond(request):
        if request.url.path == "/api/v1/parse":
            return httpx.Response(200, json={"retcode": 200, "succ": True, "data": {
                "title": "实况图", "image_list": [
                    {"url": "https://p3.douyinpic.com/0.png", "live_photo_url": video_url},
                    "https://p3.douyinpic.com/1.png",
                ],
            }})
        if request.url.host in {"v26-default.365yg.com", "sns-video-bd.xhscdn.com", "p3.douyinpic.com", "sns-img.xhscdn.com"}:
            downloads.append(str(request.url))
            return httpx.Response(200, content=MP4 if str(request.url) == video_url else PNG)
        if provider == "builtin":
            return httpx.Response(200, text=page(live_item()))
        return sdk_response(request, album=True, live_photo=True)

    settings = {"parser_provider": provider, "media_parser_url": "http://127.0.0.1:8051"}
    async with scenario(tmp_path, monkeypatch, settings=settings) as context:
        context.service._transport = httpx.MockTransport(respond)
        original = context.service._client
        context.service._client = httpx.AsyncClient(transport=context.service._transport)
        try:
            for msgid in (1, 2):
                job = await context.submit(source, msgid=msgid)
                assert job["status"] == "completed", job["error"]
                assert job["delivery_status"] == "sent"
                assert job["kind"] == "images"
                assert [file["name"] for file in job["files"]] == ["001.png", "002.mp4", "003.png"]
                assert [file["kind"] for file in job["files"]] == ["image", "video", "image"]
                assert bool(job.get("cache_hit")) == (msgid == 2)
                path = context.service.file_path(job["id"], "002.mp4")
                assert path is not None and path.read_bytes() == MP4
            assert len(downloads) == 3
            assert [route for route, _ in context.hook_calls] == [
                "/SendTextMsg", "/SendImgMsg", "/send_file_msg", "/SendImgMsg",
            ] * 2
            sent = [payload for route, payload in context.hook_calls if route == "/send_file_msg"]
            assert all(Path(payload["filepath"]).name == "002.mp4" for payload in sent)
        finally:
            await context.service._client.aclose()
            context.service._client = original
