import json

import httpx
import pytest

from tests.test_chatbot import Answers, BOT, MEMBER, context, event
from tests.test_douyin import GROUP
from tests.test_image_model import add, confirmed, image_response
from wxhook_api.services.chat_model import ChatCompletionsProvider, ModelError, ModelSettings
from wxhook_api.services.image_intent import ImageIntent, ImagePromptMemory, parse_image_intent


class SemanticAnswers(Answers):
    def __init__(self, *intents):
        super().__init__()
        self.intents = list(intents)
        self.extractions = []

    async def extract_image_intent(self, question, *, context):
        self.extractions.append((question, context()))
        result = self.intents.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def completion(payload):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})


@pytest.mark.parametrize("payload", [
    {"action": "generate", "prompt": "", "message": ""},
    {"action": "chat", "prompt": "a cat", "message": ""},
    {"action": "clarify", "prompt": "", "message": ""},
    {"action": "generate", "prompt": "a" * 8001, "message": ""},
    {"action": "run_tool", "prompt": "a cat", "message": ""},
    {"action": [], "prompt": "a cat", "message": ""},
    {"action": "generate", "prompt": "a cat", "message": "", "extra": "ignore validation"},
])
def test_intent_output_requires_consistent_bounded_schema(payload):
    with pytest.raises(ValueError):
        parse_image_intent(json.dumps(payload))


def test_intent_parser_accepts_json_fences_and_memory_is_member_owned_expiring_and_bounded():
    assert parse_image_intent('```json\n{"action":"generate","prompt":" a cat ","message":""}\n```') == ImageIntent("generate", "a cat")
    clock = [0]
    memory = ImagePromptMemory(now=lambda: clock[0])
    memory.remember(BOT, GROUP, MEMBER, "cat")
    assert memory.get(BOT, GROUP, MEMBER) == "cat"
    assert memory.get(BOT, GROUP, "other") == memory.get("other", GROUP, MEMBER) == memory.get(BOT, "other", MEMBER) == ""
    memory.remember(BOT, GROUP, "", "unknown must not share context")
    assert memory.get(BOT, GROUP, "") == ""
    clock[0] = 1800
    assert memory.get(BOT, GROUP, MEMBER) == ""
    for index in range(101):
        memory.remember(BOT, GROUP, str(index), "cat")
    assert memory.get(BOT, GROUP, "0") == "" and len(memory._items) == 100


async def test_real_provider_extracts_json_refreshes_context_and_uses_no_search(tmp_path):
    state = {"questions": ["outdated"], "image_prompt": "old"}
    requests = []
    async def verify():
        state.update(questions=["a white cat"], image_prompt="white cat, blue background")
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        data = json.loads(payload["messages"][-1]["content"])
        assert data == {"current_message": "把它换成水彩风格", "same_member_recent_questions": ["a white cat"], "last_image_prompt": "white cat, blue background"}
        assert "否定" in payload["messages"][0]["content"] and "不可信" in payload["messages"][0]["content"]
        return completion({"action": "generate", "prompt": "white cat, blue background, watercolor", "message": ""})
    provider = ChatCompletionsProvider(ModelSettings(base_url="https://chat.test/v1", model="intent"),
        transport=httpx.MockTransport(respond), account_check=verify)
    result = await provider.extract_image_intent("把它换成水彩风格", context=lambda: state)
    assert result == ImageIntent("generate", "white cat, blue background, watercolor") and len(requests) == 1


async def test_real_provider_rejects_malformed_intent_without_guessing_generation(tmp_path):
    provider = ChatCompletionsProvider(ModelSettings(base_url="https://chat.test/v1", model="intent"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "I can generate that"}}]})))
    with pytest.raises(ModelError, match="无法确认"):
        await provider.extract_image_intent("来张海报", context=lambda: {})


async def test_complex_request_sends_extracted_description_to_images_api(tmp_path):
    prompt = "咖啡馆开业海报，蓝底金字，标题原文：明天开业，简洁风格，竖版3:4，无人物"
    provider = SemanticAnswers(ImageIntent("generate", prompt))
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return image_response()
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(respond)
        await add(state)
        requests.clear()
        await state.submit(event("@机器人\u2005咖啡馆明天开业，想要一张海报，蓝底金字写“明天开业”，不要人物，竖版，简洁点"))
        assert [request["prompt"] for request in requests] == [prompt]
        assert len(state.image_sends) == 1 and not state.sends and not provider.questions
        assert state.bot.snapshot()["recent_requests"][0]["image_intent_source"] == "semantic"
        assert state.bot.image_prompts.get(BOT, GROUP, MEMBER) == prompt


@pytest.mark.parametrize("question", [
    "不要生图，只教我怎么写提示词", "你支持生图吗", "解释这句话：生成一张小猫的图片", "如何生成一张海报",
])
async def test_negative_capability_quoted_and_tutorial_queries_do_not_generate(tmp_path, question):
    provider = SemanticAnswers(ImageIntent("chat"))
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005" + question))
        assert not state.image_sends and provider.questions == [question] and len(state.sends) == 1


@pytest.mark.parametrize("question", ["不要生图，只教我写提示词", "生图功能怎么用", "生成图片需要收费吗"])
async def test_without_semantic_provider_ambiguous_questions_do_not_generate(tmp_path, question):
    provider = Answers()
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005" + question))
        assert not state.image_sends and provider.questions == [question]


async def test_explicit_image_command_stays_available_without_intent_call(tmp_path):
    provider = SemanticAnswers()  # Any attempted extraction would fail this test.
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005生图：蓝色背景的猫，不要文字"))
        assert len(state.image_sends) == 1 and not provider.extractions and not provider.questions


@pytest.mark.parametrize("question", ["生成一张小猫的图片", "画一只猫", "帮我画个图"])
async def test_simple_natural_requests_keep_working_without_extra_semantic_call(tmp_path, question):
    provider = SemanticAnswers()
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005" + question))
        assert len(state.image_sends) == 1 and not provider.extractions and not provider.questions


@pytest.mark.parametrize("followup, prompt", [("把背景换成蓝色，主体不变", "red cat, blue background"),
                                             ("太暗了，亮一点，别改主体", "red cat, garden, brighter")])
async def test_same_member_image_followup_and_other_member_context_isolation(tmp_path, followup, prompt):
    provider = SemanticAnswers(ImageIntent("generate", "red cat, garden"), ImageIntent("generate", prompt),
                               ImageIntent("clarify", message="请重新 @并说明要画什么。"))
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005想要一个花园小猫头像"))
        await state.submit(event("@机器人\u2005" + followup, msgid=2))
        assert provider.extractions[1][1]["image_prompt"] == "red cat, garden"
        await state.submit(event("@机器人\u2005照刚才那张来一张", msgid=3, sender="other_member"))
        assert provider.extractions[2][1] == {"questions": [], "image_prompt": ""}
        assert len(state.image_sends) == 2 and len(state.sends) == 1
        await state.submit(event("@机器人\u2005清空上下文", msgid=4))
        assert state.bot.image_prompts.get(BOT, GROUP, MEMBER) == ""


async def test_previous_image_does_not_authorize_a_new_image_for_unrelated_question(tmp_path):
    provider = SemanticAnswers(ImageIntent("generate", "cat portrait"), ImageIntent("chat"))
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005给我做个猫咪头像"))
        await state.submit(event("@机器人\u2005今天天气怎么样", msgid=2))
        assert provider.extractions[1][1]["image_prompt"] == "cat portrait"
        assert len(state.image_sends) == 1 and provider.questions == ["今天天气怎么样"]


async def test_semantic_context_contains_only_same_member_questions(tmp_path):
    provider = SemanticAnswers(ImageIntent("generate", "orange-white cat portrait"))
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005我家的猫是橘白色"))
        await state.submit(event("@机器人\u2005我家的狗是黑色", msgid=2, sender="other_member"))
        await state.submit(event("@机器人\u2005用我刚才说的猫做个头像", msgid=3))
        assert provider.extractions[0][1]["questions"] == ["我家的猫是橘白色"]


@pytest.mark.parametrize("result", [ImageIntent("clarify", message="请重新 @并说明图片主体和风格。"), TimeoutError("secret upstream")])
async def test_unclear_or_failed_intent_never_generates_or_falls_back_to_chat(tmp_path, result):
    provider = SemanticAnswers(result)
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005帮我处理一下这张图像"))
        assert len(state.sends) == 1 and not state.image_sends and not provider.questions
        assert "secret" not in state.sends[0]["msg"]


async def test_failed_image_delivery_is_not_remembered_as_followup_context(tmp_path):
    provider = SemanticAnswers(ImageIntent("generate", "cat portrait"))
    async def fail(request):
        return httpx.Response(503, json={"ret": -1})
    async with context(tmp_path, provider=provider, image_hook=fail) as state:
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add(state)
        await state.submit(event("@机器人\u2005给我来个猫咪头像"))
        assert state.bot.image_prompts.get(BOT, GROUP, MEMBER) == ""


async def test_policy_change_after_intent_blocks_image_api(tmp_path):
    provider = SemanticAnswers()
    requests = []
    async with context(tmp_path, provider=provider, image_hook=confirmed) as state:
        def respond(request):
            requests.append(request)
            return image_response()
        state.app.state.image_model.transport = httpx.MockTransport(respond)
        await add(state)
        async def extract(question, *, context):
            await state.app.state.groups.update_group(GROUP, {"blocked": True})
            return ImageIntent("generate", "cat portrait")
        provider.extract_image_intent = extract
        await state.submit(event("@机器人\u2005给我来个猫咪头像"))
        assert len(requests) == 1 and not state.image_sends and not state.sends


async def test_real_chat_and_image_providers_share_budget_for_semantic_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("WXHOOK_AI_DAILY_LIMIT", "3")
    image_requests, chat_requests = [], []
    async with context(tmp_path, image_hook=confirmed) as state:
        def chat(request):
            body = json.loads(request.content)
            chat_requests.append(body)
            system = body["messages"][0]["content"] if body["messages"][0]["role"] == "system" else ""
            if "生图意图路由器" in system:
                return completion({"action": "generate", "prompt": "cat logo", "message": ""})
            return httpx.Response(200, json={"choices": [{"message": {"content": "模型测试通过"}}]})
        def image(request):
            image_requests.append(request)
            return image_response()
        state.app.state.chat_model.transport = httpx.MockTransport(chat)
        state.app.state.image_model.transport = httpx.MockTransport(image)
        result = await state.client.post("/api/manage/chat-models", json={"name": "语义模型", "base_url": "https://chat.test/v1", "model": "intent"})
        assert result.json()["saved"]
        await add(state)
        await state.submit(event("@机器人\u2005帮我设计个猫咪 logo，颜色用暖色"))
        # Two profile tests and one intent extraction exhaust the shared limit.
        # The image API must not be contacted after that extraction.
        assert len(chat_requests) == 2 and len(image_requests) == 1
        assert not state.image_sends and "额度" in state.sends[0]["msg"]
