import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

from tests.test_chatbot import Answers, BOT, MEMBER, context, event
from tests.test_douyin import GROUP
from wxhook_api.services.chat_summary import CHINA
from wxhook_api.services.reminders import parse_reminder


CURRENT = datetime(2026, 10, 17, 15, 0, tzinfo=CHINA)


@pytest.mark.parametrize("text,seconds,title", [
    ("提醒一分钟后喝水", 60, "喝水"),
    ("提醒我 1 分钟后喝水", 60, "喝水"),
    ("一分钟后提醒我喝水", 60, "喝水"),
    ("请帮我提醒半小时后休息", 1800, "休息"),
    ("提醒我一小时三十分钟后开会", 5400, "开会"),
    ("提醒我十秒后喝水", 10, "喝水"),
    ("提醒我两天后提交报告", 172800, "提交报告"),
    ("提醒我今天15:01喝水", 60, "喝水"),
    ("明天09:30提醒我交报告", 66600, "交报告"),
    ("提醒我14:00喝水", 82800, "喝水"),
])
def test_parse_times(text, seconds, title):
    due, content = parse_reminder(text, CURRENT)
    assert (due - CURRENT).total_seconds() == seconds
    assert content == title


@pytest.mark.parametrize("text", ["提醒我喝水", "提醒我零分钟后喝水", "提醒我999999天后喝水", "提醒我今天14:00喝水", "提醒我明天25:00喝水", "提醒一分钟后", "提醒一分钟后" + "长" * 201])
def test_invalid_reminders(text):
    with pytest.raises(ValueError):
        parse_reminder(text, CURRENT)


@pytest.mark.parametrize("text", ["你好", "一分钟后喝水", "讨论提醒功能", "今天15:01会议"])
def test_plain_chat_is_not_a_reminder(text):
    assert parse_reminder(text, CURRENT) is None


async def reminders(tools):
    return await tools.db(lambda db: [dict(row) for row in db.execute("SELECT * FROM member_reminders ORDER BY created_at")])


async def test_callback_records_and_mentions_original_member_once_without_model(tmp_path):
    provider = Answers()
    async with context(tmp_path, provider=provider) as state:
        tools = state.app.state.group_tools
        await tools.groups.save_member(GROUP, MEMBER, {"name": "小王"}, create=True)
        payload = event("@机器人\u2005提醒一分钟后喝水", atlist=[BOT])
        await state.submit(payload)
        await state.submit(payload)
        rows = await reminders(tools)
        assert len(rows) == 1 and rows[0]['sender'] == MEMBER
        assert rows[0]['title'] == "喝水" and rows[0]['status'] == "pending"
        assert 58 <= rows[0]['due_at'] - datetime.now(CHINA).timestamp() <= 60
        assert len(state.sends) == 1 and "已记录提醒" in state.sends[0]['msg']
        assert not provider.questions
        await tools.reminders.due_reminders()
        assert len(state.sends) == 1
        due = datetime.now(CHINA) + timedelta(minutes=2)
        await asyncio.gather(tools.reminders.due_reminders(due), tools.reminders.due_reminders(due))
        assert state.sends[1] == {"wxidorgid": GROUP, "msg": "@小王\u2005提醒时间到了：喝水", "atlist": [MEMBER]}
        assert len(state.sends) == 2 and (await reminders(tools))[0]['status'] == "sent"


async def test_list_cancel_and_scope(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.submit(event("@机器人\u2005提醒我10分钟后开会"))
        row = (await reminders(tools))[0]
        await state.submit(event("@机器人\u2005我的提醒", 2))
        assert row['id'] in state.sends[-1]['msg'] and "开会" in state.sends[-1]['msg']
        await state.submit(event("@机器人\u2005取消提醒 " + row['id'], 3, sender="wxid_other"))
        assert (await reminders(tools))[0]['status'] == "pending"
        result = await tools.reminders.chat_command("取消提醒 " + row['id'], "other@chatroom", MEMBER, "other")
        assert "未找到" in result
        await state.submit(event("@机器人\u2005取消提醒 " + row['id'], 4))
        assert "已取消" in state.sends[-1]['msg']
        await tools.reminders.due_reminders(datetime.now(CHINA) + timedelta(hours=1))
        assert (await reminders(tools))[0]['status'] == "cancelled"
        assert all('atlist' not in body for body in state.sends)


async def test_pending_survives_restart_and_callback_is_deduplicated(tmp_path):
    payload = event("@机器人\u2005提醒一分钟后喝水")
    async with context(tmp_path) as state:
        await state.submit(payload)
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.submit(payload)
        assert not state.sends and len(await reminders(tools)) == 1
        await tools.reminders.due_reminders(datetime.now(CHINA) + timedelta(minutes=2))
        assert len(state.sends) == 1 and state.sends[0]['atlist'] == [MEMBER]


@pytest.mark.parametrize("policy", ["member", "group", "chat"])
async def test_policy_rechecked_before_due_send(tmp_path, policy):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        if policy == "member":
            await tools.groups.save_member(GROUP, MEMBER, {"blocked": True})
        else:
            await tools.groups.update_group(GROUP, {"blocked": True} if policy == "group" else {"chat_enabled": False})
        await tools.reminders.due_reminders(datetime.now(CHINA) + timedelta(minutes=2))
        assert len(state.sends) == 1 and (await reminders(tools))[0]['status'] == "skipped"


async def test_uncertain_send_is_not_retried_even_after_restart(tmp_path):
    async def timeout(request):
        raise httpx.ReadTimeout("lost response")
    async with context(tmp_path) as state:
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
    async with context(tmp_path, hook=timeout) as state:
        tools = state.app.state.group_tools
        due = datetime.now(CHINA) + timedelta(minutes=2)
        await tools.reminders.due_reminders(due)
        await tools.reminders.due_reminders(due)
        assert len(state.sends) == 1 and (await reminders(tools))[0]['status'] == "unknown"
    async with context(tmp_path) as state:
        await state.app.state.group_tools.reminders.due_reminders(due)
        assert not state.sends


async def test_invalid_requests_do_not_create_reminders_or_call_model(tmp_path):
    provider = Answers()
    async with context(tmp_path, provider=provider) as state:
        await state.submit(event("@机器人\u2005提醒我喝水"))
        assert "请这样设置" in state.sends[-1]['msg']
        await state.submit(event("@机器人\u2005提醒一分钟后喝水", 2, sender=""))
        assert "未能识别" in state.sends[-1]['msg']
        assert not await reminders(state.app.state.group_tools)
        assert not provider.questions


async def test_background_scheduler_sends_and_restart_recovers_overdue_pending(tmp_path):
    async with context(tmp_path) as state:
        await state.submit(event("@机器人\u2005提醒一秒后喝水"))
        async with asyncio.timeout(5):
            while len(state.sends) < 2:
                await asyncio.sleep(0.05)
        assert state.sends[-1]['atlist'] == [MEMBER]
        tools = state.app.state.group_tools
        await state.submit(event("@机器人\u2005提醒一分钟后开会", 2))
        await tools.reminders.stop()
        await tools.db(lambda db: db.execute("UPDATE member_reminders SET due_at=0 WHERE status='pending'"))
    async with context(tmp_path) as state:
        async with asyncio.timeout(5):
            while not state.sends:
                await asyncio.sleep(0.05)
        assert len(state.sends) == 1 and "开会" in state.sends[0]['msg']


async def test_global_pause_preserves_pending_and_interrupted_send_is_not_retried(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        state.bot._accepting = False
        await tools.reminders.due_reminders(datetime.now(CHINA) + timedelta(minutes=2))
        assert (await reminders(tools))[0]['status'] == "pending" and len(state.sends) == 1
        await tools.db(lambda db: db.execute("UPDATE member_reminders SET status='sending',due_at=0"))
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.reminders.due_reminders()
        assert not state.sends and (await reminders(tools))[0]['status'] == "unknown"
