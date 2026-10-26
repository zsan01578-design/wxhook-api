import asyncio
from datetime import datetime
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tests.test_manage import make_service
from tests.test_douyin import GROUP, profile_transport
from wxhook_api.main import create_app
from wxhook_api.services.chat_summary import CHINA, is_summary_request, local_date
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.services.chat_model import ModelError
from tests.test_chatbot import context
from tests.test_group_chat_context import OTHER, enable_other, question


def today_timestamp(hour=10):
    return int(datetime.now(CHINA).replace(hour=hour, minute=0, second=0, microsecond=0).timestamp())


def completion(request):
    body = json.loads(request.content)
    prompt = body["messages"][0]["content"]
    text = "今日总结：\n- 讨论了项目进展。" if "聊天记录" in prompt else "模型连接正常。"
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_summary_request_variants():
    assert all(is_summary_request(value) for value in ("总结", "今日总结", "今天聊天总结！"))
    assert not is_summary_request("总结一下模型")


async def test_summary_refresh_saves_today_and_reuses_until_forced(tmp_path):
    app = create_app(douyin_service=make_service(tmp_path), hook_transport=profile_transport())
    app.state.chat_model.transport = httpx.MockTransport(completion)
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await app.state.groups.import_history(GROUP, [
            {"msgid": 1, "timestamp": today_timestamp(), "type": 1, "sender": "member", "content": "今天讨论项目"},
            {"msgid": 2, "timestamp": today_timestamp(11), "type": 1, "sender": "member", "content": "决定明天测试"},
        ])
        config = await client.post("/api/manage/chat-model", json={"base_url": "https://model.test/v1", "model": "summary", "api_key": "key"})
        assert config.json()["saved"]
        first = await client.post(f"/api/manage/groups/{GROUP}/summary")
        assert first.status_code == 200 and first.json()["summary"]["message_count"] == 2
        second = await client.get(f"/api/manage/groups/{GROUP}/summary")
        assert second.json()["summary"]["summary"] == "今日总结：\n- 讨论了项目进展。"
        forced = await client.post(f"/api/manage/groups/{GROUP}/summary")
        assert forced.json()["summary"]["summary"] == second.json()["summary"]["summary"]


async def test_summary_without_model_and_without_messages_are_safe(tmp_path):
    app = create_app(douyin_service=make_service(tmp_path), hook_transport=profile_transport())
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        empty = await client.post(f"/api/manage/groups/{GROUP}/summary")
        assert empty.status_code == 200 and empty.json()["summary"]["message_count"] == 0
        await app.state.groups.import_history(GROUP, [{"msgid": 1, "timestamp": today_timestamp(), "type": 1, "sender": "member", "content": "有一条消息"}])
        no_model = await client.post(f"/api/manage/groups/{GROUP}/summary")
        assert no_model.status_code == 409 and "模型尚未配置" in no_model.json()["error"]["message"]


async def test_summary_excludes_robot_and_only_uses_china_today(tmp_path):
    manager = GroupManager(tmp_path / "summary.sqlite3")
    await manager.start([])
    await manager.import_history(GROUP, [
        {"msgid": 1, "timestamp": today_timestamp(), "type": 1, "sender": "member", "content": "成员消息"},
        {"msgid": 2, "timestamp": today_timestamp(), "type": 1, "sender": "robot", "content": "机器人消息"},
        {"msgid": 3, "timestamp": today_timestamp() - 86400, "type": 1, "sender": "member", "content": "昨天消息"},
    ])
    await manager.claim_account("robot")
    rows = await manager.summary_messages(GROUP, local_date())
    assert len(rows) == 1 and rows[0]["content"] == "成员消息"


async def test_mention_summary_refreshes_only_the_requested_group(tmp_path):
    class Summary:
        async def get(self, group, *, force=False):
            assert group == GROUP and force
            return {"group_id": group, "summary": "当前群最新总结"}

    bot = MentionChatService(ChatbotOptions(True, "bot", ("机器人",)))
    bot.summary_service = Summary()
    assert await bot._answer("总结", GROUP) == "当前群最新总结"
    assert await bot._answer("今日总结", GROUP) == "当前群最新总结"


async def prepare_group_summaries(state, calls, *, pause=None):
    async def respond(request):
        payload = json.loads(request.content)
        if "唯一目标群" not in payload["messages"][0]["content"]:
            return httpx.Response(200, json={"choices": [{"message": {"content": "连接正常"}}]})
        calls.append(payload)
        records = json.loads(payload["messages"][-1]["content"])
        group = records[0]["group_id"]
        assert all(record["group_id"] == group for record in records)
        assert group in payload["messages"][0]["content"] and local_date() in payload["messages"][0]["content"]
        assert not any(message["role"] == "assistant" for message in payload["messages"])
        if pause is not None:
            await pause(group)
        marker = "A群独有话题" if group == GROUP else "B群独有话题"
        assert any(marker in record["content"] for record in records)
        other_marker = "B群独有话题" if group == GROUP else "A群独有话题"
        assert not any(other_marker in record["content"] for record in records)
        return httpx.Response(200, json={"choices": [{"message": {"content": marker + "的最新总结"}}]})

    await enable_other(state)
    state.app.state.chat_model.transport = httpx.MockTransport(respond)
    response = await state.client.post("/api/manage/chat-model", json={"base_url": "https://summary.test/v1", "model": "summary"})
    assert response.json()["saved"]
    for group, marker in ((GROUP, "A群独有话题"), (OTHER, "B群独有话题")):
        await state.app.state.groups.import_history(group, [
            {"msgid": 10, "timestamp": today_timestamp(), "type": 1, "sender": "member", "content": marker},
        ])


async def test_mention_replaces_wrong_cached_text_using_only_current_group_records(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await prepare_group_summaries(state, calls)
        groups = state.app.state.groups
        # Reproduce normal A messages alongside an incorrect old daily summary.
        await groups.save_summary(GROUP, local_date(), "B群独有话题的旧总结", 1)
        await groups.save_summary(OTHER, local_date(), "B群原有缓存", 1)
        state.bot.search_service = AsyncMock()
        await state.submit(question(GROUP, "总结", 101))
        assert state.sends[-1] == {"wxidorgid": GROUP, "msg": "A群独有话题的最新总结"}
        assert (await groups.get_summary(GROUP, local_date()))["summary"] == "A群独有话题的最新总结"
        assert (await groups.get_summary(OTHER, local_date()))["summary"] == "B群原有缓存"
        await state.submit(question(OTHER, "总结", 101))
        await state.submit(question(GROUP, "今日总结", 102))
        assert [send["wxidorgid"] for send in state.sends] == [GROUP, OTHER, GROUP]
        assert [send["msg"] for send in state.sends] == ["A群独有话题的最新总结", "B群独有话题的最新总结", "A群独有话题的最新总结"]
        assert len(calls) == 3
        state.bot.search_service.context.assert_not_called()
        for group in (GROUP, OTHER):
            assert state.bot.conversations.history(state.bot.identity, group, state.bot.provider) == []


async def test_concurrent_group_summaries_keep_source_cache_and_send_target_together(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def pause(group):
        if group == GROUP:
            entered.set()
            await release.wait()

    async with context(tmp_path) as state:
        await prepare_group_summaries(state, calls, pause=pause)
        try:
            await state.client.post("/receiveChatBotMsg", json=question(GROUP, "总结", 101))
            await asyncio.wait_for(entered.wait(), 3)
            await state.client.post("/receiveChatBotMsg", json=question(OTHER, "总结", 101))
            await asyncio.wait_for(state.app.state.event_dispatcher._queue.join(), 3)
            release.set()
            await asyncio.wait_for(state.bot._queue.join(), 3)
            assert len(calls) == len(state.sends) == 2
            by_group = {send["wxidorgid"]: send["msg"] for send in state.sends}
            assert by_group == {GROUP: "A群独有话题的最新总结", OTHER: "B群独有话题的最新总结"}
            for group in (GROUP, OTHER):
                saved = await state.app.state.groups.get_summary(group, local_date())
                assert saved["group_id"] == group and saved["summary"] == by_group[group]
        finally:
            release.set()


@pytest.mark.parametrize("stage", ["cache", "records", "result"])
async def test_summary_rejects_mismatched_group_before_exposing_it(tmp_path, monkeypatch, stage):
    calls = []
    async with context(tmp_path) as state:
        await prepare_group_summaries(state, calls)
        groups = state.app.state.groups
        if stage == "cache":
            monkeypatch.setattr(groups, "get_summary", AsyncMock(return_value={
                "group_id": OTHER, "summary_date": local_date(), "message_count": 1, "summary": "B群内容",
            }))
            with pytest.raises(ModelError, match="当前群"):
                await state.app.state.chat_summary.get(GROUP)
        elif stage == "records":
            monkeypatch.setattr(groups, "summary_messages", AsyncMock(return_value=[{
                "group_id": OTHER, "sender": "member", "timestamp": today_timestamp(), "content": "B群内容",
            }]))
            with pytest.raises(ModelError, match="当前群"):
                await state.app.state.chat_summary.get(GROUP, force=True)
        else:
            monkeypatch.setattr(state.app.state.chat_summary, "get", AsyncMock(return_value={"group_id": OTHER, "summary": "B群内容"}))
            await state.submit(question(GROUP, "总结", 101))
            assert "B群内容" not in state.sends[-1]["msg"] and "当前群不一致" in state.sends[-1]["msg"]
        assert not calls
