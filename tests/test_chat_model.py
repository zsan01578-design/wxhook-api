import json

import httpx
import pytest

from tests.test_chatbot import context, event
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.chat_model import ChatModelConfiguration, ChatCompletionsProvider, ModelError, ModelSettings


CONFIG = {"base_url": "https://model.test/v1/", "model": "example/chat", "api_key": "test-private-key"}


def completion(text="模型连接正常。"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


async def test_web_save_tests_once_activates_group_answer_and_survives_restart(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return completion("测试成功" if len(calls) == 1 else "这是模型的实际回复")

    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(respond)
        response = await state.client.post("/api/manage/chat-model", json=CONFIG)
        assert response.status_code == 200 and response.json()["saved"]
        assert response.json()["test"]["reply"] == "测试成功"
        assert len(calls) == 1 and state.sends == []
        assert str(calls[0].url) == "https://model.test/v1/chat/completions"
        assert calls[0].headers["authorization"] == "Bearer test-private-key"
        payload = json.loads(calls[0].content)
        assert payload["model"] == "example/chat" and payload["stream"] is False
        assert len(payload["messages"]) == 1 and "连接测试" in payload["messages"][0]["content"]
        await state.submit(event())
        assert len(calls) == 2 and state.sends[0]["msg"] == "这是模型的实际回复"
        assert json.loads(calls[1].content)["messages"] == [{"role": "user", "content": "你好"}]
        for endpoint in ("/api/manage/chat-model", "/api/manage/state"):
            result = await state.client.get(endpoint)
            assert "test-private-key" not in result.text
            assert result.headers["cache-control"] == "no-store"
        assert "test-private-key" not in response.text
        disk = json.loads(config.path.read_text(encoding="utf-8"))
        assert disk["api_key"] == "test-private-key"
        restarted_bot = MentionChatService(ChatbotOptions())
        restarted = ChatModelConfiguration(restarted_bot)
        assert restarted.snapshot()["has_api_key"]
        assert isinstance(restarted_bot.provider, ChatCompletionsProvider)


async def test_failure_preserves_previous_config_and_blank_key_reuses_it(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return completion()

    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(respond)
        await state.client.post("/api/manage/chat-model", json=CONFIG)
        old_provider, old_file = state.bot.provider, config.path.read_bytes()
        config.transport = httpx.MockTransport(lambda request: httpx.Response(401, text="test-private-key"))
        result = (await state.client.post("/api/manage/chat-model", json={**CONFIG, "model": "bad"})).json()
        assert not result["saved"] and not result["test"]["ok"]
        assert "API Key" in result["test"]["message"] and "test-private-key" not in str(result)
        assert state.bot.provider is old_provider and config.path.read_bytes() == old_file
        config.transport = httpx.MockTransport(respond)
        result = (await state.client.post("/api/manage/chat-model", json={**CONFIG, "model": "new-model", "api_key": None})).json()
        assert result["saved"] and calls[-1].headers["authorization"] == "Bearer test-private-key"
        assert config.settings.model == "new-model"


async def test_changed_endpoint_cannot_receive_a_saved_key_without_reentry(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return completion()

    async with context(tmp_path) as state:
        state.app.state.chat_model.transport = httpx.MockTransport(respond)
        await state.client.post("/api/manage/chat-model", json=CONFIG)
        response = await state.client.post("/api/manage/chat-model", json={**CONFIG, "base_url": "https://different.test/v1", "api_key": ""})
        assert not response.json()["saved"] and len(calls) == 1
        assert "重新填写" in response.json()["test"]["message"]


@pytest.mark.parametrize("status", [302, 401, 403, 404, 429, 500])
async def test_upstream_failures_are_safe_and_never_save(tmp_path, status):
    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(lambda request: httpx.Response(status,
            headers={"location": "https://other.test"}, json={"error": {"message": "test-private-key"}}))
        result = await state.client.post("/api/manage/chat-model", json=CONFIG)
        assert not result.json()["saved"] and "test-private-key" not in result.text
        assert not config.path.exists() and state.bot.provider is None


@pytest.mark.parametrize("kind", ["timeout", "network", "json", "empty", "missing"])
async def test_invalid_or_unavailable_models_fail_the_automatic_test(tmp_path, kind):
    def respond(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("test-private-key")
        if kind == "network":
            raise httpx.ConnectError("test-private-key")
        if kind == "json":
            return httpx.Response(200, text="not-json test-private-key")
        if kind == "missing":
            return httpx.Response(200, json={"choices": []})
        return completion(" ")

    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(respond)
        response = await state.client.post("/api/manage/chat-model", json=CONFIG)
        assert not response.json()["test"]["ok"]
        assert "test-private-key" not in response.text and not config.path.exists()


@pytest.mark.parametrize("changes", [{"base_url": "file:///tmp"}, {"base_url": "https://user:secret@model.test/v1"},
    {"base_url": "https://model.test/v1?key=secret"}, {"base_url": "https://model.test:wrong/v1"},
    {"model": " "}, {"api_key": "secret\ninvalid"}])
async def test_validation_does_not_echo_sensitive_values(tmp_path, changes):
    async with context(tmp_path) as state:
        response = await state.client.post("/api/manage/chat-model", json={**CONFIG, **changes})
        assert response.status_code == 422
        assert "test-private-key" not in response.text and "secret" not in response.text


async def test_save_error_keeps_running_provider_and_does_not_claim_success(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(lambda request: completion())

        def fail(settings):
            raise PermissionError("test-private-key")

        monkeypatch.setattr(config, "_save", fail)
        response = await state.client.post("/api/manage/chat-model", json=CONFIG)
        assert response.status_code == 500 and "test-private-key" not in response.text
        assert config.settings is None and state.bot.provider is None


async def test_local_model_without_key_and_full_endpoint_are_supported(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return completion()

    async with context(tmp_path) as state:
        state.app.state.chat_model.transport = httpx.MockTransport(respond)
        result = await state.client.post("/api/manage/chat-model", json={"base_url": "http://127.0.0.1:1234/v1/chat/completions", "model": "local"})
        assert result.json()["saved"] and not result.json()["settings"]["has_api_key"]
        assert str(calls[0].url) == "http://127.0.0.1:1234/v1/chat/completions"
        assert "authorization" not in calls[0].headers


async def test_test_reply_redacts_key_and_limits_length(tmp_path):
    async with context(tmp_path) as state:
        state.app.state.chat_model.transport = httpx.MockTransport(lambda request: completion("test-private-key" + "答" * 1000))
        response = await state.client.post("/api/manage/chat-model", json=CONFIG)
        assert response.json()["saved"] and len(response.json()["test"]["reply"]) == 500
        assert "test-private-key" not in response.text


def test_corrupt_persisted_config_does_not_break_startup(tmp_path, monkeypatch):
    path = tmp_path / "chat-model.local.json"
    path.write_text('{"api_key":"secret"}', encoding="utf-8")
    monkeypatch.setenv("WXHOOK_CHAT_MODEL_CONFIG", str(path))
    bot = MentionChatService(ChatbotOptions())
    config = ChatModelConfiguration(bot)
    assert bot.provider is None and config.snapshot()["load_error"]
    assert "secret" not in str(config.snapshot())


@pytest.mark.parametrize("thinking", [None, False, True])
async def test_thinking_setting_controls_test_and_actual_answers_and_persists(tmp_path, thinking):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return completion()

    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(respond)
        response = await state.client.post("/api/manage/chat-model", json={**CONFIG, "enable_thinking": thinking})
        assert response.json()["saved"]
        assert response.json()["settings"]["enable_thinking"] is thinking
        await state.submit(event())
        assert len(calls) == 2
        for payload in calls:
            if thinking is None:
                assert "enable_thinking" not in payload
            else:
                assert payload["enable_thinking"] is thinking
        restarted = ChatModelConfiguration(MentionChatService(ChatbotOptions()))
        assert restarted.settings.enable_thinking is thinking
        assert restarted.snapshot()["enable_thinking"] is thinking


async def test_unsupported_thinking_option_failure_preserves_working_configuration(tmp_path):
    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(lambda request: completion())
        await state.client.post("/api/manage/chat-model", json=CONFIG)
        previous = config.path.read_bytes()
        config.transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "unsupported parameter"}))
        response = await state.client.post("/api/manage/chat-model", json={**CONFIG, "enable_thinking": False})
        assert not response.json()["saved"] and config.path.read_bytes() == previous
        assert config.settings.enable_thinking is None
