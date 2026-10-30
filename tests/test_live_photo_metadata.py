import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.test_douyin import ITEM, PNG, SHORT, image_item
from tests.test_douyin_delivery import scenario
from tests.test_parse_video import MP4, VIDEO, sdk_response
from wxhook_api.services.douyin_parser import find_item, page_data, parse_item
from wxhook_api.services.media_parser import enrich_live_photos
from wxhook_api.services.parse_video import ParseVideoParser


@pytest.mark.parametrize("video", [
    {"video": {"play_addr": {"uri": "v0300_live_sample"}}},
    {"video": {"playAddr": [{"src": VIDEO}]}},
    {"video_play_addr": {"url_list": [VIDEO]}},
])
async def test_actual_sdk_does_not_discard_raw_live_photo_metadata(video):
    def respond(request):
        if "slidesinfo" in request.url.path:
            item = image_item(1)
            item["images"][0].update(video)
            return httpx.Response(200, json={"aweme_details": [item]})
        return sdk_response(request, album=True)

    parsed = await ParseVideoParser(transport=httpx.MockTransport(respond)).parse(SHORT)
    assert [asset["kind"] for asset in parsed.assets] == ["image", "video"]
    url = parsed.assets[1]["urls"][0]
    if "uri" in str(video):
        assert urlsplit(url).hostname == "www.iesdouyin.com"
        assert parse_qs(urlsplit(url).query)["video_id"] == ["v0300_live_sample"]
    else:
        assert url == VIDEO


def test_streaming_desktop_data_and_camelcase_live_photo_addresses():
    item = {"awemeId": ITEM, "desc": "实况", "authorInfo": {"nickname": "作者"}, "images": [
        {"urlList": [{"src": "https://p3.douyinpic.com/still.webp"}], "video": {"playAddr": [{"src": VIDEO}]}}
    ]}
    chunk = "a:" + json.dumps({"detail": item}) + "\n"
    document = "<script>self.__pace_f.push([1," + json.dumps(chunk) + "]);</script>"
    parsed = parse_item(find_item(page_data(document)[0], ITEM), ITEM)
    assert parsed.author == "作者"
    assert parsed.assets[-1] == {"kind": "video", "urls": [VIDEO]}


@pytest.mark.parametrize("video", [
    {"play_addr": {"uri": "http://127.0.0.1/private"}},
    {"play_addr": {"uri": "../private"}},
    {"playAddr": [{"src": "https://evil.example/private.mp4"}]},
    {"play_addr": {"uri": "music_mp3_sample"}},
])
def test_untrusted_live_photo_addresses_never_become_downloads(video):
    item = image_item(1)
    item["images"][0]["video"] = video
    assert [asset["kind"] for asset in parse_item(item, ITEM).assets] == ["image"]


async def test_successful_static_gallery_uses_secondary_motion_tracks():
    media = parse_item(image_item(1), ITEM)
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"retcode": 200, "succ": True, "data": {
            "image_list": [{"url": media.assets[0]["urls"][0], "live_photo_url": VIDEO}],
        }})

    enriched, notice, used = await enrich_live_photos(
        media, SHORT, "http://127.0.0.1:8051", transport=httpx.MockTransport(respond))
    assert used and not notice and calls == ["/api/v1/parse"]
    assert [asset["kind"] for asset in enriched.assets] == ["image", "video"]


async def test_secondary_failure_keeps_images_and_reports_missing_motion():
    media = parse_item(image_item(1), ITEM)
    enriched, notice, used = await enrich_live_photos(
        media, SHORT, "http://127.0.0.1:8051", transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    assert enriched is media and not used
    assert "仅获取到静态图片" in notice and "补充解析未成功" in notice


async def test_incomplete_secondary_gallery_does_not_replace_originals():
    media = parse_item(image_item(2), ITEM)
    response = {"retcode": 200, "succ": True, "data": {
        "image_list": [{"url": media.assets[0]["urls"][0], "live_photo_url": VIDEO}],
    }}
    enriched, notice, used = await enrich_live_photos(
        media, SHORT, "http://127.0.0.1:8051", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    assert enriched is media and not used and notice


async def test_raw_metadata_is_isolated_between_concurrent_douyin_parses():
    async def parse(item_id):
        source = f"https://www.douyin.com/note/{item_id}"

        async def respond(request):
            await asyncio.sleep(0)
            if "slidesinfo" in request.url.path:
                item = image_item(1)
                item["aweme_id"] = item_id
                item["images"][0]["video"] = {"play_addr": {"uri": f"video_{item_id}"}}
                return httpx.Response(200, json={"aweme_details": [item]})
            return httpx.Response(404)

        return await ParseVideoParser(transport=httpx.MockTransport(respond)).parse(source)

    results = await asyncio.gather(parse("123456789"), parse("987654321"))
    for media, item_id in zip(results, ("123456789", "987654321")):
        assert media.item_id == item_id
        assert parse_qs(urlsplit(media.assets[-1]["urls"][0]).query)["video_id"] == [f"video_{item_id}"]


async def test_uri_only_live_photo_download_and_group_return(tmp_path, monkeypatch):
    def respond(request):
        if "slidesinfo" in request.url.path:
            item = image_item(1)
            item["images"][0]["video"] = {"play_addr": {"uri": "v0300_live_sample"}}
            return httpx.Response(200, json={"aweme_details": [item]})
        if request.url.path == "/aweme/v1/play/":
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        if request.url.host == "p3.douyinpic.com":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return sdk_response(request, album=True)

    async with scenario(tmp_path, monkeypatch, settings={"parser_provider": "parse_video"}) as context:
        context.service._transport = httpx.MockTransport(respond)
        original = context.service._client
        context.service._client = httpx.AsyncClient(transport=context.service._transport)
        try:
            job = await context.submit()
            assert job["status"] == "completed", job["error"]
            assert job["delivery_status"] == "sent"
            assert [file["name"] for file in job["files"]] == ["001.png", "002.mp4"]
            assert not job["media_notice"]
            assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/SendImgMsg", "/send_file_msg"]
        finally:
            await context.service._client.aclose()
            context.service._client = original


async def test_static_sdk_success_still_uses_local_enrichment_and_returns_video(tmp_path, monkeypatch):
    def respond(request):
        if "slidesinfo" in request.url.path:
            return httpx.Response(200, json={"aweme_details": [image_item(1)]})
        if request.url.path == "/api/v1/parse":
            return httpx.Response(200, json={"retcode": 200, "succ": True, "data": {
                "image_list": [{"url": "https://p3.douyinpic.com/0.png", "live_photo_url": VIDEO}],
            }})
        if str(request.url) == VIDEO:
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        if request.url.host == "p3.douyinpic.com":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return sdk_response(request, album=True)

    async with scenario(tmp_path, monkeypatch, settings={
        "parser_provider": "parse_video", "media_parser_url": "http://127.0.0.1:8051",
    }) as context:
        context.service._transport = httpx.MockTransport(respond)
        original = context.service._client
        context.service._client = httpx.AsyncClient(transport=context.service._transport)
        try:
            job = await context.submit()
            assert job["status"] == "completed", job["error"]
            assert job["parser_provider"] == "media_parser" and job["parser_fallback"]
            assert [file["name"] for file in job["files"]] == ["001.png", "002.mp4"]
            assert not job["media_notice"]
            assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/SendImgMsg", "/send_file_msg"]
        finally:
            await context.service._client.aclose()
            context.service._client = original
