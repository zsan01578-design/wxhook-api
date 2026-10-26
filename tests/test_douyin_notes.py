import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tests.test_douyin import ITEM, SHORT, image_item, page
from tests.test_douyin_fallback import run_job
from wxhook_api.main import create_app
from wxhook_api.services.douyin_parser import DouyinParser, find_item, page_data
from wxhook_api.services.parse_video import ParseVideoParser


def note_response(request):
    if request.url.host == "v.douyin.com":
        return httpx.Response(302, headers={"location": f"https://www.douyin.com/note/{ITEM}"})
    if "slidesinfo" in request.url.path:
        return httpx.Response(200, json={"aweme_details": None})
    if request.url.path == f"/share/video/{ITEM}/":
        return httpx.Response(200, text=page(image_item(2)))
    return httpx.Response(200, text="<html></html>")


def test_router_data_can_be_a_json_string_with_trailing_javascript():
    payload = {"item": image_item(2)}
    encoded = json.dumps(json.dumps(payload)).replace("<", r"\u003c")
    document = f"<script>window._ROUTER_DATA = {encoded}; window.ready=true;</script>"
    assert find_item(page_data(document)[0], ITEM) == payload["item"]


@pytest.mark.parametrize("source", [SHORT, f"https://www.douyin.com/note/{ITEM}"])
async def test_sdk_empty_note_recovers_images_from_public_video_page(source):
    result = await ParseVideoParser(transport=httpx.MockTransport(note_response)).parse(source)
    assert result.item_id == ITEM
    assert result.kind == "images"
    assert result.author == "测试作者"
    assert [asset["urls"][0] for asset in result.assets] == [f"https://p3.douyinpic.com/{i}.png" for i in range(2)]


async def test_empty_sdk_media_recovers_public_note_images():
    def respond(request):
        if "slidesinfo" in request.url.path:
            return httpx.Response(200, json={"aweme_details": [{"aweme_id": ITEM, "desc": "empty"}]})
        if request.url.path == f"/share/note/{ITEM}/":
            return httpx.Response(200, text=page(image_item(2)))
        return note_response(request)

    result = await ParseVideoParser(transport=httpx.MockTransport(respond)).parse(SHORT)
    assert result.kind == "images" and len(result.assets) == 2


async def test_short_link_id_survives_desktop_http_failure():
    def respond(request):
        if request.url.host == "www.douyin.com":
            return httpx.Response(403)
        return note_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await DouyinParser(client).parse(SHORT)
    assert result.item_id == ITEM and len(result.assets) == 2


async def test_group_job_recovers_without_third_party_api(tmp_path):
    job, calls = await run_job(tmp_path, note_response, parser_provider="parse_video", parser_api_url="")
    assert job["status"] == "parsed", job["error"]
    assert job["kind"] == "images" and len(job["assets"]) == 2
    assert all("jk.y-l.cc" not in url for url in calls)


async def test_parse_api_uses_the_same_note_recovery():
    app = create_app(hook_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    app.state.parse_video_parser = ParseVideoParser(transport=httpx.MockTransport(note_response))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/parse", json={"url": SHORT})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "images" and len(response.json()["assets"]) == 2
