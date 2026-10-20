import asyncio
from datetime import datetime, timedelta

import pytest

from tests.test_chatbot import context, event
from tests.test_douyin import GROUP
from tests.test_group_tools import Models
from wxhook_api.services.chat_summary import CHINA
from wxhook_api.services.group_tools import report_period
from wxhook_api.schemas.events import CallbackEvent


async def test_due_query_includes_automatic_completion_but_not_closed_tasks(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        past = (datetime.now(CHINA) - timedelta(minutes=1)).isoformat()
        due = await tools.save_task(GROUP, dict(title="到期事项", owner="", due_at=past))
        manual = await tools.save_task(GROUP, dict(title="手动完成", owner=""))
        cancelled = await tools.save_task(GROUP, dict(title="已取消", owner=""))
        await tools.task_status(GROUP, manual, "done")
        await tools.task_status(GROUP, cancelled, "cancelled")
        for task_id in (manual, cancelled):
            await tools.save_task(GROUP, dict(title="手动完成" if task_id == manual else "已取消", owner="", due_at=past), task_id)
        await tools.save_task(GROUP, dict(title="未来事项", owner="", due_at=(datetime.now(CHINA) + timedelta(days=1)).isoformat()))
        await tools.save_task(GROUP, dict(title="无期限", owner=""))
        await tools.complete_due_tasks()
        reply = await state.bot._answer("到期待办", GROUP)
        assert "到期事项" in reply and "到期自动完成" in reply
        assert all(title not in reply for title in ("手动完成", "已取消", "未来事项", "无期限"))
        assert "到期事项" not in await state.bot._answer("本群待办", GROUP)
        await tools.task_status(GROUP, due, "done")
        assert "暂无" in await state.bot._answer("到期待办", GROUP)


@pytest.mark.parametrize("kind", ["week", "month"])
@pytest.mark.parametrize("automatic", [False, True])
async def test_empty_reports_recheck_imported_history_without_repeated_model_calls(tmp_path, kind, automatic):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.preferences(GROUP, enabled=True)
        models = Models("历史导入后的报表")
        tools.models = models
        empty = await tools.report(GROUP, kind, automatic=automatic)
        assert empty["message_count"] == 0
        assert await tools.report(GROUP, kind, automatic=automatic) == empty
        assert not models.calls
        start, _ = report_period(kind)
        ts = int(datetime.combine(start, datetime.min.time(), CHINA).timestamp())
        await tools.groups.import_history(GROUP, [dict(msgid=999, timestamp=ts, sender="wxid_member", type=1, content="本期已完成测试")])
        report = await tools.report(GROUP, kind, automatic=automatic)
        assert report["message_count"] == 1 and report["content"] == models.result
        assert await tools.report(GROUP, kind, automatic=automatic) == report
        assert len(models.calls) == 1


async def seed_tasks(tools, group, status, count):
    await tools.db(lambda db: db.executemany(
        "INSERT INTO group_tasks (id,group_id,title,owner,due_date,status,created_at,updated_at) VALUES (?,?,?,'','',?,'','')",
        [(f"{group}-{status}-{i}", group, f"历史事项{i}", status) for i in range(count)]))


async def test_closed_tasks_do_not_consume_manual_or_member_reminder_capacity(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await seed_tasks(tools, GROUP, "done", 1000)
        await seed_tasks(tools, GROUP, "cancelled", 1000)
        response = await state.client.post(f"/api/manage/groups/{GROUP}/tools/tasks", json={"title": "新的事项"})
        assert response.status_code == 200
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        tasks = (await tools.snapshot(GROUP))["tasks"]
        assert len(tasks) == 2002
        reminder = next(task for task in tasks if task["source_kind"] == "reminder")
        assert reminder["status"] == "open" and reminder["reminder_status"] == "pending"


async def test_capacity_is_enforced_for_create_reopen_and_reschedule(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        closed = await tools.save_task(GROUP, dict(title="可重开", owner=""))
        await tools.task_status(GROUP, closed, "cancelled")
        auto = await tools.save_task(GROUP, dict(title="可延期", owner="", due_at=(datetime.now(CHINA) - timedelta(minutes=1)).isoformat()))
        await tools.complete_due_tasks()
        await seed_tasks(tools, GROUP, "open", 1000)
        url = f"/api/manage/groups/{GROUP}/tools/tasks"
        assert (await state.client.post(url, json={"title": "超额"})).status_code == 409
        assert (await state.client.patch(f"{url}/{closed}", json={"status": "open"})).status_code == 409
        assert (await state.client.put(f"{url}/{auto}", json={"title": "延期", "due_at": (datetime.now(CHINA) + timedelta(days=1)).isoformat()})).status_code == 409
        await state.submit(event("@机器人\u2005提醒一分钟后喝水"))
        assert "1000 条未完成待办" in state.sends[-1]["msg"]
        assert not any(task["reminder_id"] for task in (await tools.snapshot(GROUP))["tasks"])
        other = "other@chatroom"
        await tools.groups.add_group(other, "另一群")
        assert await tools.save_task(other, dict(title="独立容量", owner=""))
        await tools.task_status(GROUP, f"{GROUP}-open-0", "done")
        assert (await state.client.patch(f"{url}/{closed}", json={"status": "open"})).status_code == 200
        assert (await state.client.post(url, json={"title": "再次超额"})).status_code == 409


@pytest.mark.parametrize("changed", [{"keyword": "费用", "reply": "100元"}, {"match_mode": "exact"}])
async def test_queued_rules_rematch_edited_definition_before_claim(tmp_path, changed):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        tools._worker.cancel()
        await asyncio.gather(tools._worker, return_exceptions=True)
        body = dict(name="查询", keyword="天气", match_mode="contains", action="reply", reply="晴天", enabled=True, cooldown=10)
        rule_id = await tools.save_rule(GROUP, body)
        assert await tools.receive(CallbackEvent.model_validate(event("天气怎么样")))
        job = tools._queue.get_nowait()
        await tools.save_rule(GROUP, {**body, **changed}, rule_id)
        await tools._process_rule(job)
        tools._queue.task_done()
        assert not state.sends
        snapshot = await tools.snapshot(GROUP)
        assert not snapshot["hits"] and not snapshot["rules"][0]["last_triggered"]


@pytest.mark.parametrize("changed", [{"keyword": "费用"}, {"match_mode": "exact"}, {"reply": "新的回复"}])
async def test_rule_edits_during_send_account_check_stop_old_reply(tmp_path, monkeypatch, changed):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        body = dict(name="查询", keyword="天气", match_mode="contains", action="reply", reply="晴天", enabled=True, cooldown=10)
        rule_id = await tools.save_rule(GROUP, body)
        entered, release = asyncio.Event(), asyncio.Event()
        async def pause():
            entered.set()
            await release.wait()
        monkeypatch.setattr(state.bot._messenger, "_verify_account", pause)
        pending = asyncio.create_task(tools._process_rule((GROUP, rule_id, "race", "wxid_member", "天气怎么样")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await tools.save_rule(GROUP, {**body, **changed}, rule_id)
            release.set()
            await asyncio.wait_for(pending, 2)
            assert not state.sends
            assert (await tools.snapshot(GROUP))["hits"][0]["status"] == "skipped"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("length", [1000, 2000])
async def test_long_messages_keep_full_text_for_rule_matching(tmp_path, length):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        text = "x" * length + "TRIGGER"
        await tools.save_rule(GROUP, dict(name="长消息", keyword="trigger", match_mode="contains", action="reply", reply="收到", enabled=True, cooldown=10))
        await state.submit(event(text))
        await asyncio.wait_for(tools._queue.join(), 2)
        assert [item["msg"] for item in state.sends] == ["收到"]
        assert len((await tools.snapshot(GROUP))["hits"][0]["content"]) == 1000
