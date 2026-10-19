import json

import httpx
import pytest

from tests.test_chatbot import context, event
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.chat_model import ChatModelConfiguration, ChatCompletionsProvider, ModelDefinition, ModelError, ModelSettings
from wxhook_api.core.secrets import reveal_secret


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
        actual = json.loads(calls[1].content)["messages"]
        assert actual[0]["role"] == "system"
        frame = json.loads(actual[-1]["content"])
        assert frame["question"] == "你好" and frame["participant"].startswith("member_")
        for endpoint in ("/api/manage/chat-model", "/api/manage/state"):
            result = await state.client.get(endpoint)
            assert "test-private-key" not in result.text
            assert result.headers["cache-control"] == "no-store"
        assert "test-private-key" not in response.text
        disk = json.loads(config.path.read_text(encoding="utf-8"))
        assert reveal_secret(disk["models"][0]["api_key"]) == "test-private-key"
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


async def test_multiple_models_edit_switch_restart_and_delete(tmp_path):
    calls = []

    def respond(request):
        calls.append((json.loads(request.content)["model"], request.headers.get("authorization")))
        return completion()

    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(respond)
        first = (await state.client.post("/api/manage/chat-models", json={**CONFIG, "name": "首选"})).json()["profile"]
        second = (await state.client.post("/api/manage/chat-models", json={**CONFIG, "model": "second", "api_key": "second-key", "enable_thinking": False})).json()["profile"]
        assert first["active"] and not second["active"]
        first_id, second_id = first["id"], second["id"]
        active_provider = state.bot.provider
        edited = await state.client.put(f"/api/manage/chat-models/{second_id}", json={**CONFIG, "model": "edited", "name": "备用", "api_key": None, "enable_thinking": False})
        assert edited.json()["saved"] and state.bot.provider is active_provider
        assert calls[-1] == ("edited", "Bearer second-key")
        await state.submit(event())
        assert calls[-1] == ("example/chat", "Bearer test-private-key")
        result = await state.client.post(f"/api/manage/chat-models/{second_id}/activate", json={})
        assert result.json()["saved"]
        snapshot = (await state.client.get("/api/manage/chat-model")).json()
        assert snapshot["active_id"] == second_id and sum(p["active"] for p in snapshot["models"]) == 1
        assert "second-key" not in str(snapshot) and "test-private-key" not in str(snapshot)
        await state.submit(event(msgid="new-model-message"))
        assert calls[-1] == ("edited", "Bearer second-key")
        restarted = ChatModelConfiguration(MentionChatService(ChatbotOptions()))
        assert restarted.settings.id == second_id and restarted.settings.enable_thinking is False
        assert len(restarted.models) == 2 and restarted.models[first_id].name == "首选"
        rejected = await state.client.delete(f"/api/manage/chat-models/{second_id}")
        assert not rejected.json()["saved"] and len(config.models) == 2
        removed = await state.client.delete(f"/api/manage/chat-models/{first_id}")
        assert removed.json()["saved"] and list(config.models) == [second_id]
        assert ChatModelConfiguration(MentionChatService(ChatbotOptions())).active_id == second_id


async def test_failed_switch_and_edit_leave_all_models_and_active_provider_unchanged(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(lambda request: completion())
        first = (await state.client.post("/api/manage/chat-models", json=CONFIG)).json()["profile"]
        second = (await state.client.post("/api/manage/chat-models", json={**CONFIG, "model": "second"})).json()["profile"]
        previous, provider = config.path.read_bytes(), state.bot.provider
        config.transport = httpx.MockTransport(lambda request: httpx.Response(401))
        for method, path, body in [
            ("POST", f"/api/manage/chat-models/{second['id']}/activate", {}),
            ("PUT", f"/api/manage/chat-models/{first['id']}", CONFIG),
            ("POST", "/api/manage/chat-models", CONFIG),
        ]:
            response = await state.client.request(method, path, json=body)
            assert not response.json()["saved"]
            assert config.path.read_bytes() == previous and state.bot.provider is provider
        config.transport = httpx.MockTransport(lambda request: completion())

        def fail(collection):
            raise PermissionError("second-key")

        monkeypatch.setattr(config, "_save", fail)
        response = await state.client.post(f"/api/manage/chat-models/{second['id']}/activate", json={})
        assert response.status_code == 500 and "second-key" not in response.text
        assert config.active_id == first["id"] and state.bot.provider is provider


async def test_new_profile_never_inherits_another_profiles_key_and_missing_ids_do_not_create(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return completion()

    async with context(tmp_path) as state:
        config = state.app.state.chat_model
        config.transport = httpx.MockTransport(respond)
        await state.client.post("/api/manage/chat-models", json=CONFIG)
        second = await state.client.post("/api/manage/chat-models", json={**CONFIG, "api_key": None})
        assert second.json()["saved"] and "authorization" not in calls[-1].headers
        for method, path, body in [("PUT", "/api/manage/chat-models/missing", CONFIG),
                                   ("POST", "/api/manage/chat-models/missing/activate", {}),
                                   ("DELETE", "/api/manage/chat-models/missing", None)]:
            response = await state.client.request(method, path, json=body)
            assert not response.json()["saved"]
        assert len(config.models) == 2 and len(calls) == 2
        invalid = await state.client.put(f"/api/manage/chat-models/{second.json()['profile']['id']}", json={**CONFIG, "api_key": "bad\nkey"})
        assert invalid.status_code == 422 and "bad" not in invalid.text


async def test_legacy_config_migrates_without_losing_key_or_thinking_setting(tmp_path):
    path = tmp_path / "chat-model.local.json"
    path.write_text(json.dumps({**CONFIG, "enable_thinking": False}), encoding="utf-8")
    bot = MentionChatService(ChatbotOptions())
    config = ChatModelConfiguration(bot)
    assert not config.load_error and config.active_id == "legacy"
    assert config.settings.api_key.get_secret_value() == CONFIG["api_key"]
    assert config.settings.enable_thinking is False and len(config.models) == 1
    config.transport = httpx.MockTransport(lambda request: completion())
    await config.save_profile(ModelDefinition(base_url="http://localhost:1234/v1", model="local"))
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["active_id"] == "legacy" and len(disk["models"]) == 2
    restored = ChatModelConfiguration(MentionChatService(ChatbotOptions()))
    assert restored.settings.api_key.get_secret_value() == CONFIG["api_key"]
