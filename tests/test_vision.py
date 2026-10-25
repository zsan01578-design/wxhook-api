from pathlib import Path

from PIL import Image

from tests.test_chatbot import BOT, context, event


class VisionAnswers:
    supports_vision = True
    request_timeout = 30

    def __init__(self):
        self.calls = []

    async def answer(self, question, context=None, *, system=None, conversation=None, image_data_url=None, **kwargs):
        history = conversation.history() if conversation is not None else []
        self.calls.append({"question": question, "image": image_data_url, "history": history})
        return "从照片看，你的动作比较标准，继续保持。"

    async def answer_with_search(self, question, search_service, *, history=None, conversation=None, image_data_url=None):
        return await self.answer(question, conversation=conversation, image_data_url=image_data_url)


def make_image(path: Path):
    image = Image.new("RGB", (640, 480), "white")
    image.save(path, format="PNG")
    return path


async def test_image_with_mention_is_analyzed_and_committed_to_context(tmp_path):
    image_path = make_image(tmp_path / "exercise.png")
    provider = VisionAnswers()
    async with context(tmp_path, provider=provider, image_path=image_path) as state:
        await state.submit(event(type=3, content="", msgid=101, atlist=[BOT]))
        assert len(state.sends) == 1 and "动作" in state.sends[0]["msg"]
        assert provider.calls[0]["image"].startswith("data:image/jpeg;base64,")
        assert provider.calls[0]["question"] == "请描述这张图片的内容，并指出值得注意的细节。"
        # The first answer is ordinary text context; no image bytes are stored.
        await state.submit(event("@机器人\u2005我厉不厉害", msgid=102))
        assert len(provider.calls) == 2
        assert provider.calls[1]["image"].startswith("data:image/jpeg;base64,")
        assert any(item["role"] == "assistant" and "动作" in item["content"] for item in provider.calls[1]["history"])


async def test_unmentioned_image_is_remembered_for_the_same_member_only(tmp_path):
    image_path = make_image(tmp_path / "exercise.png")
    provider = VisionAnswers()
    async with context(tmp_path, provider=provider, image_path=image_path) as state:
        await state.submit(event(type=3, content="", msgid=201))
        assert not provider.calls and not state.sends
        await state.submit(event("@机器人\u2005看看这张图", msgid=202))
        assert len(provider.calls) == 1 and provider.calls[0]["image"]
        await state.submit(event("@机器人\u2005你好", msgid=203))
        assert len(provider.calls) == 2 and provider.calls[1]["image"] is None
        # Another member cannot attach the first member's image.
        await state.submit(event("@机器人\u2005看看这张图", msgid=204, sender="wxid_other"))
        assert len(provider.calls) == 3 and provider.calls[2]["image"] is None


async def test_image_failure_is_explained_without_calling_the_model(tmp_path):
    provider = VisionAnswers()
    async with context(tmp_path, provider=provider, image_path=tmp_path / "missing.png") as state:
        await state.submit(event(type=3, content="", msgid=301, atlist=[BOT]))
        assert not provider.calls and len(state.sends) == 1
        assert "图片" in state.sends[0]["msg"]
