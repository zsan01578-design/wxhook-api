from datetime import datetime, timedelta
import json
import sqlite3

import pytest

from tests.test_chatbot import MEMBER, context, event
from tests.test_douyin import GROUP
from tests.test_group_tools import Models
from wxhook_api.services.chat_summary import CHINA


async def test_reminder_is_a_task_with_creator_and_precise_deadline(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.groups.save_member(GROUP, MEMBER, {"name": "小王"}, create=True)
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        tasks = (await tools.snapshot(GROUP))["tasks"]
        assert len(tasks) == 1
        task = tasks[0]
        assert task["source_kind"] == "reminder" and task["creator_id"] == MEMBER
        assert task["creator_display_name"] == "小王" and task["title"] == "喝水"
        assert task["due_at"] - datetime.fromisoformat(task["created_at"]).timestamp() == 60
        assert task["reminder_status"] == "pending" and task["status"] == "open"
        await tools.reminders.due_reminders(datetime.now(CHINA) + timedelta(minutes=2))
        task = (await tools.snapshot(GROUP))["tasks"][0]
        assert task["status"] == "done" and task["completion_reason"] == "deadline"
        assert task["reminder_status"] == "sent"
        await tools.preferences(GROUP, reminders=True)
        await tools.due_reminders()
        assert len(state.sends) == 2, "Reminder tasks are excluded from the daily task digest"


@pytest.mark.parametrize("status", ["done", "cancelled"])
async def test_closing_task_stops_pending_reminder_and_reopen_preserves_creator(tmp_path, status):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        task = (await tools.snapshot(GROUP))["tasks"][0]
        url = f"/api/manage/groups/{GROUP}/tools/tasks/{task['id']}"
        response = await state.client.patch(url, json={"status": status})
        assert response.status_code == 200
        closed = (await tools.snapshot(GROUP))["tasks"][0]
        assert closed["status"] == status and closed["reminder_status"] == "cancelled"
        assert bool(closed["completed_at"]) == (status == "done")
        await tools.reminders.due_reminders(datetime.now(CHINA) + timedelta(minutes=2))
        assert len(state.sends) == 1
        await state.submit(event("@机器人\u2005我的提醒", 2))
        assert "暂无" in state.sends[-1]["msg"]
        assert (await state.client.patch(url, json={"status": "open"})).status_code == 200
        reopened = (await tools.snapshot(GROUP))["tasks"][0]
        assert reopened["status"] == "open" and reopened["reminder_status"] == "pending"
        assert reopened["creator_id"] == task["creator_id"] and reopened["created_at"] == task["created_at"]
        assert not reopened["completed_at"]


async def test_chat_cancel_updates_unified_task(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        task = (await tools.snapshot(GROUP))["tasks"][0]
        await state.submit(event("@机器人\u2005取消提醒 " + task["reminder_id"], 2))
        task = (await tools.snapshot(GROUP))["tasks"][0]
        assert task["status"] == "cancelled" and task["reminder_status"] == "cancelled"


async def test_manual_ai_and_edit_keep_metadata_and_validate_deadlines(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        url = f"/api/manage/groups/{GROUP}/tools/tasks"
        body = {"title": "提交报告", "due_at": "2027-01-01T15:30:25+08:00"}
        response = await state.client.post(url, json=body)
        assert response.status_code == 200
        task_id = response.json()["id"]
        task = (await tools.snapshot(GROUP))["tasks"][0]
        assert task["due_at"] == datetime(2027, 1, 1, 15, 30, 25, tzinfo=CHINA).timestamp()
        assert task["source_kind"] == "manual" and task["creator_name"] == "本地管理员"
        assert task["creator_id"] == "local-admin"
        assert (await state.client.post(url, json={"title": "无效", "due_at": "2027-02-30T15:30:25"})).status_code == 422
        assert (await state.client.post(url, json={"title": "无效", "due_at": "2027-01-01"})).status_code == 422
        assert (await state.client.post(url, json={"title": "伪造", "creator_id": MEMBER})).status_code == 422
        edited = {"title": "提交周报", "owner": "小王", "due_at": "2027-01-01T16:30:26"}
        assert (await state.client.put(url + "/" + task_id, json=edited)).status_code == 200
        updated = (await tools.snapshot(GROUP))["tasks"][0]
        assert updated["due_at"] == datetime(2027, 1, 1, 16, 30, 26, tzinfo=CHINA).timestamp()
        assert updated["created_at"] == task["created_at"] and updated["creator_id"] == task["creator_id"]
        await tools.groups.add_group("other@chatroom", "另一群")
        assert (await state.client.put("/api/manage/groups/other@chatroom/tools/tasks/" + task_id, json=edited)).status_code == 404
        ts = int(datetime.now(CHINA).timestamp())
        await tools.groups.import_history(GROUP, [{"msgid": 900, "timestamp": ts, "sender": MEMBER, "type": 1, "content": "明天提交月报"}])
        _, sources = await tools.text_window(GROUP, datetime.now(CHINA).date(), datetime.now(CHINA).date() + timedelta(days=1))
        source = next(item for item in sources if item["content"] == "明天提交月报")
        tools.models = Models(json.dumps([dict(title="提交月报", source_id=source["id"], due_date="2027-01-02")]))
        candidate = (await tools.extract_tasks(GROUP))[0]
        payload = {key: candidate[key] for key in ("title", "owner", "due_date", "source_id")}
        ai = await state.client.post(url, json=payload)
        await state.client.post(url, json=payload)
        tasks = (await tools.snapshot(GROUP))["tasks"]
        assert len(tasks) == 2
        ai_task = next(item for item in tasks if item["id"] == ai.json()["id"])
        assert ai_task["source_kind"] == "ai" and ai_task["creator_id"] == "local-admin"
        assert ai_task["due_at"] == datetime(2027, 1, 2, 23, 59, 59, tzinfo=CHINA).timestamp()


async def test_scheduler_rechecks_task_and_edited_deadline_before_claim(tmp_path, monkeypatch):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        task = (await tools.snapshot(GROUP))["tasks"][0]
        original_members = tools.groups.members
        async def reschedule(group):
            await tools.save_task(group, dict(title="喝水", owner="", due_at=(datetime.now(CHINA) + timedelta(hours=1)).isoformat()), task["id"])
            return await original_members(group)
        monkeypatch.setattr(tools.groups, "members", reschedule)
        await tools.reminders.due_reminders(datetime.now(CHINA) + timedelta(minutes=2))
        assert len(state.sends) == 1
        assert (await tools.snapshot(GROUP))["tasks"][0]["reminder_status"] == "pending"
        monkeypatch.setattr(tools.groups, "members", original_members)
        await tools.task_status(GROUP, task["id"], "done")
        await tools.reminders.due_reminders(datetime.now(CHINA) + timedelta(hours=2))
        assert len(state.sends) == 1


async def test_rescheduling_a_sent_reminder_is_explicit_and_preserves_task_identity(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        task = (await tools.snapshot(GROUP))["tasks"][0]
        await tools.reminders.due_reminders(datetime.now(CHINA) + timedelta(minutes=2))
        await tools.task_status(GROUP, task["id"], "done")
        await tools.task_status(GROUP, task["id"], "open")
        assert (await tools.snapshot(GROUP))["tasks"][0]["reminder_status"] == "sent"
        due = datetime.now(CHINA) + timedelta(hours=1)
        await tools.save_task(GROUP, dict(title="再次喝水", owner="", due_at=due.isoformat()), task["id"])
        updated = (await tools.snapshot(GROUP))["tasks"][0]
        assert updated["id"] == task["id"] and updated["creator_id"] == task["creator_id"]
        assert updated["reminder_status"] == "pending"
        await tools.reminders.due_reminders(due - timedelta(seconds=1))
        assert len(state.sends) == 2
        await tools.reminders.due_reminders(due)
        assert len(state.sends) == 3 and "再次喝水" in state.sends[-1]["msg"]


async def test_daily_digest_does_not_send_before_precise_manual_deadline(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        task_id = await tools.save_task(GROUP, dict(title="稍后开会", owner="", due_at=(datetime.now(CHINA) + timedelta(minutes=1)).isoformat()))
        await tools.preferences(GROUP, reminders=True)
        await tools.due_reminders()
        assert not state.sends
        await tools.save_task(GROUP, dict(title="稍后开会", owner="", due_at=(datetime.now(CHINA) - timedelta(minutes=1)).isoformat()), task_id)
        await tools.due_reminders()
        await tools.due_reminders()
        assert len(state.sends) == 1
        await tools.db(lambda db: db.execute("UPDATE group_tool_reminders SET reminder_date='2000-01-01'"))
        await tools.due_reminders()
        assert len(state.sends) == 1, "Auto-completed tasks are not announced again the next day"


async def test_all_task_sources_complete_at_deadline_even_while_bot_paused(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.reminders.stop()
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        reminder = (await tools.snapshot(GROUP))["tasks"][0]
        deadline = datetime.fromtimestamp(reminder["due_at"], CHINA)
        await tools.save_task(GROUP, dict(title="手动定时", owner="", due_at=deadline.isoformat()))
        ts = int(datetime.now(CHINA).timestamp())
        await tools.groups.import_history(GROUP, [dict(msgid=333, timestamp=ts, type=1, sender=MEMBER, content="准备报告")])
        _, rows = await tools.text_window(GROUP, datetime.now(CHINA).date(), datetime.now(CHINA).date() + timedelta(days=1))
        source = next(row for row in rows if row["content"] == "准备报告")
        await tools.save_task(GROUP, dict(title="AI定时", owner="", source_id=source["id"], due_at=deadline.isoformat()))
        await tools.save_task(GROUP, dict(title="没有截止时间", owner=""))
        cancelled = await tools.save_task(GROUP, dict(title="已取消", owner="", due_at=deadline.isoformat()))
        await tools.task_status(GROUP, cancelled, "cancelled")
        original = {task["id"]: task for task in (await tools.snapshot(GROUP))["tasks"]}
        state.bot._accepting = False
        await tools.reminders.due_reminders(deadline - timedelta(microseconds=1))
        tasks = (await tools.snapshot(GROUP))["tasks"]
        assert sum(task["status"] == "open" for task in tasks) == 4
        await tools.reminders.due_reminders(deadline)
        tasks = (await tools.snapshot(GROUP))["tasks"]
        timed = [task for task in tasks if task["title"] in ("喝水", "手动定时", "AI定时")]
        assert len(timed) == 3 and all(task["status"] == "done" for task in timed)
        assert all(task["completion_reason"] == "deadline" and task["completed_at"] == deadline.isoformat() for task in timed)
        assert all(task["creator_id"] == original[task["id"]]["creator_id"] for task in timed)
        assert next(task for task in tasks if task["title"] == "没有截止时间")["status"] == "open"
        assert next(task for task in tasks if task["id"] == cancelled)["status"] == "cancelled"
        assert len(state.sends) == 1
        state.bot._accepting = True
        await tools.reminders.due_reminders(deadline + timedelta(seconds=5))
        await tools.reminders.due_reminders(deadline + timedelta(seconds=6))
        assert len(state.sends) == 2 and state.sends[-1]["atlist"] == [MEMBER]
        assert next(task for task in (await tools.snapshot(GROUP))["tasks"] if task["id"] == reminder["id"])["completed_at"] == deadline.isoformat()


async def test_legacy_tasks_and_reminders_are_migrated_once(tmp_path):
    future = datetime.now(CHINA) + timedelta(days=1)
    created = datetime.now(CHINA).isoformat()
    with sqlite3.connect(tmp_path / "management.sqlite3") as db:
        db.executescript("""
            CREATE TABLE group_tasks (id TEXT PRIMARY KEY,group_id TEXT NOT NULL,title TEXT NOT NULL,
                owner TEXT NOT NULL,due_date TEXT NOT NULL,status TEXT NOT NULL,source_id INTEGER,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE member_reminders (id TEXT PRIMARY KEY,group_id TEXT NOT NULL,sender TEXT NOT NULL,
                message_key TEXT NOT NULL,title TEXT NOT NULL,due_at REAL NOT NULL,status TEXT NOT NULL,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(group_id,message_key));
        """)
        db.execute("INSERT INTO group_tasks VALUES ('legacy',?,'旧任务','','2026-10-18','done',NULL,?,?)", (GROUP, created, created))
        for index, status in enumerate(("pending", "sent", "cancelled")):
            db.execute("INSERT INTO member_reminders VALUES (?,?,?,?,?,?,?,?,?)",
                       (str(index), GROUP, MEMBER, str(index), "喝水", future.timestamp(), status, created, created))
    async with context(tmp_path) as state:
        tasks = (await state.app.state.group_tools.snapshot(GROUP))["tasks"]
        assert len(tasks) == 4
        legacy = next(item for item in tasks if item["id"] == "legacy")
        assert legacy["status"] == "done" and legacy["completed_at"] == created
        assert legacy["creator_name"] == "未知（历史记录）" and legacy["due_at"] is not None
        assert next(item for item in tasks if item["reminder_id"] == "1")["status"] == "open"
        assert next(item for item in tasks if item["reminder_id"] == "2")["status"] == "cancelled"
    async with context(tmp_path) as state:
        assert len((await state.app.state.group_tools.snapshot(GROUP))["tasks"]) == 4
