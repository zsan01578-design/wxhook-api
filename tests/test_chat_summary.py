from datetime import datetime, timedelta, timezone
import json

import httpx
from asgi_lifespan import LifespanManager

from tests.test_manage import make_service
from tests.test_douyin import GROUP
from wxhook_api.main import create_app
from wxhook_api.services.chat_summary import CHINA, ChatSummaryService, is_summary_request, local_date
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.core.chatbot_config import ChatbotOptions


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
    app = create_app(douyin_service=make_service(tmp_path))
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
    app = create_app(douyin_service=make_service(tmp_path))
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


async def test_mention_summary_uses_saved_group_summary(tmp_path):
    class Summary:
        async def get(self, group):
            assert group == GROUP
            return {"summary": "已保存的今日总结"}

    bot = MentionChatService(ChatbotOptions(True, "bot", ("机器人",)))
    bot.summary_service = Summary()
    assert await bot._answer("总结", GROUP) == "已保存的今日总结"
    assert await bot._answer("今日总结", GROUP) == "已保存的今日总结"
