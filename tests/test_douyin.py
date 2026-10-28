import asyncio
import base64
from dataclasses import replace
import json
from urllib.parse import quote

import httpx
from asgi_lifespan import LifespanManager
import pytest

from tests.test_callbacks import EVENT
from wxhook_api.core.douyin_config import DouyinOptions, GroupSettings, load_douyin_options
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.douyin_parser import DouyinError, DouyinParser, checked_url, extract_links, find_item, page_data


GROUP = "123456789@chatroom"


def profile_transport():
    """Media integration tests use a local synthetic Hook account."""
    def respond(request):
        assert request.url.path == "/GetSelfProfile"
        return httpx.Response(200, json={"wxid": "wxid_robot", "nickname": "机器人"})
    return httpx.MockTransport(respond)


ITEM = "7360323126909095219"
SHORT = "https://v.douyin.com/testshare/"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1cAAAAASUVORK5CYII=")


def options(tmp_path, **settings):
    return DouyinOptions(GroupSettings(**{"enabled": True, "groups": [GROUP], "parser_provider": "builtin", "media_parser_url": "", **settings}), tmp_path / "douyin.local.yml", tmp_path / "downloads")


def page(item):
    payload = {"loaderData": {"note_(id)/page": {"videoInfoRes": {"item_list": [item]}}}}
    return "<script>window._ROUTER_DATA = " + json.dumps(payload).replace("<", r"\u003c") + ";</script>"


def image_item(count=7):
    return {"aweme_id": ITEM, "desc": "图集标题 <script>不会执行</script>", "author": {"nickname": "测试作者"}, "images": [{"url_list": [f"https://p3.douyinpic.com/{index}.png"]} for index in range(count)]}


def test_extracts_share_text_xml_and_rejects_lookalike_domains():
    text = ('抖音分享 https://v.douyin.com/testshare/ 复制此链接 '
            '<url><![CDATA[https://www.douyin.com/note/7360323126909095219?a=1&amp;b=2]]></url> '
            'https://douyin.com.evil.example/video/123456 https://douyin.com@evil.example/ '
            'https://www.douyin.com:1234/video/123456 https://v.douyin.com/testshare/')
    assert extract_links(text) == [SHORT, f"https://www.douyin.com/note/{ITEM}?a=1&b=2"]


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "https://douyin.com.evil.test/a", "https://user:pass@www.douyin.com/a", "https://www.douyin.com:8080/a"])
def test_untrusted_urls_are_rejected_before_request(url):
    with pytest.raises(DouyinError):
        checked_url(url, media=True)


def test_router_json_and_encoded_render_data_preserve_titles_and_large_ids():
    item = image_item(1)
    router = page(item)
    payload = page_data(router)[0]
    assert find_item(payload, ITEM)["aweme_id"] == ITEM
    encoded = quote(json.dumps({"item": item}))
    assert find_item(page_data(f'<script id="RENDER_DATA">{encoded}</script>')[0], ITEM)["images"] == item["images"]
    data = page_data('<script>window._ROUTER_DATA={"desc":"undefined","optional":undefined};</script>')[0]
    assert data == {"desc": "undefined", "optional": None}


async def test_short_link_redirect_to_private_host_is_not_followed():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:30001/GetDecodeKey"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DouyinError):
            await DouyinParser(client).parse(SHORT)
    assert calls == [SHORT]


async def test_only_configured_groups_are_processed_and_zero_message_ids_are_not_deduplicated(tmp_path):
    service = DouyinService(options(tmp_path))
    service._accepting = True
    await service.receive(CallbackEvent.model_validate({**EVENT, "content": SHORT}))
    await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": "other@chatroom", "content": SHORT}))
    assert service.snapshot()["jobs"] == []
    event = CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "content": SHORT})
    await service.receive(event)
    await service.receive(event)
    assert len(service.snapshot()["jobs"]) == 1
    zero = event.model_copy(update={"msgid": 0})
    await service.receive(zero)
    await service.receive(zero)
    assert len(service.snapshot()["jobs"]) == 3


async def test_callback_ack_is_fast_while_image_album_downloads_in_background(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    received = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(str(request.url))
        if request.url.host == "v.douyin.com":
            started.set()
            await release.wait()
            return httpx.Response(302, headers={"location": f"https://www.douyin.com/note/{ITEM}"})
        if request.url.host == "www.douyin.com":
            return httpx.Response(200, text=page(image_item()))
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

    service = DouyinService(options(tmp_path), transport=httpx.MockTransport(respond))
    app = create_app(douyin_service=service, hook_transport=profile_transport())

    async def observe(event):
        received.set()

    app.state.event_dispatcher.register(observe)
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        accepted = await asyncio.wait_for(client.post("/receiveChatBotMsg", json={**EVENT, "roomid": GROUP, "content": SHORT}), timeout=1)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert accepted.status_code == 200
        assert (await client.get("/health")).status_code == 200
        release.set()
        await asyncio.wait_for(received.wait(), timeout=1)
        await asyncio.wait_for(service._queue.join(), timeout=2)
        jobs = (await client.get("/api/douyin/jobs")).json()["jobs"]
        job = jobs[0]
        assert job["status"] == "completed", job["error"]
        assert len(job["files"]) == 7
        assert [file["name"] for file in job["files"]] == [f"{i:03d}.png" for i in range(1, 8)]
        url = f'/api/douyin/files/{job["id"]}/001.png'
        saved = await client.get(url)
        assert saved.content == PNG
        assert saved.headers["content-type"] == "image/png"
        attachment = await client.get(url + "?download=true")
        assert "attachment" in attachment.headers["content-disposition"]
        assert service.file_path(job["id"], "../../douyin.local.yml") is None
        assert (await client.get(f'/api/douyin/files/{job["id"]}/999.png')).status_code == 404
    # Saved metadata and downloads remain accessible after a fresh service starts.
    reloaded = DouyinService(options(tmp_path), transport=httpx.MockTransport(lambda request: pytest.fail("must not request network on startup")))
    await reloaded.start()
    try:
        assert reloaded.snapshot()["jobs"][0]["status"] == "completed"
        assert reloaded.file_path(job["id"], "001.png").read_bytes() == PNG
    finally:
        await reloaded.stop()


async def test_video_save_and_size_limit_failure_cleanup(tmp_path):
    video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40

    def respond(request):
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": f"https://www.douyin.com/video/{ITEM}"})
        if request.url.host == "www.douyin.com":
            item = {"aweme_id": ITEM, "video": {"play_addr": {"url_list": ["https://v3.douyinvod.com/test.mp4"]}}}
            return httpx.Response(200, text=page(item))
        return httpx.Response(200, content=video)

    service = DouyinService(options(tmp_path), transport=httpx.MockTransport(respond))
    await service.start()
    try:
        event = CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "content": SHORT})
        await service.receive(event)
        await asyncio.wait_for(service._queue.join(), timeout=2)
        job = service.snapshot()["jobs"][0]
        assert job["files"][0]["name"] == "001.mp4"
        assert service.file_path(job["id"], "001.mp4").read_bytes() == video
        service.options = replace(service.options, max_file_bytes=10)
        await service.receive(event.model_copy(update={"msgid": 2}))
        await asyncio.wait_for(service._queue.join(), timeout=2)
        failed = service.snapshot()["jobs"][0]
        assert failed["status"] == "failed"
        assert "大小限制" in failed["error"]
        assert not list((service.options.output_dir / failed["id"]).glob("*.part"))
    finally:
        await service.stop()


async def test_configuration_is_validated_persisted_and_effective_without_restart(tmp_path, monkeypatch):
    service = DouyinService(options(tmp_path, auto_download=False))
    app = create_app(douyin_service=service, hook_transport=profile_transport())
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        invalid = await client.post("/api/douyin/config", json={"enabled": True, "groups": ["群昵称"], "auto_download": True})
        assert invalid.status_code == 422
        assert not service.options.config_path.exists()
        saved = await client.post("/api/douyin/config", json={"enabled": False, "groups": [GROUP], "auto_download": False})
        assert saved.json()["saved"] is True
        assert not service.allowed(GROUP)
        assert (await client.get("/douyin")).status_code == 200
    monkeypatch.setenv("WXHOOK_DOUYIN_CONFIG", str(service.options.config_path))
    loaded = load_douyin_options()
    assert loaded.settings.enabled is False
    assert loaded.settings.groups == [GROUP]
    assert loaded.settings.auto_download is False


async def test_disabling_download_keeps_media_urls_without_fetching_files(tmp_path):
    hosts = []

    def respond(request):
        hosts.append(request.url.host)
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": f"https://www.douyin.com/note/{ITEM}"})
        return httpx.Response(200, text=page(image_item(2)))

    service = DouyinService(options(tmp_path, auto_download=False), transport=httpx.MockTransport(respond))
    await service.start()
    try:
        await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "content": SHORT}))
        await asyncio.wait_for(service._queue.join(), timeout=2)
        job = service.snapshot()["jobs"][0]
        assert job["status"] == "parsed"
        assert len(job["assets"]) == 2
        assert job["files"] == []
        assert hosts == ["v.douyin.com", "www.douyin.com"]
    finally:
        await service.stop()


async def test_standard_url_can_fall_back_to_public_share_page():
    def respond(request):
        if request.url.host == "www.douyin.com":
            return httpx.Response(403)
        return httpx.Response(200, text=page(image_item(1)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await DouyinParser(client).parse(f"https://www.douyin.com/note/{ITEM}")
    assert result.kind == "images"
    assert len(result.assets) == 1
