from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.test_chatbot import BOT, MEMBER, context, event
from tests.test_douyin import GROUP
from tests.test_image_model import add as add_image, confirmed, image_response
from wxhook_api.core.ai_safety import BudgetError, check_ai_permission, current_ai_group
from wxhook_api.services.chat_model import ModelError
from wxhook_api.services.chat_summary import CHINA
from wxhook_api.services.group_insights import InsightRequest, parse_command, window_bounds


OTHER = "other-insights@chatroom"


@pytest.mark.parametrize("command,kind,term,window,limit,after", [
    ("梗百科：周三战神", "meme", "周三战神", "今天", 200, None),
    ("“周三战神”是什么梗？", "meme", "周三战神", "今天", 200, None),
    ("爬楼", "catchup", "", "今天", 200, None),
    ("爬楼助手 最近两小时", "catchup", "", "最近两小时", 200, None),
    ("爬楼 从消息#123之后", "catchup", "", "最近7天", 200, 123),
    ("我今天没看群，发生啥了", "catchup", "", "今天", 200, None),
    ("聊天变漫画 最近30条", "comic", "", "最近7天", 30, None),
    ("四格漫画 最近半小时", "comic", "", "最近半小时", 50, None),
])
def test_commands(command, kind, term, window, limit, after):
    assert parse_command(command) == InsightRequest(kind, term, window, limit, after)


@pytest.mark.parametrize("command", ["梗百科", "爬楼 最近0小时", "爬楼 最近8天", "爬楼 最近201条",
                                      "聊天变漫画 最近101条", "爬楼 从消息9223372036854775808之后",
                                      "聊天变漫画 随便", "爬楼 最近半条"])
def test_invalid_commands_do_not_fall_through_to_ordinary_models(command):
    with pytest.raises(ModelError):
        parse_command(command)


def test_bounds_use_china_midnight_and_non_commands_pass_through():
    current = datetime(2026, 10, 26, 0, 30, tzinfo=CHINA)
    start, end = window_bounds("今天", current)
    assert datetime.fromtimestamp(start, CHINA).hour == 0
    assert end == int(current.timestamp()) + 1
    start, _ = window_bounds("最近两小时", current)
    assert datetime.fromtimestamp(start, CHINA) == current - timedelta(hours=2)
    assert parse_command("怎么写漫画脚本") is None


async def seed(groups, group=GROUP, *, current=None):
    current = current or int(datetime.now(CHINA).timestamp()) - 30
    await groups.import_history(group, [
        {"msgid": 10, "timestamp": current - 10, "type": 1, "sender": "alice", "content": "周三战神第一次出现，因为周三修好了故障"},
        {"msgid": 11, "timestamp": current, "type": 1, "sender": MEMBER, "content": "本群决定周五测试，尚未确定负责人"},
        {"msgid": 12, "timestamp": current, "type": 1, "sender": BOT, "content": "机器人不应入选"},
        {"msgid": 13, "timestamp": current, "type": 1, "sender": "flagged", "content": "自发消息不应入选", "isSend": True},
        {"msgid": 14, "timestamp": current, "type": 1, "sender": "blocked", "content": "黑名单消息不应入选"},
        {"msgid": 15, "timestamp": current, "type": 3, "sender": MEMBER, "content": "图片不应入选"},
    ])
    await groups.save_member(group, "blocked", {"blocked": True})


def configure_fake(worker, *, reply=None):
    calls = []

    async def answer(prompt, **kwargs):
        check_ai_permission()
        data = json.loads(prompt)
        assert current_ai_group() == GROUP
        assert all(row["group_id"] == GROUP for row in data["records"])
        calls.append((data, kwargs))
        value = {"text": "根据记录，本群计划周五测试。", "source_ids": [data["records"][0]["id"]],
                 "image_prompt": "四格分镜：角色A修复故障，角色B庆祝，一起测试，开心收尾。"}
        return json.dumps(reply if reply is not None else value, ensure_ascii=False)

    worker.models = SimpleNamespace(settings=True, answer=answer)
    return calls


async def test_source_query_filters_group_self_flags_blacklist_and_current_command(tmp_path):
    async with context(tmp_path) as state:
        groups, worker = state.app.state.groups, state.app.state.group_insights
        await seed(groups)
        await seed(groups, OTHER)
        calls = configure_fake(worker)
        result = await worker.generate(GROUP, InsightRequest("catchup", window="最近2小时"), exclude_msgid="11")
        assert result["message_count"] == result["sampled_count"] == 1
        assert [row["speaker"] for row in calls[0][0]["records"]] == ["alice"]
        assert result["sources"][0]["group_id"] == GROUP
        assert "未配置" not in result["text"]


async def test_meme_uses_earliest_archive_with_context_not_today_only(tmp_path):
    async with context(tmp_path) as state:
        groups, worker = state.app.state.groups, state.app.state.group_insights
        await seed(groups, current=int(datetime.now(CHINA).timestamp()) - 86400 * 20)
        calls = configure_fake(worker)
        result = await worker.generate(GROUP, InsightRequest("meme", term="周三战神"))
        assert result["message_count"] == 1
        assert any("周三战神" in row["content"] for row in calls[0][0]["records"])
        assert "真正起源" in calls[0][1]["system"]
        assert result["sources"]


async def test_catchup_after_anchor_uses_time_then_id_and_rejects_other_group_anchor(tmp_path):
    async with context(tmp_path) as state:
        groups, worker = state.app.state.groups, state.app.state.group_insights
        await seed(groups)
        await seed(groups, OTHER)
        first = await groups.read(lambda db: db.execute("SELECT id FROM chat_messages WHERE group_id=? AND msgid='10'", (GROUP,)).fetchone()[0])
        configure_fake(worker)
        result = await worker.generate(GROUP, InsightRequest("catchup", window="最近7天", after_id=first))
        assert result["message_count"] == 1
        assert result["sources"][0]["msgid"] == "11"
        with pytest.raises(ModelError, match="起始消息不属于"):
            await worker.generate(OTHER, InsightRequest("catchup", window="最近7天", after_id=first))


async def test_empty_history_and_unknown_meme_make_no_model_calls(tmp_path):
    async with context(tmp_path) as state:
        worker = state.app.state.group_insights
        worker.models = SimpleNamespace(settings=False, answer=AsyncMock())
        for request in (InsightRequest("catchup"), InsightRequest("comic", limit=50), InsightRequest("meme", term="不存在")):
            result = await worker.generate(GROUP, request)
            assert result["sampled_count"] == 0 and result["sources"] == []
        worker.models.answer.assert_not_called()


@pytest.mark.parametrize("reply", ["bad json", {"text": "", "source_ids": [1]},
    {"text": "fake citation", "source_ids": [99999]}, {"text": "no citations", "source_ids": []},
    {"text": "bool id", "source_ids": [True]}])
async def test_invalid_model_output_cannot_expose_unverified_sources(tmp_path, reply):
    async with context(tmp_path) as state:
        worker = state.app.state.group_insights
        await seed(state.app.state.groups)
        configure_fake(worker, reply=reply)
        with pytest.raises(ModelError):
            await worker.generate(GROUP, InsightRequest("catchup", window="最近7天"))


async def test_late_contributor_blacklist_blocks_output_even_if_not_cited(tmp_path):
    async with context(tmp_path) as state:
        groups, worker = state.app.state.groups, state.app.state.group_insights
        await seed(groups)
        calls = configure_fake(worker)
        answer = worker.models.answer

        async def blocking_answer(prompt, **kwargs):
            response = await answer(prompt, **kwargs)
            await groups.save_member(GROUP, MEMBER, {"blocked": True})
            return response

        worker.models.answer = blocking_answer
        with pytest.raises(BudgetError):
            await worker.generate(GROUP, InsightRequest("catchup", window="最近7天"))
        assert len(calls) == 1


async def test_management_api_validates_limits_ids_policies_and_sources(tmp_path):
    async with context(tmp_path) as state:
        groups, worker = state.app.state.groups, state.app.state.group_insights
        await seed(groups)
        configure_fake(worker)
        base = f"/api/manage/groups/{GROUP}/insights"
        result = await state.client.post(base + "/catchup", json={"window": "最近2小时"})
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json()["sources"] and "_contributors" not in result.json()
        assert not state.sends
        for payload in ({"limit": 201}, {"after_id": 2**63}, {"window": "最近8天"}, {"message_ids": [True]}):
            assert (await state.client.post(base + "/catchup", json=payload)).status_code == 422
        assert (await state.client.post(base + "/comic", json={"limit": 101})).status_code == 409
        assert (await state.client.post(base + "/meme", json={})).status_code == 409
        assert (await state.client.post(base + "/catchup", json={"message_ids": [99999]})).status_code == 409
        await groups.update_group(GROUP, {"enabled": False})
        assert (await state.client.post(base + "/catchup", json={})).status_code == 409


async def test_chat_lookup_has_sources_deduplicates_and_does_not_use_search_or_context(tmp_path):
    async with context(tmp_path) as state:
        worker = state.app.state.group_insights
        await seed(state.app.state.groups)
        calls = configure_fake(worker)
        state.bot.search_service = AsyncMock()
        payload = event("@机器人\u2005梗百科：周三战神", msgid=100)
        await state.submit(payload)
        await state.submit(payload)
        assert len(calls) == len(state.sends) == 1
        assert "来源原话" in state.sends[0]["msg"] and "[#" in state.sends[0]["msg"]
        assert all("梗百科：" not in row["content"] for row in calls[0][0]["records"])
        state.bot.search_service.context.assert_not_called()
        assert state.bot.conversations.history(BOT, GROUP, state.bot.provider) == []


async def test_comic_model_generation_and_one_image_delivery(tmp_path):
    async with context(tmp_path, image_hook=confirmed) as state:
        worker = state.app.state.group_insights
        await seed(state.app.state.groups)
        calls = configure_fake(worker)
        state.app.state.image_model.transport = httpx.MockTransport(lambda request: image_response())
        await add_image(state)
        payload = event("@机器人\u2005聊天变漫画 最近10条", msgid=100)
        await state.submit(payload)
        await state.submit(payload)
        assert len(calls) == len(state.image_sends) == 1 and not state.sends
        response = await state.client.post(f"/api/manage/groups/{GROUP}/insights/comic", json={"window": "最近2小时"})
        assert response.status_code == 200, response.text[:500]
        assert response.json()["image_data_url"].startswith("data:image/png;base64,")
        assert response.json()["text"] and response.json()["sources"]
        assert len(state.image_sends) == 1  # Web preview never sends to WeChat.


async def test_unknown_message_id_does_not_turn_the_question_itself_into_meme_evidence(tmp_path):
    async with context(tmp_path) as state:
        calls = configure_fake(state.app.state.group_insights)
        await state.submit(event("@机器人\u2005梗百科：从未出现的词", msgid=0))
        assert not calls
        assert len(state.sends) == 1 and "没有找到这个梗" in state.sends[0]["msg"]


async def test_contributor_blacklist_after_generation_still_blocks_send(tmp_path):
    async with context(tmp_path) as state:
        groups, worker = state.app.state.groups, state.app.state.group_insights
        await seed(groups)
        configure_fake(worker)
        original = worker.chat_command

        async def block_before_send(*args, **kwargs):
            result = await original(*args, **kwargs)
            await groups.save_member(GROUP, "alice", {"blocked": True})
            return result

        worker.chat_command = block_before_send
        await state.submit(event("@机器人\u2005爬楼 最近2小时", msgid=100))
        assert not state.sends


async def test_exact_source_selection_and_bounded_sampling(tmp_path):
    async with context(tmp_path) as state:
        groups, worker = state.app.state.groups, state.app.state.group_insights
        timestamp = int(datetime.now(CHINA).timestamp()) - 1000
        await groups.import_history(GROUP, [
            {"msgid": i + 10, "timestamp": timestamp + i, "type": 1, "sender": MEMBER, "content": f"长文本 {i}: " + "讨论内容" * 200}
            for i in range(200)
        ])
        calls = configure_fake(worker)
        result = await worker.generate(GROUP, InsightRequest("catchup", window="最近2小时"))
        assert result["message_count"] == 200 and 0 < result["sampled_count"] < 200
        records = calls[-1][0]["records"]
        assert "199:" in records[-1]["content"]  # Keep the most recent complete records.
        selected = [records[0]["id"], records[-1]["id"]]
        result = await worker.generate(GROUP, InsightRequest("catchup", window="最近2小时"), message_ids=selected)
        assert result["sampled_count"] == 2
        assert [row["id"] for row in calls[-1][0]["records"]] == selected


async def test_api_policy_change_returns_actionable_conflict(tmp_path):
    async with context(tmp_path) as state:
        groups, worker = state.app.state.groups, state.app.state.group_insights
        await seed(groups)
        configure_fake(worker)
        original = worker.models.answer

        async def pause(prompt, **kwargs):
            result = await original(prompt, **kwargs)
            await groups.update_group(GROUP, {"enabled": False})
            return result

        worker.models.answer = pause
        response = await state.client.post(f"/api/manage/groups/{GROUP}/insights/catchup", json={"window": "最近2小时"})
        assert response.status_code == 409
        assert "暂停" in response.json()["error"]["message"]
