"""Ordered provider failover; all endpoints and generated images are local mocks."""
import json

import httpx
import pytest

from tests.test_chatbot import context, event
from tests.test_chat_model import completion
from tests.test_image_model import image_response
from wxhook_api.core.ai_safety import ai_permission
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.chat_context import ConversationView
from wxhook_api.services.chat_model import ChatModelConfiguration, ModelError
from wxhook_api.services.image_model import ImageModelConfiguration


async def configure(state, kind):
    config = getattr(state.app.state, f"{kind}_model")
    config.transport = httpx.MockTransport(lambda request: image_response() if kind == "image" else completion())
    ids = []
    for name in ("first", "second", "third"):
        response = await state.client.post(f"/api/manage/{kind}-models", json={
            "name": name, "model": name, "base_url": f"https://{name}.test/v1", "api_key": f"{name}-private-key"})
        assert response.json()["saved"], response.text
        ids.append(response.json()["profile"]["id"])
    response = await state.client.put(f"/api/manage/{kind}-model/fallbacks", json={"fallback_ids": ids[1:]})
    assert response.json()["saved"], response.text
    return config, ids


async def invoke(config, kind):
    return await config.generate("a cat") if kind == "image" else await config.answer("你好")


@pytest.mark.parametrize("kind", ["chat", "image"])
async def test_order_credentials_stop_on_success_and_restart(tmp_path, kind):
    async with context(tmp_path) as state:
        config, ids = await configure(state, kind)
        calls = []
        def respond(request):
            name = json.loads(request.content)["model"]
            calls.append(name)
            assert request.url.host == f"{name}.test"
            assert request.headers["authorization"] == f"Bearer {name}-private-key"
            return httpx.Response(402) if name == "first" else image_response() if kind == "image" else completion("备用成功")
        config.transport = httpx.MockTransport(respond)
        await invoke(config, kind)
        await invoke(config, kind)
        assert calls == ["first", "second", "first", "second"]
        snap = config.snapshot()
        assert snap["active_id"] == ids[0] and snap["last_route"]["used_id"] == ids[1]
        assert [a["status"] for a in snap["last_route"]["attempts"]] == ["failed", "success"]
        assert "private-key" not in json.dumps(snap)
        reloaded = (ImageModelConfiguration(state.bot, budget=config.budget) if kind == "image"
                    else ChatModelConfiguration(MentionChatService(ChatbotOptions())))
        assert reloaded.fallback_ids == ids[1:]
        assert reloaded.active_id == ids[0]
        # Reordering is persisted without a model test or extra billed call.
        changed = await state.client.put(f"/api/manage/{kind}-model/fallbacks", json={"fallback_ids": ids[:0:-1]})
        assert changed.json()["saved"]
        await invoke(config, kind)
        assert calls[-2:] == ["first", "third"]
        deleted = await state.client.delete(f"/api/manage/{kind}-models/{ids[2]}")
        assert deleted.json()["settings"]["fallback_ids"] == [ids[1]]
        switched = await state.client.post(f"/api/manage/{kind}-models/{ids[1]}/activate", json={})
        assert switched.json()["settings"]["fallback_ids"] == []


@pytest.mark.parametrize("kind", ["chat", "image"])
async def test_invalid_order_and_disk_failure_leave_configuration_unchanged(tmp_path, monkeypatch, kind):
    async with context(tmp_path) as state:
        config, ids = await configure(state, kind)
        before = config.path.read_bytes()
        for invalid in ([ids[0]], [ids[1], ids[1]], ["missing"]):
            response = await state.client.put(f"/api/manage/{kind}-model/fallbacks", json={"fallback_ids": invalid})
            assert not response.json()["saved"]
            assert config.path.read_bytes() == before
        response = await state.client.put(f"/api/manage/{kind}-model/fallbacks", json={"fallback_ids": ["x"] * 5})
        assert response.status_code == 422
        def fail(collection):
            raise PermissionError("secret")
        monkeypatch.setattr(config, "_save", fail)
        response = await state.client.put(f"/api/manage/{kind}-model/fallbacks", json={"fallback_ids": []})
        assert response.status_code == 500 and "secret" not in response.text
        assert config.path.read_bytes() == before and config.fallback_ids == ids[1:]


@pytest.mark.parametrize("kind", ["chat", "image"])
@pytest.mark.parametrize("status", [401, 402, 403, 404, 429, 503])
async def test_exhausted_chain_is_bounded_and_safe(tmp_path, kind, status):
    async with context(tmp_path) as state:
        config, ids = await configure(state, kind)
        calls = []
        def respond(request):
            calls.append(request.url.host)
            return httpx.Response(status, text="private-key upstream secret")
        config.transport = httpx.MockTransport(respond)
        with pytest.raises(ModelError, match="3 个模型配置均不可用"):
            await invoke(config, kind)
        assert calls == ["first.test", "second.test", "third.test"]
        assert config.last_route["used_id"] == ""
        assert "secret" not in json.dumps(config.last_route)


@pytest.mark.parametrize("kind", ["chat", "image"])
@pytest.mark.parametrize("code,expected", [("insufficient_quota", 2), ("content_policy_violation", 1), ("invalid_parameter", 1)])
async def test_http_400_quota_codes_only(tmp_path, kind, code, expected):
    async with context(tmp_path) as state:
        config, _ = await configure(state, kind)
        calls = []
        def respond(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(400, json={"error": {"code": code, "message": "secret"}})
            return image_response() if kind == "image" else completion()
        config.transport = httpx.MockTransport(respond)
        if expected == 1:
            with pytest.raises(ModelError):
                await invoke(config, kind)
        else:
            await invoke(config, kind)
        assert len(calls) == expected


@pytest.mark.parametrize("kind", ["chat", "image"])
@pytest.mark.parametrize("failure", ["budget", "permission", "account"])
async def test_local_limits_and_account_changes_never_bypass_to_backups(tmp_path, kind, failure):
    async with context(tmp_path) as state:
        config, _ = await configure(state, kind)
        calls, allowed = [], [True]
        async def account_check():
            if failure == "account":
                raise ModelError("账号已变化")
        config._verify_group_account = account_check
        def respond(request):
            calls.append(request)
            allowed[0] = False if failure == "permission" else True
            return httpx.Response(402)
        config.transport = httpx.MockTransport(respond)
        if failure == "budget":
            # The three successful configuration tests have consumed 3 attempts.
            config.budget.limits = (4, 100, 30)
        with ai_permission(lambda: allowed[0]):
            with pytest.raises(ModelError):
                await invoke(config, kind)
        assert len(calls) == (0 if failure == "account" else 1)


@pytest.mark.parametrize("failure", ["read_timeout", "invalid_image", "download_connect"])
async def test_accepted_image_is_not_generated_again(tmp_path, monkeypatch, failure):
    async with context(tmp_path) as state:
        config, _ = await configure(state, "image")
        calls = []
        async def safe_url(self, url):
            return url
        monkeypatch.setattr("wxhook_api.services.image_model.ImagesProvider._download_url", safe_url)
        def respond(request):
            calls.append(request)
            if failure == "read_timeout":
                raise httpx.ReadTimeout("secret")
            if failure == "invalid_image":
                return httpx.Response(200, json={"data": [{"b64_json": "bm90YW5pbWFnZQ=="}]})
            if request.method == "GET":
                assert "authorization" not in request.headers
                raise httpx.ConnectError("secret")
            return httpx.Response(200, json={"data": [{"url": "https://download.test/image.png"}]})
        config.transport = httpx.MockTransport(respond)
        with pytest.raises(ModelError):
            await config.generate("cat")
        assert len([r for r in calls if r.method == "POST"]) == 1


async def test_semantic_invalid_json_uses_next_model_and_group_reply_is_once(tmp_path):
    async with context(tmp_path) as state:
        config, ids = await configure(state, "chat")
        calls = []
        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            if body["model"] == "first":
                return completion("not-json")
            return completion(json.dumps({"action": "generate", "prompt": "a blue cat", "message": ""}))
        config.transport = httpx.MockTransport(respond)
        intent = await state.bot.provider.extract_image_intent("请做张海报，主体是一只蓝猫", context=lambda: {})
        assert intent.prompt == "a blue cat" and config.last_route["used_id"] == ids[1]
        assert len(calls) == 2
        config.transport = httpx.MockTransport(lambda r: httpx.Response(402) if r.url.host == "first.test" else completion("备用回复"))
        await state.submit(event())
        assert len(state.sends) == 1 and state.sends[0]["msg"] == "备用回复"


async def test_chat_timeout_fails_over_and_search_is_not_repeated(tmp_path):
    async with context(tmp_path) as state:
        config, _ = await configure(state, "chat")
        calls = []
        class Search:
            count = 0
            async def context(self, question):
                self.count += 1
                return "搜索内容"
        search = Search()
        def respond(request):
            calls.append(json.loads(request.content))
            if len(calls) == 1:
                raise httpx.ReadTimeout("secret")
            return completion("ok")
        config.transport = httpx.MockTransport(respond)
        assert await state.bot.provider.answer_with_search("问题", search) == "ok"
        assert len(calls) == 2 and search.count == 1
        assert all("搜索内容" in json.dumps(call, ensure_ascii=False) for call in calls)


@pytest.mark.parametrize("kind", ["chat", "image"])
async def test_primary_success_and_disabled_backups_never_call_other_profiles(tmp_path, kind):
    async with context(tmp_path) as state:
        config, _ = await configure(state, kind)
        calls = []
        def respond(request):
            calls.append(request.url.host)
            return image_response() if kind == "image" else completion()
        config.transport = httpx.MockTransport(respond)
        await invoke(config, kind)
        assert calls == ["first.test"]
        await config.set_fallbacks([])
        def fail(request):
            calls.append(request.url.host)
            return httpx.Response(402)
        config.transport = httpx.MockTransport(fail)
        with pytest.raises(ModelError, match="余额不足"):
            await invoke(config, kind)
        assert calls == ["first.test", "first.test"]


@pytest.mark.parametrize("action,message", [("chat", ""), ("clarify", "想画什么？")])
async def test_valid_semantic_non_generation_never_falls_through(tmp_path, action, message):
    async with context(tmp_path) as state:
        config, _ = await configure(state, "chat")
        calls = []
        def respond(request):
            calls.append(request)
            return completion(json.dumps({"action": action, "prompt": "", "message": message}))
        config.transport = httpx.MockTransport(respond)
        result = await state.bot.provider.extract_image_intent("不要生成图片", context=lambda: {})
        assert result.action == action and len(calls) == 1


async def test_history_is_refreshed_before_fallback_dispatch(tmp_path):
    async with context(tmp_path) as state:
        config, _ = await configure(state, "chat")
        allowed, payloads = [True], []
        history = [{"role": "user", "content": "private-old-message"}, {"role": "assistant", "content": "old-answer"}]
        conversation = ConversationView("member_current", lambda: history if allowed[0] else [])
        def respond(request):
            payloads.append(json.loads(request.content))
            if len(payloads) == 1:
                allowed[0] = False
                return httpx.Response(402)
            return completion("ok")
        config.transport = httpx.MockTransport(respond)
        assert await state.bot.provider.answer("current", conversation=conversation) == "ok"
        assert "private-old-message" in json.dumps(payloads[0])
        assert "private-old-message" not in json.dumps(payloads[1])


async def test_service_failure_skips_a_provider_for_a_short_cooldown(tmp_path):
    async with context(tmp_path) as state:
        config, ids = await configure(state, "chat")
        calls = []

        def respond(request):
            name = json.loads(request.content)["model"]
            calls.append(name)
            return httpx.Response(503) if name == "first" else completion("冷却后成功")

        config.transport = httpx.MockTransport(respond)
        await invoke(config, "chat")
        await invoke(config, "chat")
        assert calls == ["first", "second", "second"]
        assert ids[0] in config.snapshot()["cooldown"]
