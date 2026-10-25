import json

import httpx
import pytest

from tests.test_chat_model import CONFIG, completion
from tests.test_chatbot import BOT, context, event
from tests.test_vision import make_image
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.vision_model import TEST_IMAGE, VisionModelConfiguration


async def test_independent_vision_profiles_route_images_and_restore_on_restart(tmp_path):
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return completion("图片里是红色色块。" if isinstance(payload["messages"][-1]["content"], list) else "文字回答")

    async with context(tmp_path, image_path=make_image(tmp_path / "photo.png")) as state:
        chat = state.app.state.chat_model
        vision = state.app.state.vision_model
        chat.transport = vision.transport = httpx.MockTransport(respond)
        chat_result = await state.client.post("/api/manage/chat-models", json=CONFIG)
        assert chat_result.json().get("saved"), chat_result.text
        chat_provider = state.bot.provider
        chat_file = chat.path.read_bytes()
        result = await state.client.post("/api/manage/vision-models", json={**CONFIG, "model": "vision/main"})
        assert result.status_code == 200 and result.json()["saved"]
        assert result.json()["profile"]["supports_vision"] is True
        assert calls[-1]["messages"][-1]["content"][1]["image_url"]["url"] == TEST_IMAGE
        assert not state.sends
        assert state.bot.provider is chat_provider and chat.path.read_bytes() == chat_file
        assert vision.budget is chat.budget is state.app.state.image_model.budget
        assert state.bot.snapshot()["vision_configured"] is True

        await state.submit(event(type=3, content="", msgid=101, atlist=[BOT]))
        await state.submit(event("@机器人\u2005这张图是什么颜色", msgid=102))
        assert calls[-1]["model"] == "vision/main"
        assert any(item["role"] == "assistant" for item in calls[-1]["messages"])
        await state.submit(event("@机器人\u2005你好", msgid=103))
        assert calls[-1]["model"] == CONFIG["model"]
        assert isinstance(calls[-1]["messages"][-1]["content"], str)
        assert state.sends[-1]["msg"] == "文字回答"

        snapshot = await state.client.get("/api/manage/vision-model")
        assert snapshot.headers["cache-control"] == "no-store"
        assert "test-private-key" not in snapshot.text and "test-private-key" not in result.text
        bot = MentionChatService(ChatbotOptions())
        restored = VisionModelConfiguration(bot, budget=chat.budget)
        assert restored.settings.model == "vision/main" and restored.settings.supports_vision
        assert bot.provider is None and bot.vision_provider.supports_vision


async def test_vision_works_without_a_text_model(tmp_path):
    async with context(tmp_path, image_path=make_image(tmp_path / "photo.png")) as state:
        state.app.state.vision_model.transport = httpx.MockTransport(lambda request: completion("识图成功"))
        assert (await state.client.post("/api/manage/vision-models", json=CONFIG)).json()["saved"]
        assert state.bot.provider is None
        await state.submit(event(type=3, content="", msgid=101, atlist=[BOT]))
        assert state.sends[-1]["msg"] == "识图成功"


async def test_vision_fallback_update_activation_and_delete_are_independent(tmp_path):
    calls = []
    fail_main = False

    def respond(request):
        payload = json.loads(request.content)
        calls.append((payload["model"], request.headers["authorization"]))
        if fail_main and payload["model"] == "main":
            return httpx.Response(402)
        return completion("识图成功")

    async with context(tmp_path, image_path=make_image(tmp_path / "photo.png")) as state:
        vision = state.app.state.vision_model
        vision.transport = httpx.MockTransport(respond)
        main = (await state.client.post("/api/manage/vision-models", json={**CONFIG, "model": "main"})).json()["profile"]["id"]
        backup = (await state.client.post("/api/manage/vision-models", json={**CONFIG, "model": "backup", "api_key": "backup-key"})).json()["profile"]["id"]
        assert (await state.client.put("/api/manage/vision-model/fallbacks", json={"fallback_ids": [backup]})).json()["saved"]
        fail_main = True
        calls.clear()
        await state.submit(event(type=3, content="", msgid=101, atlist=[BOT]))
        assert calls == [("main", "Bearer test-private-key"), ("backup", "Bearer backup-key")]
        assert vision.last_route["used_id"] == backup
        assert not (await state.client.delete(f"/api/manage/vision-models/{main}")).json()["saved"]
        result = await state.client.put(f"/api/manage/vision-models/{backup}", json={**CONFIG, "model": "backup2", "api_key": None})
        assert result.json()["saved"] and calls[-1] == ("backup2", "Bearer backup-key")
        assert (await state.client.post(f"/api/manage/vision-models/{backup}/activate")).json()["saved"]
        assert vision.active_id == backup and not vision.fallback_ids
        assert (await state.client.delete(f"/api/manage/vision-models/{main}")).json()["saved"]
        assert list(vision.models) == [backup]
        assert not state.app.state.chat_model.models and state.bot.provider is None


async def test_failed_image_test_preserves_saved_vision_and_key_requires_reentry(tmp_path):
    async with context(tmp_path) as state:
        config = state.app.state.vision_model
        config.transport = httpx.MockTransport(lambda request: completion())
        profile = (await state.client.post("/api/manage/vision-models", json=CONFIG)).json()["profile"]
        original = config.path.read_bytes()
        provider = state.bot.vision_provider
        config.transport = httpx.MockTransport(lambda request: httpx.Response(400))
        result = await state.client.put(f"/api/manage/vision-models/{profile['id']}", json={**CONFIG, "model": "no-images"})
        assert not result.json()["saved"]
        assert config.path.read_bytes() == original and state.bot.vision_provider is provider
        result = await state.client.put(f"/api/manage/vision-models/{profile['id']}", json={**CONFIG, "base_url": "https://other.test/v1", "api_key": None})
        assert not result.json()["saved"] and "重新填写" in result.json()["test"]["message"]


@pytest.mark.parametrize("changes", [{"supports_vision": False}, {"base_url": "https://secret@model.test"}, {"api_key": "secret\n" + "key"}])
async def test_vision_validation_hides_secrets(tmp_path, changes):
    async with context(tmp_path) as state:
        response = await state.client.post("/api/manage/vision-models", json={**CONFIG, **changes})
        assert response.status_code == 422 and "secret" not in response.text and "test-private-key" not in response.text
