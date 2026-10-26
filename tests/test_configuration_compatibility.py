"""Configuration files edited by Windows tools must survive a restart."""

from datetime import date
import json
from pathlib import Path

import pytest

from wxhook_api.core.ai_safety import ModelBudget
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.chat_model import ChatModelConfiguration
from wxhook_api.services.image_model import ImageModelConfiguration
from wxhook_api.services.vision_model import VisionModelConfiguration
from wxhook_api.services.web_search import SearchSettings, WebSearchConfiguration


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
@pytest.mark.parametrize("kind", ["chat", "vision", "image", "search", "legacy_chat"])
def test_configuration_loads_with_or_without_utf8_bom(tmp_path, monkeypatch, encoding, kind):
    profile = {"id": "primary", "model": "example/model", "base_url": "https://model.test/v1"}
    data = {"version": 2, "active_id": "primary", "models": [profile]}
    if kind == "vision":
        profile["supports_vision"] = True
    elif kind == "image":
        data["version"] = 1
    elif kind == "legacy_chat":
        data = {key: value for key, value in profile.items() if key != "id"}
    elif kind == "search":
        data = {**SearchSettings().model_dump(), "usage_date": "2026-10-26", "used_today": 7}
    path = tmp_path / f"{kind}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding=encoding)
    bot = MentionChatService(ChatbotOptions())
    if kind in {"chat", "legacy_chat"}:
        config = ChatModelConfiguration(bot, path=path)
    elif kind == "vision":
        monkeypatch.setenv("WXHOOK_VISION_MODEL_CONFIG", str(path))
        config = VisionModelConfiguration(bot)
    elif kind == "image":
        monkeypatch.setenv("WXHOOK_IMAGE_MODEL_CONFIG", str(path))
        config = ImageModelConfiguration(bot, budget=ModelBudget())
    else:
        config = WebSearchConfiguration(path, now=lambda: date(2026, 10, 26))
    assert not config.load_error
    if kind == "search":
        assert config.used_today == 7
        assert config.usage_date == "2026-10-26"
    else:
        assert config.models[config.active_id].model == "example/model"


@pytest.mark.parametrize("host, expected", [
    ("127.0.0.1", "http://127.0.0.1:8123"),
    ("0.0.0.0", "http://127.0.0.1:8123"),
    ("::", "http://[::1]:8123"),
    ("::1", "http://[::1]:8123"),
    ("[::1]", "http://[::1]:8123"),
    ("localhost", "http://localhost:8123"),
])
async def test_health_callback_uses_listener_address_and_backend_port(host, expected):
    import httpx

    from wxhook_api.core.config import Settings
    from wxhook_api.launcher import service_url
    from wxhook_api.main import create_app

    settings = Settings(Path("unused.yml"), host, 8123, "http://127.0.0.1:30001", {})
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["callback_url"] == service_url(settings) + "/receiveChatBotMsg" == expected + "/receiveChatBotMsg"
