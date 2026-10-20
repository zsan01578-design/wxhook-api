import pytest

from tests.test_chatbot import BOT, MEMBER, Answers, context, event
from tests.test_douyin import GROUP, SHORT
from wxhook_api.core.chatbot_config import ChatbotOptions


@pytest.mark.parametrize("sender", ["", GROUP])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_prefixed_self_shares_are_ignored_after_identity_discovery(tmp_path, sender, newline, enabled):
    async with context(tmp_path, bot_options=ChatbotOptions(enabled, "", ())) as state:
        await state.submit(event(f"{BOT}:{newline}{SHORT}", sender=sender))
        assert state.bot.identity == BOT
        assert not state.media.snapshot()["jobs"] and not state.media_calls
        await state.submit(event(f"{BOT}:{newline}{SHORT}", 2, sender=MEMBER))
        assert len(state.media.snapshot()["jobs"]) == 1


@pytest.mark.parametrize("sender", ["", GROUP])
async def test_prefixed_self_mention_does_not_call_model(tmp_path, sender):
    answers = Answers()
    async with context(tmp_path, provider=answers) as state:
        await state.submit(event(f"{BOT}:\n@机器人\u2005你好", sender=sender))
        assert not answers.questions and not state.sends


@pytest.mark.parametrize("deadline", [
    {"due_at": "9999-12-31T23:59:59-23:59"},
    {"due_at": "0001-01-01T00:00:00+23:59"},
    {"due_at": "0001-01-01T00:00:00"},
])
async def test_unrepresentable_task_deadlines_are_validation_errors(tmp_path, deadline):
    async with context(tmp_path) as state:
        url = f"/api/manage/groups/{GROUP}/tools/tasks"
        task_id = (await state.client.post(url, json={"title": "original"})).json()["id"]
        for path, method in ((url, "POST"), (f"{url}/{task_id}", "PUT")):
            response = await state.client.request(method, path, json={"title": "invalid", **deadline})
            assert response.status_code == 422
        tasks = (await state.app.state.group_tools.snapshot(GROUP))["tasks"]
        assert len(tasks) == 1 and tasks[0]["title"] == "original" and tasks[0]["due_at"] is None
