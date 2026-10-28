import asyncio
import json
from pathlib import Path

import httpx
from parse_video_py import ImgInfo, VideoAuthor, VideoInfo
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, ITEM, PNG, SHORT, options
from tests.test_douyin_delivery import scenario
from wxhook_api.core.douyin_config import GroupSettings, load_douyin_options, save_group_settings
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.douyin_parser import DouyinError, extract_links
from wxhook_api.services import parse_video as adapter
from wxhook_api.services.parse_video import ParseVideoParser, REFERERS, normalize_media


XHS = "https://xhslink.cn/o/sample"
BILI = "https://www.bilibili.com/video/BV1test123456/?share_source=copy_web"
VIDEO = "https://v26-default.365yg.com/video/test.mp4"
XHS_VIDEO = "https://sns-video-bd.xhscdn.com/test.mp4"
BILI_VIDEO = "https://upos-sz-mirrorali.bilivideo.com/test.mp4"
MP4 = b"\0\0\0\x18ftypisom" + b"\0" * 40


@pytest.fixture(autouse=True)
def isolated_cookie_config(tmp_path, monkeypatch):
    monkeypatch.setenv("WXHOOK_PARSE_VIDEO_CONFIG", str(tmp_path / "parse-video.local.yml"))
    monkeypatch.delenv("PARSE_VIDEO_XHS_COOKIE", raising=False)
    monkeypatch.delenv("PARSE_VIDEO_PROXY", raising=False)


def test_extracts_three_platforms_from_share_text_and_xml_without_losing_signed_query():
    xhs = "https://www.xiaohongshu.com/explore/test?xsec_token=abc%2B123&xsec_source=pc_share"
    content = f"分享 {SHORT} <url><![CDATA[{xhs.replace('&', '&amp;')}]]></url> {BILI} https://xhslink.cn.evil.test/o/a {SHORT}"
    assert extract_links(content, multi_platform=True) == [SHORT, xhs, BILI]
    assert extract_links(content) == [SHORT]


def test_real_sdk_models_preserve_gallery_order_without_cover_music_or_live_photo():
    images = [ImgInfo(url=f"https://ci.xiaohongshu.com/{i}.jpg", live_photo_url=XHS_VIDEO) for i in range(2)]
    info = VideoInfo(video_url=XHS_VIDEO, cover_url="https://ci.xiaohongshu.com/cover.jpg", music_url=VIDEO, images=images, title="图文", author=VideoAuthor(name="作者"))
    result = normalize_media(info, XHS)
    assert result.platform == "xiaohongshu" and result.kind == "images"
    assert result.author == "作者"
    assert [asset["urls"][0] for asset in result.assets] == [image.url for image in images]


@pytest.mark.parametrize("info", [{}, {"video_url": "http://127.0.0.1:30001/x"}, {"images": [{}]}, {"images": [ImgInfo(url=VIDEO)] * 101}])
def test_invalid_sdk_media_is_never_accepted(info):
    with pytest.raises(DouyinError):
        normalize_media(info, SHORT)


def sdk_response(request, *, album=False, fail=False):
    host, path = request.url.host, request.url.path
    if host == "v.douyin.com":
        return httpx.Response(302, headers={"location": f"https://www.douyin.com/note/{ITEM}"})
    if "slidesinfo" in path:
        if fail:
            return httpx.Response(200, json={"aweme_details": None})
        item = {"desc": "抖音测试", "author": {"nickname": "作者"}, "video": {"play_addr": {"url_list": [VIDEO]}}}
        if album:
            item["images"] = [{"url_list": [f"https://p3.douyinpic.com/{i}.png"]} for i in range(2)]
        return httpx.Response(200, json={"aweme_details": [item]})
    if host == "www.iesdouyin.com":
        return httpx.Response(200, text='window._ROUTER_DATA = {"loaderData":{"video_(id)/page":{}}}</script>')
    if host == "xhslink.cn":
        return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/explore/test?xsec_token=signed"})
    if host == "www.xiaohongshu.com":
        data = {"title": "小红书测试", "user": {"userId": "test", "nickname": "作者", "avatar": ""}, "imageList": [{"urlDefault": f"https://sns-img.xhscdn.com/{i}.png"} for i in range(2)]}
        if not album:
            data["video"] = {"media": {"stream": {"h264": [{"masterUrl": XHS_VIDEO}]}}}
        state = {"note": {"currentNoteId": "test", "noteDetailMap": {"test": {"note": data}}}}
        return httpx.Response(200, text="<script>window.__INITIAL_STATE__=" + json.dumps(state) + "</script>")
    if host == "b23.tv":
        return httpx.Response(302, headers={"location": BILI})
    if host == "api.bilibili.com":
        if path.endswith("view"):
            return httpx.Response(200, json={"code": 0, "data": {"pages": [{"cid": 1}], "title": "B站测试", "owner": {"name": "作者"}}})
        return httpx.Response(200, json={"code": 0, "data": {"durl": [{"url": BILI_VIDEO}]}})
    return httpx.Response(200, content=PNG if album else MP4, headers={"content-type": "image/png" if album else "video/mp4"})


@pytest.mark.parametrize("source,platform,album", [(SHORT, "douyin", False), (SHORT, "douyin", True), (XHS, "xiaohongshu", False), (XHS, "xiaohongshu", True), (BILI, "bilibili", False), ("https://b23.tv/sample", "bilibili", False)])
async def test_actual_sdk_parses_all_platforms_and_galleries_with_mocked_http(source, platform, album):
    requests = []

    def respond(request):
        requests.append(request)
        return sdk_response(request, album=album)

    result = await ParseVideoParser(transport=httpx.MockTransport(respond)).parse(source)
    assert result.platform == platform and result.kind == ("images" if album else "video")
    assert len(result.assets) == (2 if album else 1)
    assert requests
    assert all(request.url.host != "127.0.0.1" for request in requests)


async def test_metadata_redirect_cannot_reach_local_hook():
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://127.0.0.1:30001/GetDecodeKey"})

    with pytest.raises(DouyinError, match="域名"):
        await ParseVideoParser(transport=httpx.MockTransport(respond)).parse(XHS)
    assert calls == ["xhslink.cn"]


async def test_xhs_login_has_actionable_error_and_cookie_only_goes_to_main_host(tmp_path, monkeypatch):
    path = tmp_path / "parse-video.local.yml"
    path.write_text("xiaohongshu-cookie: 'a1=private-test-value'", encoding="utf-8")
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host == "www.xiaohongshu.com":
            assert request.headers["cookie"] == "a1=private-test-value"
            return httpx.Response(200)
        assert "cookie" not in request.headers
        return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/login"})

    with pytest.raises(DouyinError, match="parse-video.local.yml") as error:
        await ParseVideoParser(transport=httpx.MockTransport(respond)).parse(XHS)
    assert "private-test-value" not in str(error.value)
    assert len(calls) == 2
    monkeypatch.setenv("PARSE_VIDEO_XHS_COOKIE", "a1=env-value")
    assert adapter._local_xhs_cookie() == "a1=env-value"


async def test_bad_cookie_is_local_error_without_network_or_secret_echo(monkeypatch):
    monkeypatch.setenv("PARSE_VIDEO_XHS_COOKIE", "secret\r\nInjected: true")
    calls = []
    with pytest.raises(DouyinError, match="Cookie 格式无效") as error:
        await ParseVideoParser(transport=httpx.MockTransport(lambda request: calls.append(request))).parse(XHS)
    assert "secret" not in str(error.value) and calls == []


async def test_cookie_is_not_sent_over_http_or_to_media_cdn(monkeypatch):
    monkeypatch.setenv("PARSE_VIDEO_XHS_COOKIE", "a1=local-only")
    for url in ["http://www.xiaohongshu.com/explore/test", "https://sns-video-bd.xhscdn.com/test.mp4", XHS]:
        token = adapter._request_context.set(adapter._RequestContext(xhs_cookie=adapter._local_xhs_cookie()))
        try:
            request = httpx.Request("GET", url)
            await adapter._guard_request(request)
            assert "cookie" not in request.headers
        finally:
            adapter._request_context.reset(token)


class CountedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.reads, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def test_binary_redirect_probe_does_not_load_video_into_memory():
    stream = CountedStream([MP4] * 100)

    def respond(request):
        if request.url.host == "v26-default.365yg.com":
            return httpx.Response(200, stream=stream, headers={"content-type": "video/mp4"})
        return sdk_response(request)

    result = await ParseVideoParser(transport=httpx.MockTransport(respond)).parse(SHORT)
    assert result.assets[0]["urls"] == [VIDEO]
    assert stream.reads == 0 and stream.closed


async def test_oversized_page_stops_stream_and_is_not_a_valid_parse():
    stream = CountedStream([b"x" * (1024 * 1024)] * 5)
    with pytest.raises(DouyinError, match="页面过大"):
        await ParseVideoParser(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))).parse(XHS)
    assert stream.reads == 4 and stream.closed


async def test_concurrent_parses_keep_transports_and_cookies_isolated(monkeypatch):
    monkeypatch.setenv("PARSE_VIDEO_XHS_COOKIE", "a1=local-only")
    xhs_calls, bili_calls = [], []

    async def xhs(request):
        xhs_calls.append(request.url.host)
        if request.url.host != "www.xiaohongshu.com":
            assert "cookie" not in request.headers
        await asyncio.sleep(0)
        return sdk_response(request)

    async def bili(request):
        bili_calls.append(request.url.host)
        assert "cookie" not in request.headers
        await asyncio.sleep(0)
        return sdk_response(request)

    results = await asyncio.gather(ParseVideoParser(transport=httpx.MockTransport(xhs)).parse(XHS), ParseVideoParser(transport=httpx.MockTransport(bili)).parse(BILI))
    assert [result.platform for result in results] == ["xiaohongshu", "bilibili"]
    assert xhs_calls == ["xhslink.cn", "www.xiaohongshu.com"]
    assert bili_calls == ["api.bilibili.com", "api.bilibili.com"]


@pytest.mark.parametrize("source,album", [(SHORT, False), (SHORT, True), (XHS, False), (XHS, True), (BILI, False)])
async def test_real_sdk_download_and_group_return_pipeline(tmp_path, monkeypatch, source, album):
    async with scenario(tmp_path, monkeypatch, settings={"parser_provider": "parse_video"}) as context:
        downloads = []

        def respond(request):
            if request.url.host in {"p3.douyinpic.com", "sns-img.xhscdn.com", "sns-video-bd.xhscdn.com", "upos-sz-mirrorali.bilivideo.com"} or request.url.host == "v26-default.365yg.com":
                downloads.append(request)
            return sdk_response(request, album=album)

        context.service._transport = httpx.MockTransport(respond)
        original = context.service._client
        context.service._client = httpx.AsyncClient(transport=context.service._transport)
        try:
            job = await context.submit(source)
            assert job["status"] == "completed", job["error"]
            assert job["delivery_status"] == "sent"
            assert job["parser_provider"] == "parse_video"
            assert len(job["files"]) == (2 if album else 1)
            paths = [path for path, _ in context.hook_calls]
            assert paths == ["/SendTextMsg", *(["/SendImgMsg"] * 2 if album else ["/send_file_msg"])]
            assert context.hook_calls[0][1]["msg"] == "开始解析"
            assert "complete" not in job["notifications"]
            assert downloads[-1].headers["referer"] == REFERERS[job["platform"]]
            assert Path(context.service.options.output_dir, job["id"], "result.json").is_file()
        finally:
            await context.service._client.aclose()
            context.service._client = original


async def test_sdk_failure_notifies_group_without_sending_any_media(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch, settings={"parser_provider": "parse_video"}) as context:
        context.service._transport = httpx.MockTransport(lambda request: sdk_response(request, fail=True))
        job = await context.submit()
        assert job["status"] == "failed" and not job["files"]
        assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/SendTextMsg"]
        assert "解析失败" in context.hook_calls[-1][1]["msg"]
        assert "http" not in context.hook_calls[-1][1]["msg"]


async def test_unconfigured_group_does_not_parse_other_platforms(tmp_path):
    service = DouyinService(options(tmp_path, parser_provider="parse_video"))
    service._accepting = True
    await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": "other@chatroom", "content": XHS + " " + BILI}))
    assert service.snapshot()["jobs"] == []


def test_old_provider_migrates_once_while_groups_are_preserved(tmp_path, monkeypatch):
    path = tmp_path / "douyin.local.yml"
    path.write_text(f"enabled: true\ngroups: ['{GROUP}']\nparser-provider: api\nnotify-group: false\n", encoding="utf-8")
    monkeypatch.setenv("WXHOOK_DOUYIN_CONFIG", str(path))
    config = load_douyin_options()
    assert config.settings.parser_provider == "parse_video"
    assert config.settings.groups == [GROUP] and not config.settings.notify_group
    save_group_settings(config, GroupSettings(**{**config.settings.model_dump(), "parser_provider": "builtin"}))
    assert load_douyin_options().settings.parser_provider == "builtin"


@pytest.mark.parametrize("provider", ["api", "builtin"])
@pytest.mark.parametrize("source", [BILI, XHS])
async def test_old_provider_no_longer_silently_drops_non_douyin_links_in_managed_group(tmp_path, monkeypatch, provider, source):
    async with scenario(tmp_path, monkeypatch, settings={"enabled": False, "groups": [], "parser_provider": provider}) as context:
        await context.client.post("/api/manage/groups", json={"id": GROUP})
        await context.client.patch(f"/api/manage/groups/{GROUP}", json={"douyin_enabled": True, "auto_download": True})

        def unexpected(request):
            pytest.fail("An unsupported parser must not request a media platform")

        context.service._transport = httpx.MockTransport(unexpected)
        job = await context.submit(source)
        assert job["status"] == "failed"
        assert "parse-video-py" in job["error"]
        assert job["parser_provider"] == provider
        assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/SendTextMsg"]
        assert "开始解析" in context.hook_calls[0][1]["msg"]
        assert "解析失败" in context.hook_calls[-1][1]["msg"]


@pytest.mark.parametrize("missing_roomid", [False, True])
async def test_management_enabled_bili_card_parses_without_legacy_group_list(tmp_path, monkeypatch, missing_roomid):
    async with scenario(tmp_path, monkeypatch, settings={"enabled": False, "groups": [], "parser_provider": "parse_video"}) as context:
        await context.client.post("/api/manage/groups", json={"id": GROUP})
        await context.client.patch(f"/api/manage/groups/{GROUP}", json={"douyin_enabled": True, "auto_download": True})
        context.service._transport = httpx.MockTransport(sdk_response)
        original = context.service._client
        context.service._client = httpx.AsyncClient(transport=context.service._transport)
        try:
            content = f"<msg><appmsg><url><![CDATA[{BILI}]]></url></appmsg></msg>"
            if missing_roomid:
                payload = {**EVENT, "wxid": GROUP, "content": content}
                payload.pop("roomid")
                response = await context.client.post("/receiveChatBotMsg", json=payload)
                assert response.status_code == 200
                await asyncio.wait_for(context.app.state.event_dispatcher._queue.join(), 2)
                await asyncio.wait_for(context.service._queue.join(), 3)
                job = context.service.snapshot()["jobs"][0]
            else:
                job = await context.submit(content)
            assert job["status"] == "completed", job["error"]
            assert job["delivery_status"] == "sent" and job["platform"] == "bilibili"
            assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/send_file_msg"]
            assert context.hook_calls[0][1]["msg"] == "开始解析"
            assert "complete" not in job["notifications"]
            state = (await context.client.get("/api/manage/state")).json()
            assert state["callbacks"]["total"] == 1 and state["callbacks"]["last_received_at"]
            assert state["media"] == {"parser_provider": "parse_video", "notify_group": True, "send_results": True, "feedback_available": True}
            assert (await context.client.get("/api/douyin/config")).json()["supported_parsers"] == ["parse_video", "api", "builtin", "media_parser"]
        finally:
            await context.service._client.aclose()
            context.service._client = original
