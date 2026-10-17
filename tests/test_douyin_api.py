import asyncio
from unittest.mock import Mock

import httpx
from pydantic import ValidationError
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, PNG, SHORT, options
from tests.test_douyin_delivery import scenario
from wxhook_api.core.douyin_config import GroupSettings, load_douyin_options, save_group_settings
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.douyin_api import DouyinAPIParser, parse_api_response
from wxhook_api.services.douyin_parser import DouyinError


ENDPOINT = "https://jk.y-l.cc/api/dsp/test/account/"
VIDEO = "http://v3-default.ixigua.com/video/test.mp4?a=1768&mime_type=video_mp4"
IMAGE = "https://p26-sign.douyinpic.com/test.webp"


def video_response():
    return {"status": 101, "code": 200, "msg": "解析成功12", "data": {"title": "视频标题", "pics": None, "url": VIDEO, "down": VIDEO + "&download=1", "cover": IMAGE}}


def test_success_is_code_200_despite_status_101_and_cover_is_not_photo():
    result = parse_api_response(video_response(), SHORT)
    assert result.kind == "video"
    assert result.title == "视频标题"
    assert result.assets == [{"kind": "video", "urls": [VIDEO, VIDEO + "&download=1"]}]


def test_photos_have_priority_over_video_url_and_preserve_order():
    data = video_response()
    data["data"]["pics"] = [IMAGE + "?i=1", {"url": IMAGE + "?i=2"}]
    result = parse_api_response(data, SHORT)
    assert result.kind == "images"
    assert [asset["urls"][0] for asset in result.assets] == [IMAGE + "?i=1", IMAGE + "?i=2"]


def test_download_image_is_used_when_pics_is_empty():
    data = video_response()
    data["data"].update(pics=[], download_image=[IMAGE])
    assert parse_api_response(data, SHORT).kind == "images"


@pytest.mark.parametrize("value", [None, {}, {"code": 200, "data": None}, {"code": 200, "data": {"url": "", "pics": []}}, {"code": 200, "data": {"pics": "not-an-array"}}])
def test_invalid_and_empty_success_responses_are_not_marked_success(value):
    with pytest.raises(DouyinError):
        parse_api_response(value, SHORT)


def test_membership_error_is_not_accepted_as_media():
    with pytest.raises(DouyinError, match="108.*会员接口不存在"):
        parse_api_response({"status": 108, "code": 108, "msg": "会员接口不存在", "data": {"url": "", "pics": []}}, SHORT)


def test_provider_error_does_not_echo_share_links_into_group_notifications():
    with pytest.raises(DouyinError) as error:
        parse_api_response({"code": 500, "msg": "失败 " + SHORT}, SHORT)
    assert "https://" not in str(error.value)


@pytest.mark.parametrize("media_url", ["http://127.0.0.1/private", "file:///C:/secret", "https://v3-default.ixigua.com.evil.test/x.mp4"])
def test_api_cannot_supply_arbitrary_download_targets(media_url):
    data = video_response()
    data["data"]["url"] = media_url
    with pytest.raises(DouyinError):
        parse_api_response(data, SHORT)


async def test_get_parameter_is_encoded_as_one_url_and_json_slashes_are_decoded():
    source = "https://www.douyin.com/video/7360323126909095219?copy=1&from=share"
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.params["url"] == source
        return httpx.Response(200, json=video_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await DouyinAPIParser(client, ENDPOINT).parse(source)
    assert len(calls) == 1
    assert result.assets[0]["urls"][0] == VIDEO


@pytest.mark.parametrize("kind", ["non_json", "http_error", "timeout", "redirect"])
async def test_network_and_invalid_responses_report_api_errors(kind):
    def respond(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("should not expose endpoint")
        if kind == "http_error":
            return httpx.Response(502)
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/"})
        return httpx.Response(200, text="<html>not JSON</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DouyinError) as error:
            await DouyinAPIParser(client, ENDPOINT).parse(SHORT)
    assert ENDPOINT not in str(error.value)


async def test_missing_endpoint_is_actionable_without_any_request():
    handler = Mock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DouyinError, match="尚未配置"):
            await DouyinAPIParser(client, "").parse(SHORT)
    handler.assert_not_called()


@pytest.mark.parametrize("value", ["http://jk.y-l.cc/api/dsp/test/account/", "https://evil.test/api/dsp/test/account/", ENDPOINT + "?url=" + SHORT, ENDPOINT + "#fragment"])
def test_local_endpoint_configuration_rejects_wrong_hosts_or_query_parameters(value):
    with pytest.raises(ValidationError):
        GroupSettings(parser_api_url=value)


async def test_configuration_validation_explains_field_without_echoing_rejected_values(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch) as context:
        for body, expected in [({"groups": ["private-group-name"]}, "群 ID"), ({"parser_provider": "unknown-engine"}, "解析来源"), ({"parser_api_url": "https://secret.invalid/private-account"}, "接口地址")]:
            response = await context.client.post("/api/douyin/config", json=body)
            assert response.status_code == 422
            assert expected in response.json()["error"]["message"]
            assert "private-group-name" not in response.text and "private-account" not in response.text


def test_endpoint_is_saved_only_in_local_settings_and_env_can_override(tmp_path, monkeypatch):
    config = options(tmp_path)
    settings = GroupSettings(enabled=True, groups=[GROUP], parser_provider="api", parser_api_url=ENDPOINT)
    saved = save_group_settings(config, settings)
    monkeypatch.setenv("WXHOOK_DOUYIN_CONFIG", str(saved.config_path))
    assert load_douyin_options().settings.parser_api_url == ENDPOINT
    override = "https://jk.y-l.cc/api/dsp/override/account/"
    monkeypatch.setenv("WXHOOK_DOUYIN_API_URL", override)
    assert load_douyin_options().settings.parser_api_url == override


@pytest.mark.parametrize("album", [False, True])
async def test_service_uses_api_then_existing_download_pipeline(tmp_path, album):
    calls = []

    def respond(request):
        calls.append(request.url.host)
        if request.url.host == "jk.y-l.cc":
            data = video_response()
            if album:
                data["data"]["pics"] = ["https://p26-sign.douyinpic.com/one.png", "https://p26-sign.douyinpic.com/two.png"]
            return httpx.Response(200, json=data)
        return httpx.Response(200, content=PNG if album else b"\0\0\0\x18ftypisom" + b"\0" * 40)

    config = options(tmp_path, parser_provider="api", parser_api_url=ENDPOINT)
    service = DouyinService(config, transport=httpx.MockTransport(respond))
    await service.start()
    try:
        await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "content": SHORT}))
        await asyncio.wait_for(service._queue.join(), 2)
        job = service.snapshot()["jobs"][0]
        assert job["status"] == "completed", job["error"]
        assert job["parser_provider"] == "api"
        assert len(job["files"]) == (2 if album else 1)
        assert set(calls) == {"jk.y-l.cc", "p26-sign.douyinpic.com" if album else "v3-default.ixigua.com"}
    finally:
        await service.stop()


async def test_api_business_failure_notifies_group_without_falling_back(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch, settings={"parser_provider": "api", "parser_api_url": ENDPOINT}) as context:
        calls = []

        def failure(request):
            calls.append(request.url.host)
            return httpx.Response(200, json={"code": 108, "status": 108, "msg": "会员接口不存在", "data": {}})

        old = context.service._client
        context.service._client = httpx.AsyncClient(transport=httpx.MockTransport(failure))
        try:
            job = await context.submit()
            assert job["status"] == "failed"
            assert job["parser_provider"] == "api"
            assert "108" in job["error"] and "会员接口不存在" in job["error"]
            assert calls == ["jk.y-l.cc"]
            assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/SendTextMsg"]
            assert "会员接口不存在" in context.hook_calls[-1][1]["msg"]
        finally:
            await context.service._client.aclose()
            context.service._client = old


@pytest.mark.parametrize("album", [False, True])
async def test_api_success_reuses_group_notices_and_media_return(tmp_path, monkeypatch, album):
    async with scenario(tmp_path, monkeypatch, settings={"parser_provider": "api", "parser_api_url": ENDPOINT}) as context:
        def respond(request):
            if request.url.host == "jk.y-l.cc":
                payload = video_response()
                if album:
                    payload["data"]["pics"] = ["https://p26-sign.douyinpic.com/one.png", "https://p26-sign.douyinpic.com/two.png"]
                return httpx.Response(200, json=payload)
            return httpx.Response(200, content=PNG if album else b"\0\0\0\x18ftypisom" + b"\0" * 40)

        old = context.service._client
        context.service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            job = await context.submit()
            assert job["delivery_status"] == "sent"
            assert [path for path, _ in context.hook_calls] == (["/SendTextMsg", "/SendImgMsg", "/SendImgMsg"] if album else ["/SendTextMsg", "/send_file_msg"])
            assert context.hook_calls[0][1]["msg"] == "开始解析"
            assert "complete" not in job["notifications"]
        finally:
            await context.service._client.aclose()
            context.service._client = old
