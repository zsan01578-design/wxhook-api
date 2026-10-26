import asyncio

import httpx
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, ITEM, SHORT, options
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin import DouyinService


API = "https://jk.y-l.cc/api/dsp/demo/user/"
XHS = "https://xhslink.cn/o/sample"


def note_failure(request):
    host, path = request.url.host, request.url.path
    if host == "v.douyin.com":
        return httpx.Response(302, headers={"location": f"https://www.douyin.com/note/{ITEM}"})
    if host == "www.iesdouyin.com" and "/slidesinfo/" in path:
        return httpx.Response(200, json={"aweme_details": None})
    if host in {"www.douyin.com", "www.iesdouyin.com", "m.douyin.com"}:
        return httpx.Response(200, text='window._ROUTER_DATA = {"loaderData":{"video_(id)/page":{}}}</script>')
    pytest.fail(f"unexpected request: {request.url}")


def api_success(request):
    if request.url.host == "jk.y-l.cc":
        return httpx.Response(200, json={
            "code": 200,
            "data": {"aweme_id": ITEM, "title": "图文标题", "author": {"nickname": "作者"},
                     "pics": ["https://p3.douyinpic.com/1.png"]},
        })
    return note_failure(request)


async def run_job(tmp_path, respond, content=SHORT, **settings):
    calls = []

    def spy(request):
        calls.append(str(request.url))
        return respond(request)

    service = DouyinService(options(tmp_path, auto_download=False, **settings), transport=httpx.MockTransport(spy))
    await service.start()
    try:
        await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "content": content}))
        await asyncio.wait_for(service._queue.join(), timeout=5)
        return service.snapshot()["jobs"][0], calls
    finally:
        await service.stop()


async def test_douyin_note_falls_back_to_configured_api(tmp_path):
    job, calls = await run_job(tmp_path, api_success, parser_provider="parse_video", parser_api_url=API)
    assert job["status"] == "parsed", job["error"]
    assert job["parser_provider"] == "api"
    assert job["parser_fallback"] is True
    assert job["platform"] == "douyin"
    assert job["title"] == "图文标题"
    assert job["assets"] == [{"kind": "image", "urls": ["https://p3.douyinpic.com/1.png"]}]
    assert any("jk.y-l.cc" in url for url in calls)
    assert any("v.douyin.com" in url for url in calls)


async def test_douyin_note_without_api_url_keeps_primary_error(tmp_path):
    job, calls = await run_job(tmp_path, note_failure, parser_provider="parse_video", parser_api_url="")
    assert job["status"] == "failed"
    assert "未返回有效媒体数据" in job["error"]
    assert not any("jk.y-l.cc" in url for url in calls)


async def test_non_douyin_failure_does_not_use_douyin_api(tmp_path):
    def xhs_login(request):
        if request.url.host == "xhslink.cn":
            return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/explore/test"})
        if request.url.host == "www.xiaohongshu.com":
            return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/login"})
        pytest.fail(f"unexpected request: {request.url}")

    job, calls = await run_job(tmp_path, xhs_login, content=XHS, parser_provider="parse_video", parser_api_url=API)
    assert job["status"] == "failed"
    assert "登录" in job["error"]
    assert not any("jk.y-l.cc" in url for url in calls)


async def test_api_fallback_failure_reports_both_causes(tmp_path):
    def api_failure(request):
        if request.url.host == "jk.y-l.cc":
            return httpx.Response(200, json={"code": 108, "msg": "会员接口不存在"})
        return note_failure(request)

    job, _ = await run_job(tmp_path, api_failure, parser_provider="parse_video", parser_api_url=API)
    assert job["status"] == "failed"
    assert "parse-video-py 解析失败" in job["error"]
    assert "备用接口也失败" in job["error"]
    assert "108" in job["error"]
