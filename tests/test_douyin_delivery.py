import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
from asgi_lifespan import LifespanManager
from PIL import Image
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, ITEM, PNG, SHORT, image_item, options, page
from wxhook_api.clients.hook_client import HookClient
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.douyin_delivery import DouyinMessenger, prepare_image


@asynccontextmanager
async def scenario(tmp_path, monkeypatch, *, images=2, video=False, broken_page=False, hook=None, settings=None, media=PNG):
    monkeypatch.setenv("WXHOOK_DOUYIN_FEEDBACK_ENABLED", "true")
    context = SimpleNamespace(hook_calls=[], order=[])

    async def hook_response(request):
        payload = json.loads(request.content)
        context.hook_calls.append((request.url.path, payload))
        context.order.append(request.url.path)
        if hook:
            return await hook(request, context)
        return httpx.Response(200, json={"ret": 0})

    def media_response(request):
        context.order.append("parse" if request.url.host in {"v.douyin.com", "www.douyin.com"} else "download")
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": f"https://www.douyin.com/video/{ITEM}"})
        if request.url.host in {"www.douyin.com", "www.iesdouyin.com", "m.douyin.com"}:
            item = {"aweme_id": ITEM, "video": {"play_addr": {"url_list": ["https://v3.douyinvod.com/test.mp4"]}}} if video else image_item(images)
            return httpx.Response(200, text="<html>no public data</html>" if broken_page else page(item))
        return httpx.Response(200, content=b"\0\0\0\x18ftypisom" + b"\0" * 40 if video else media)

    context.service = DouyinService(options(tmp_path, **(settings or {})), transport=httpx.MockTransport(media_response))
    hook_client = HookClient("http://hook.test", transport=httpx.MockTransport(hook_response))
    context.app = create_app(douyin_service=context.service, hook_client=hook_client)
    try:
        async with LifespanManager(context.app), httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://test") as client:
            context.client = client

            async def submit(content=SHORT, msgid=1):
                result = await client.post("/receiveChatBotMsg", json={**EVENT, "msgid": msgid, "roomid": GROUP, "content": content})
                assert result.status_code == 200
                await asyncio.wait_for(context.app.state.event_dispatcher._queue.join(), 2)
                await asyncio.wait_for(context.service._queue.join(), 3)
                return context.service.snapshot()["jobs"][0]

            context.submit = submit
            yield context
    finally:
        await hook_client.aclose()


async def test_start_notice_precedes_parsing_and_images_are_returned_in_order(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch, images=3) as context:
        job = await context.submit()
        assert context.order[0] == "/SendTextMsg"
        assert context.order.index("/SendTextMsg") < context.order.index("parse")
        assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/SendImgMsg", "/SendImgMsg", "/SendImgMsg", "/SendTextMsg"]
        images = [payload for path, payload in context.hook_calls if path == "/SendImgMsg"]
        assert all(payload["wxidorgid"] == GROUP and Path(payload["path"]).is_absolute() for payload in images)
        assert [Path(payload["path"]).name for payload in images] == ["001.png", "002.png", "003.png"]
        assert job["delivery_status"] == "sent"
        assert all(file["send_status"] == "sent" for file in job["files"])
        # Automatic text callbacks contain no source link and cannot recurse.
        for path, payload in context.hook_calls:
            if path == "/SendTextMsg":
                assert "http" not in payload["msg"]
                await context.service.receive(CallbackEvent.model_validate({**EVENT, "msgid": 999, "roomid": GROUP, "content": payload["msg"]}))
        assert len(context.service.snapshot()["jobs"]) == 1


async def test_video_is_sent_as_mp4_file_using_documented_api(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch, video=True) as context:
        job = await context.submit()
        files = [payload for path, payload in context.hook_calls if path == "/send_file_msg"]
        assert len(files) == 1
        assert files[0]["wxid"] == GROUP and files[0]["stage"] == "send"
        assert Path(files[0]["filepath"]).suffix == ".mp4"
        assert job["delivery_status"] == "sent"
        assert not any(path == "/SendImgMsg" for path, _ in context.hook_calls)


async def test_parse_failure_posts_failure_notice_without_sending_media(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch, broken_page=True) as context:
        job = await context.submit()
        assert job["status"] == "failed"
        assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/SendTextMsg"]
        assert "开始解析" in context.hook_calls[0][1]["msg"]
        assert "解析失败" in context.hook_calls[1][1]["msg"]
        assert job["notifications"]["failure"]["status"] == "sent"


async def test_start_notification_failure_does_not_stop_successful_parse(tmp_path, monkeypatch):
    async def respond(request, context):
        return httpx.Response(200, json={"ret": -1 if len(context.hook_calls) == 1 else 0})

    async with scenario(tmp_path, monkeypatch, hook=respond) as context:
        job = await context.submit()
        assert job["notifications"]["start"]["status"] == "failed"
        assert job["status"] == "completed" and job["delivery_status"] == "sent"


@pytest.mark.parametrize("timeout", [False, True])
async def test_media_failure_stops_remaining_album_without_retry(tmp_path, monkeypatch, timeout):
    count = 0

    async def respond(request, context):
        nonlocal count
        if request.url.path == "/SendImgMsg":
            count += 1
            if count == 2:
                if timeout:
                    raise httpx.ReadTimeout("uncertain")
                return httpx.Response(200, json={"ret": -1})
        return httpx.Response(200, json={"ret": 0})

    async with scenario(tmp_path, monkeypatch, images=3, hook=respond) as context:
        job = await context.submit()
        assert count == 2
        assert job["status"] == "completed"  # Parsing and saving succeeded.
        assert job["delivery_status"] == ("unknown" if timeout else "partial")
        assert [file.get("send_status") for file in job["files"]] == ["sent", "unknown" if timeout else "failed", None]
        assert "已确认 1/3" in context.hook_calls[-1][1]["msg"]
        assert job["notifications"]["delivery_failure"]["status"] == "sent"


async def test_blacklisting_during_delivery_suppresses_further_media_and_notices(tmp_path, monkeypatch):
    async def respond(request, context):
        if request.url.path == "/SendImgMsg":
            await context.app.state.groups.update_group(GROUP, {"blocked": True})
        return httpx.Response(200, json={"ret": 0})

    async with scenario(tmp_path, monkeypatch, images=3, hook=respond) as context:
        job = await context.submit()
        assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/SendImgMsg"]
        assert job["delivery_status"] == "partial"
        assert job["notifications"]["delivery_failure"]["status"] == "skipped"


async def test_closed_feedback_settings_send_nothing(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch, settings={"notify_group": False, "send_results": False}) as context:
        job = await context.submit()
        assert job["status"] == "completed" and job["delivery_status"] == "disabled"
        assert context.hook_calls == []


async def test_missing_download_posts_explanation_instead_of_fake_success(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch, settings={"auto_download": False}) as context:
        job = await context.submit()
        assert job["status"] == "parsed" and job["delivery_status"] == "skipped"
        assert all(path == "/SendTextMsg" for path, _ in context.hook_calls)
        assert "未回传" in context.hook_calls[-1][1]["msg"]


def test_webp_conversion_keeps_original_and_creates_valid_png(tmp_path):
    source = tmp_path / "001.webp"
    Image.new("RGB", (8, 8), "red").save(source, format="WEBP")
    original = source.read_bytes()
    target, mode = prepare_image(source)
    assert mode == "image" and target.name == "send-001.png"
    assert source.read_bytes() == original
    with Image.open(target) as image:
        assert image.format == "PNG" and image.size == (8, 8)


async def test_restart_does_not_resend_persisted_callback(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch) as context:
        job = await context.submit(msgid=987)
    reloaded = DouyinService(options(tmp_path), transport=httpx.MockTransport(lambda request: pytest.fail("must not reparse")))
    await reloaded.start()
    try:
        await reloaded.receive(CallbackEvent.model_validate({**EVENT, "msgid": 987, "roomid": GROUP, "content": SHORT}))
        assert len(reloaded.snapshot()["jobs"]) == 1
        assert reloaded.snapshot()["jobs"][0]["id"] == job["id"]
        assert reloaded._queue.empty()
    finally:
        await reloaded.stop()


async def test_webp_album_is_sent_using_png_copy(tmp_path, monkeypatch):
    content = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(content, format="WEBP")
    async with scenario(tmp_path, monkeypatch, images=1, media=content.getvalue()) as context:
        job = await context.submit()
        assert job["files"][0]["name"] == "001.webp"
        path = next(payload["path"] for route, payload in context.hook_calls if route == "/SendImgMsg")
        assert Path(path).name == "send-001.png"
        assert job["delivery_status"] == "sent"


async def test_policy_is_rechecked_after_durable_write_before_send(tmp_path, monkeypatch):
    async with scenario(tmp_path, monkeypatch, images=1) as context:
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = context.service._save
        paused = False

        def save(job):
            nonlocal paused
            original(job)
            if job.get("delivery_status") == "sending" and not paused:
                paused = True
                loop.call_soon_threadsafe(entered.set)
                release.wait(2)

        monkeypatch.setattr(context.service, "_save", save)
        submitted = asyncio.create_task(context.submit())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            await context.app.state.groups.update_group(GROUP, {"blocked": True})
            release.set()
            job = await submitted
            assert [route for route, payload in context.hook_calls] == ["/SendTextMsg"]
            assert job["files"][0]["send_status"] == "skipped"
        finally:
            release.set()


async def test_queue_full_notice_is_background_and_duplicate_callbacks_notify_once(tmp_path):
    messages = []

    def respond(request):
        messages.append(json.loads(request.content)["msg"])
        return httpx.Response(200, json={"ret": 0})

    async with HookClient("http://hook.test", transport=httpx.MockTransport(respond)) as client:
        service = DouyinService(options(tmp_path))
        service._queue = asyncio.Queue(maxsize=1)
        service._accepting = True
        service.messenger = DouyinMessenger(client)
        await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "msgid": 1, "content": SHORT}))
        duplicate = CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "msgid": 2, "content": SHORT})
        await service.receive(duplicate)
        await service.receive(duplicate)
        assert messages == []  # The callback does not await outbound HTTP.
        service._notice_worker = asyncio.create_task(service._run_notices())
        try:
            await asyncio.wait_for(service._notices.join(), timeout=2)
            assert len(messages) == 1 and "队列已满" in messages[0]
        finally:
            await service.stop()
