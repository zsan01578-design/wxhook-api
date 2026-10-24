import base64
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import time

import httpx
from PIL import Image
import pytest

from tests.test_chatbot import BOT, Answers, context, event
from tests.test_douyin import GROUP
from wxhook_api.core.secrets import reveal_secret
from wxhook_api.services.image_model import ImageModelConfiguration


CONFIG = {"name": "生图主模型", "base_url": "https://image.test/v1", "model": "example/image", "api_key": "image-private-key"}
output = BytesIO()
Image.new("RGB", (32, 32), "blue").save(output, format="PNG")
PNG = output.getvalue()


def image_response():
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})


async def confirmed(request):
    assert Path(json.loads(request.content)["path"]).is_file()
    return httpx.Response(200, json={"ret": 0})


async def add(state, **changes):
    response = await state.client.post("/api/manage/image-models", json={**CONFIG, **changes})
    assert response.status_code == 200 and response.json()["saved"], response.text
    return response.json()["profile"]


async def test_profile_lifecycle_separate_from_chat_persists_and_hides_keys(tmp_path):
    requests = []
    def respond(request):
        requests.append(request)
        return image_response()

    async with context(tmp_path, provider=Answers()) as state:
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(respond)
        original_provider = state.bot.provider
        first = await add(state, base_url=CONFIG["base_url"] + "/images/generations/")
        second = await add(state, name="备用", api_key="backup-key", size="", response_format="b64_json")
        assert first["active"] and not second["active"]
        assert first["base_url"] == CONFIG["base_url"]
        body = json.loads(requests[0].content)
        assert body == {"model": CONFIG["model"], "prompt": "A simple blue circle on a plain white background.", "n": 1, "size": "1024x1024"}
        assert "size" not in json.loads(requests[1].content)
        assert str(requests[0].url) == CONFIG["base_url"] + "/images/generations"
        edited = await state.client.put(f"/api/manage/image-models/{second['id']}", json={**CONFIG, "api_key": None, "model": "edited", "size": "auto"})
        assert edited.json()["saved"] and edited.json()["test"]["preview"].startswith("data:image/png;base64,")
        assert requests[-1].headers["authorization"] == "Bearer backup-key"
        total = len(requests)
        switched = await state.client.post(f"/api/manage/image-models/{second['id']}/activate", json={})
        assert switched.json()["saved"] and len(requests) == total
        assert state.bot.provider is original_provider
        snapshot = await state.client.get("/api/manage/image-model")
        assert snapshot.headers["cache-control"] == "no-store"
        assert "image-private-key" not in snapshot.text and "backup-key" not in snapshot.text
        restarted = ImageModelConfiguration(state.bot, budget=config.budget)
        assert restarted.active_id == second["id"] and restarted.settings.model == "edited"
        assert restarted.settings.api_key.get_secret_value() == "backup-key"
        saved = json.loads(config.path.read_text(encoding="utf-8"))
        assert reveal_secret(saved["models"][0]["api_key"]) == CONFIG["api_key"]
        rejected = await state.client.delete(f"/api/manage/image-models/{second['id']}")
        assert not rejected.json()["saved"]
        deleted = await state.client.delete(f"/api/manage/image-models/{first['id']}")
        assert deleted.json()["saved"] and len(config.models) == 1
        assert state.app.state.backups.configs["image-model.local.json"] == config.path


async def test_failed_test_or_disk_write_preserves_working_profile(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(lambda request: image_response())
        profile = await add(state)
        disk = config.path.read_bytes()
        config.transport = httpx.MockTransport(lambda request: httpx.Response(401, text="image-private-key"))
        failed = await state.client.put(f"/api/manage/image-models/{profile['id']}", json={**CONFIG, "model": "bad"})
        assert not failed.json()["saved"] and "image-private-key" not in failed.text
        assert config.path.read_bytes() == disk and config.settings.model == CONFIG["model"]
        config.transport = httpx.MockTransport(lambda request: image_response())
        def fail(collection):
            raise PermissionError("image-private-key")
        monkeypatch.setattr(config, "_save", fail)
        failed = await state.client.post("/api/manage/image-models", json=CONFIG)
        assert failed.status_code == 500 and "image-private-key" not in failed.text
        assert config.path.read_bytes() == disk and len(config.models) == 1


async def test_key_never_crosses_endpoint_or_profile_and_missing_ids_do_not_create(tmp_path):
    requests = []
    def respond(request):
        requests.append(request)
        return image_response()
    async with context(tmp_path) as state:
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(respond)
        profile = await add(state)
        await add(state, api_key=None)
        assert "authorization" not in requests[-1].headers
        changed = await state.client.put(f"/api/manage/image-models/{profile['id']}", json={**CONFIG, "base_url": "https://other.test/v1", "api_key": None})
        assert not changed.json()["saved"] and len(requests) == 2
        for method, suffix, body in [("PUT", "missing", CONFIG), ("DELETE", "missing", None), ("POST", "missing/activate", {})]:
            response = await state.client.request(method, "/api/manage/image-models/" + suffix, json=body)
            assert not response.json()["saved"]
        assert len(requests) == 2 and len(config.models) == 2


@pytest.mark.parametrize("changes", [{"api_key": "bad\nkey"}, {"base_url": "http://remote.test/v1"}, {"base_url": "https://user:password@image.test"}, {"size": "9999x1024"}, {"size": "hello"}, {"response_format": "html"}, {"enable_thinking": True}])
async def test_invalid_fields_are_rejected_without_secrets(tmp_path, changes):
    async with context(tmp_path) as state:
        response = await state.client.post("/api/manage/image-models", json={**CONFIG, **changes})
        assert response.status_code == 422
        assert "image-private-key" not in response.text and "password" not in response.text


async def test_local_url_download_redirect_does_not_forward_credential(tmp_path):
    requests = []
    def respond(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, headers={"set-cookie": "secret=private; Path=/"}, json={"data": [{"url": "http://localhost:1234/files/image"}]})
        if request.url.path == "/files/image":
            return httpx.Response(302, headers={"location": "/files/final.png"})
        return httpx.Response(200, content=PNG)
    async with context(tmp_path) as state:
        state.app.state.image_model.transport = httpx.MockTransport(respond)
        await add(state, base_url="http://localhost:1234/v1")
        assert len(requests) == 3
        for request in requests[1:]:
            assert "authorization" not in request.headers and "cookie" not in request.headers


@pytest.mark.parametrize("url", ["http://localhost:30001/GetSelfProfile", "https://127.0.0.1/file", "file:///etc/passwd", "https://user:password@image.test/file", "https://10.0.0.1/image"])
async def test_unsafe_returned_url_is_not_requested(tmp_path, url):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"url": url}]})
    async with context(tmp_path) as state:
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(respond)
        result = await state.client.post("/api/manage/image-models", json=CONFIG)
        assert not result.json()["saved"] and len(calls) == 1 and not config.models


@pytest.mark.parametrize("payload", [{}, {"data": []}, {"data": [None]}, {"data": [{"b64_json": "broken!"}]}, {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}])
async def test_malformed_image_does_not_save_configuration(tmp_path, payload):
    async with context(tmp_path) as state:
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        response = await state.client.post("/api/manage/image-models", json=CONFIG)
        assert not response.json()["saved"] and not config.path.exists()


async def test_group_command_sends_exactly_one_image_and_normal_chat_still_works(tmp_path):
    answers, requests = Answers(), []
    def respond(request):
        requests.append(json.loads(request.content))
        return image_response()
    async with context(tmp_path, provider=answers, image_hook=confirmed) as state:
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(respond)
        await add(state)
        for index, command in enumerate(["生图：一只猫", "帮我生图一幅夜景", "一朵花，生图"], 1):
            await state.submit(event("@机器人\u2005" + command, msgid=index))
        assert "生成图片" in state.bot.snapshot()["recent_events"][0]["message"]
        await state.submit(event("@机器人\u2005生图：一只猫", msgid=1))
        assert len(state.image_sends) == 3 and len(requests) == 4 and not state.sends
        assert [item["prompt"] for item in requests[1:]] == ["一只猫", "帮我生图一幅夜景", "一朵花，生图"]
        assert all(item["wxidorgid"] == GROUP for item in state.image_sends)
        assert state.bot.snapshot()["image_configured"]
        await state.submit(event("@机器人\u2005你好", msgid=5))
        assert answers.questions == ["你好"] and len(state.sends) == 1
        assert state.bot.conversations.history(state.bot.identity, GROUP, answers)[0]["content"] == "你好"


@pytest.mark.parametrize("payload, question", [
    (event("生图：一只猫"), None),
    (event("@其他人\u2005生图：一只猫"), None),
    (event("@机器人\u2005生图：一只猫", atlist=["wxid_other"]), None),
    (event("帮我画一个图，一只猫"), None),
    (event("@其他人\u2005帮我画一个图，一只猫"), None),
    (event("帮我画一个图 @机器人\u2005你好"), "帮我画一个图 \u2005你好"),
    (event("@机器人\u2005这张图片是什么"), "这张图片是什么"),
    (event("@机器人\u2005这幅画很好看"), "这幅画很好看"),
    (event("@机器人\u2005生成一份小猫饲养计划"), "生成一份小猫饲养计划"),
    (event("@机器人\u2005生成一个数据表"), "生成一个数据表"),
    (event("@机器人\u2005生成一份报告。图片是什么格式"), "生成一份报告。图片是什么格式"),
    (event("生成一张小猫的图片"), None),
    (event("@其他人\u2005生成一张小猫的图片"), None),
    (event("生图 @机器人\u2005你好"), "生图 \u2005你好"),
])
async def test_image_model_requires_keyword_after_bot_mention(tmp_path, payload, question):
    answers, requests = Answers(), []
    def respond(request):
        requests.append(request)
        return image_response()
    async with context(tmp_path, provider=answers, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(respond)
        await add(state)
        requests.clear()
        await state.submit(payload)
        assert not requests and not state.image_sends
        assert answers.questions == ([] if question is None else [question])


@pytest.mark.parametrize("command, prompt", [
    ("帮我画一个图", "帮我画一个图"),
    ("帮我画一个图：一只猫", "帮我画一个图：一只猫"),
    ("画一张图，一朵花", "画一张图，一朵花"),
    ("画个图：日落", "画个图：日落"),
    ("画一只猫", "画一只猫"),
    ("请画一幅山水画", "请画一幅山水画"),
    ("给我画个可爱的机器人", "给我画个可爱的机器人"),
    ("生成一张图片：夜景", "生成一张图片：夜景"),
    ("生成一张小猫的图片", "生成一张小猫的图片"),
    ("帮我生成一张可爱的小猫图片", "帮我生成一张可爱的小猫图片"),
    ("请生成小猫的图片", "请生成小猫的图片"),
    ("生成一个小猫的图像", "生成一个小猫的图像"),
    ("生成一幅雨中森林插画", "生成一幅雨中森林插画"),
    ("生成一张橘猫照片", "生成一张橘猫照片"),
    ("生成星空壁纸", "生成星空壁纸"),
    ("生成活动海报", "生成活动海报"),
    ("生成一个猫咪头像", "生成一个猫咪头像"),
    ("生成一张小猫图", "生成一张小猫图"),
    ("绘制一幅图：森林", "绘制一幅图：森林"),
    ("画图：一只猫", "一只猫"),
    ("生成图片：一只猫", "一只猫"),
])
async def test_natural_drawing_requests_call_image_model(tmp_path, command, prompt):
    answers, requests = Answers(), []
    def respond(request):
        requests.append(json.loads(request.content))
        return image_response()
    async with context(tmp_path, provider=answers, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(respond)
        await add(state)
        requests.clear()
        await state.submit(event("@机器人\u2005" + command))
        assert len(requests) == 1 and requests[0]["prompt"] == prompt
        assert len(state.image_sends) == 1 and not state.sends and not answers.questions


async def test_image_keyword_with_unsynced_alias_and_structured_bot_id(tmp_path):
    async with context(tmp_path, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@群内别名\u2005帮我生图一只猫", atlist=[BOT]))
        assert len(state.image_sends) == 1 and not state.sends


async def test_screenshot_request_with_bot_display_name_uses_image_model(tmp_path):
    from wxhook_api.core.chatbot_config import ChatbotOptions
    answers, requests = Answers(), []
    def respond(request):
        requests.append(json.loads(request.content))
        return image_response()
    async with context(tmp_path, provider=answers, image_hook=confirmed,
                       bot_options=ChatbotOptions(True, BOT, ("吊毛",))) as state:
        state.app.state.image_model.transport = httpx.MockTransport(respond)
        await add(state)
        requests.clear()
        await state.submit(event("@吊毛\u2005生成一张小猫的图片", atlist=[BOT]))
        assert [request["prompt"] for request in requests] == ["生成一张小猫的图片"]
        assert len(state.image_sends) == 1 and not state.sends and not answers.questions


async def test_missing_config_empty_prompt_and_generation_error_reply_with_text(tmp_path):
    async with context(tmp_path) as state:
        await state.submit(event("@机器人\u2005生图：一只猫"))
        assert "生图模型尚未配置" in state.sends[-1]["msg"]
        await state.submit(event("@机器人\u2005生图", msgid=2))
        assert "填写描述" in state.sends[-1]["msg"]
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        config.transport = httpx.MockTransport(lambda request: httpx.Response(429))
        await state.submit(event("@机器人\u2005生图：一只猫", msgid=3))
        assert "额度不足" in state.sends[-1]["msg"] and not state.image_sends


async def test_policy_change_during_generation_prevents_image_delivery(tmp_path):
    async with context(tmp_path, image_hook=confirmed) as state:
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        async def respond(request):
            await state.app.state.groups.update_group(GROUP, {"blocked": True})
            return image_response()
        config.transport = httpx.MockTransport(respond)
        await state.submit(event("@机器人\u2005生图：一只猫"))
        assert not state.image_sends and not state.sends
        assert not list(config.output_dir.glob("*.png"))


async def test_shared_budget_stops_image_before_outbound_request(tmp_path, monkeypatch):
    monkeypatch.setenv("WXHOOK_AI_DAILY_LIMIT", "1")
    async with context(tmp_path, image_hook=confirmed) as state:
        config, calls = state.app.state.image_model, []
        def respond(request):
            calls.append(request)
            return image_response()
        config.transport = httpx.MockTransport(respond)
        await add(state)
        assert config.budget is state.app.state.chat_model.budget
        await state.submit(event("@机器人\u2005生图：一只猫"))
        assert len(calls) == 1 and "额度已用尽" in state.sends[-1]["msg"]


async def test_cleanup_keeps_recent_and_unrelated_files(tmp_path):
    async with context(tmp_path) as state:
        config = state.app.state.image_model
        config.output_dir.mkdir(parents=True)
        old, recent, unrelated = [config.output_dir / name for name in ["a" * 32 + ".png", "b" * 32 + ".png", "keep.png"]]
        for path in [old, recent, unrelated]:
            path.write_bytes(PNG)
        os.utime(old, (time.time() - 90000,) * 2)
        os.utime(unrelated, (time.time() - 90000,) * 2)
        config.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        result = await config.generate("a cat")
        assert result.is_file() and not old.exists() and recent.exists() and unrelated.exists()


def test_corrupt_config_is_reported_and_does_not_break_startup(tmp_path):
    path = tmp_path / "image-model.local.json"
    path.write_text('{"models": ["image-private-key"]}', encoding="utf-8")
    from wxhook_api.core.chatbot_config import ChatbotOptions
    from wxhook_api.core.ai_safety import ModelBudget
    from wxhook_api.services.chatbot import MentionChatService
    config = ImageModelConfiguration(MentionChatService(ChatbotOptions()), budget=ModelBudget())
    assert config.load_error and config.settings is None
    assert "image-private-key" not in str(config.snapshot())


async def test_public_cdn_image_and_redirect_revalidation(tmp_path, monkeypatch):
    calls, target = [], "https://cdn.example/image.png?signature=private"
    async def resolve(host, port, **kwargs):
        assert host == "cdn.example"
        return [(2, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    redirect_private = False
    def respond(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": target}]})
        if redirect_private:
            return httpx.Response(302, headers={"location": "https://127.0.0.1/secrets"})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=PNG)
    async with context(tmp_path) as state:
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(respond)
        await add(state)
        assert len(calls) == 2
        redirect_private = True
        response = await state.client.post("/api/manage/image-models", json=CONFIG)
        assert not response.json()["saved"] and len(calls) == 4 and len(config.models) == 1


async def test_cdn_resolving_to_private_address_is_blocked(tmp_path, monkeypatch):
    async def resolve(*args, **kwargs):
        return [(2, 1, 6, "", ("192.168.1.1", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image"}]})
    async with context(tmp_path) as state:
        state.app.state.image_model.transport = httpx.MockTransport(respond)
        response = await state.client.post("/api/manage/image-models", json=CONFIG)
        assert not response.json()["saved"] and len(calls) == 1


async def test_oversized_response_stops_without_saving(tmp_path, monkeypatch):
    monkeypatch.setattr("wxhook_api.services.image_model.MAX_JSON_BYTES", 20)
    async with context(tmp_path) as state:
        config = state.app.state.image_model
        config.transport = httpx.MockTransport(lambda request: image_response())
        response = await state.client.post("/api/manage/image-models", json=CONFIG)
        assert not response.json()["saved"] and "过大" in response.json()["test"]["message"]
        assert not config.path.exists()


async def test_timeout_hides_upstream_details_and_does_not_retry(tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("image-private-key", request=request)
    async with context(tmp_path) as state:
        state.app.state.image_model.transport = httpx.MockTransport(respond)
        response = await state.client.post("/api/manage/image-models", json=CONFIG)
        assert not response.json()["saved"] and "超时" in response.json()["test"]["message"]
        assert "image-private-key" not in response.text and len(calls) == 1


async def test_unconfirmed_image_send_is_never_retried(tmp_path):
    async def uncertain(request):
        return httpx.Response(503, json={"ret": -1})
    async with context(tmp_path, image_hook=uncertain) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005生图：一只猫"))
        assert state.bot.snapshot()["recent_events"][0]["status"] == "unknown"
        await state.submit(event("@机器人\u2005生图：一只猫"))
        assert len(state.image_sends) == 1 and not state.sends


def test_png_conversion_preserves_transparency():
    from wxhook_api.services.image_model import image_png
    output = BytesIO()
    Image.new("RGBA", (4, 4), (20, 50, 100, 0)).save(output, format="PNG")
    with Image.open(BytesIO(image_png(output.getvalue()))) as image:
        assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 0
