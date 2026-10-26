from datetime import date, datetime, timedelta
import json

import httpx
from asgi_lifespan import LifespanManager

from tests.test_chatbot import context, event
from tests.test_douyin import GROUP
from tests.test_manage import make_service
from wxhook_api.main import create_app
from wxhook_api.services.chat_summary import CHINA
from wxhook_api.services.group_tools import report_period


class Models:
    settings = True

    def __init__(self, result="本期讨论项目，确定周五完成测试。"):
        self.result, self.calls = result, []

    async def answer(self, prompt, **kwargs):
        self.calls.append(prompt)
        return self.result


def test_report_calendar_boundaries():
    assert report_period("week", date(2026, 10, 17)) == (date(2026, 10, 5), date(2026, 10, 12))
    assert report_period("month", date(2026, 1, 1)) == (date(2025, 12, 1), date(2026, 1, 1))
    assert report_period("month", date(2024, 3, 1)) == (date(2024, 2, 1), date(2024, 3, 1))


async def test_reports_sample_correct_period_bot_filter_persistence_and_scheduler(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await state.app.state.groups.add_group(GROUP, "测试群")
        models = Models()
        tools.models = models
        start, end = report_period("week")
        ts = int(datetime.combine(start, datetime.min.time(), CHINA).timestamp())
        await tools.groups.claim_account("wxid_robot")
        await tools.groups.import_history(GROUP, [
            {"msgid": 1, "timestamp": ts, "type": 1, "sender": "wxid_member", "content": "周五完成测试"},
            {"msgid": 2, "timestamp": ts - 1, "type": 1, "sender": "wxid_member", "content": "范围之外"},
            {"msgid": 3, "timestamp": ts + 1, "type": 1, "sender": "wxid_robot", "content": "机器人内容"},
            {"msgid": 4, "timestamp": ts + 2, "type": 1, "sender": "wxid_member2", "is_self": "true", "content": "自身标识"},
            {"msgid": 5, "timestamp": ts + 3, "type": 3, "sender": "wxid_member", "content": "非文本"},
        ])
        report = await tools.report(GROUP, "week")
        assert report["message_count"] == report["sampled_count"] == 1
        assert report["start_date"] == str(start) and report["end_date"] == str(end - timedelta(days=1))
        assert await tools.report(GROUP, "week") == report
        assert len(models.calls) == 1
        assert "周五完成测试" in models.calls[0] and "范围之外" not in models.calls[0] and "机器人内容" not in models.calls[0]
        await tools.preferences(GROUP, True, False)
        await tools.scheduled_reports()
        count = len(models.calls)
        await tools.scheduled_reports()
        assert len(models.calls) == count, "Scheduled reports are durably cached"
        assert not state.sends, "Reports are saved locally"
        assert "本期讨论" in await state.bot._answer("周报", GROUP)


async def test_keyword_record_reply_cooldown_dedup_policy_and_self(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.groups.add_group(GROUP, "测试群")
        rule = dict(name="故障反馈", keyword="故障", match_mode="contains", action="reply", reply="请提供发生时间。", enabled=False, cooldown=60)
        rule_id = await tools.save_rule(GROUP, rule)
        await state.submit(event("系统故障", 10))
        await tools._queue.join()
        assert not state.sends
        rule["enabled"] = True
        await tools.save_rule(GROUP, rule, rule_id)
        await state.submit(event("系统故障", 11))
        await tools._queue.join()
        assert len(state.sends) == 1 and state.sends[0]["msg"] == rule["reply"]
        await state.submit(event("系统故障", 12))
        await tools._queue.join()
        assert len(state.sends) == 1, "Cooldown applies to all members"
        await tools.db(lambda db: db.execute("UPDATE keyword_rules SET last_triggered=''"))
        await state.submit(event("系统故障", 11))
        await tools._queue.join()
        assert len(state.sends) == 1, "Duplicate callbacks do not repeat after cooldown"
        await state.submit(event("系统故障", 13, is_self=True))
        await tools.groups.update_group(GROUP, {"blocked": True})
        await state.submit(event("系统故障", 14))
        await tools._queue.join()
        assert len(state.sends) == 1
        await tools.groups.update_group(GROUP, {"blocked": False})
        rule.update(action="record", match_mode="exact", keyword="English", reply="")
        await tools.save_rule(GROUP, rule, rule_id)
        await tools.db(lambda db: db.execute("UPDATE keyword_rules SET last_triggered=''"))
        await state.submit(event("English extra", 15))
        await state.submit(event("english", 16))
        await tools._queue.join()
        data = await tools.snapshot(GROUP)
        assert [hit["status"] for hit in data["hits"]] == ["recorded", "sent"]


async def test_rules_recheck_queued_jobs_and_failed_sends_do_not_repeat(tmp_path):
    async def fail(request):
        raise httpx.ReadTimeout("lost response")
    async with context(tmp_path, hook=fail) as state:
        tools = state.app.state.group_tools
        await tools.groups.add_group(GROUP, "测试群")
        rule_id = await tools.save_rule(GROUP, dict(name="反馈", keyword="故障", match_mode="contains", action="reply", reply="收到", enabled=True, cooldown=10))
        job = (GROUP, rule_id, "42", "wxid_member", "故障")
        await tools._process_rule(job)
        await tools.db(lambda db: db.execute("UPDATE keyword_rules SET last_triggered=''"))
        await tools._process_rule(job)
        assert len(state.sends) == 1
        assert (await tools.snapshot(GROUP))["hits"][0]["status"] == "unknown"
        await tools.groups.save_member(GROUP, "wxid_member", {"blocked": True}, create=True)
        await tools._process_rule((GROUP, rule_id, "43", "wxid_member", "故障"))
        assert len(state.sends) == 1


async def test_task_candidates_need_save_and_keep_only_current_group_sources(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.groups.add_group(GROUP, "测试群")
        ts = int(datetime.now(CHINA).timestamp())
        await tools.groups.import_history(GROUP, [{"msgid": 1, "timestamp": ts, "sender": "wxid_member", "type": 1, "content": "小王明天提交报告"}])
        _, rows = await tools.text_window(GROUP, datetime.now(CHINA).date(), datetime.now(CHINA).date() + timedelta(days=1))
        source_id = rows[0]["id"]
        tools.models = Models(json.dumps([
            dict(title="提交报告", owner="小王", due_date=(datetime.now(CHINA).date() + timedelta(days=1)).isoformat(), source_id=source_id),
            dict(title="伪造来源", source_id=9999),
            dict(title="无效截止日", due_date="bad", source_id=source_id),
        ]))
        candidates = await tools.extract_tasks(GROUP)
        assert len(candidates) == 2 and candidates[1]["due_date"] == ""
        assert not (await tools.snapshot(GROUP))["tasks"]
        task = {key: candidates[0][key] for key in ("title", "owner", "due_date", "source_id")}
        await tools.save_task(GROUP, task)
        await tools.save_task(GROUP, task)
        tasks = (await tools.snapshot(GROUP))["tasks"]
        assert len(tasks) == 1
        assert "提交报告" in await state.bot._answer("本群待办", GROUP)
        assert await tools.task_status(GROUP, tasks[0]["id"], "done")
        assert "暂无" in await state.bot._answer("本群待办", GROUP)


async def test_due_reminder_opt_in_once_per_day_and_blacklist(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.groups.add_group(GROUP, "测试群")
        await tools.save_task(GROUP, dict(title="提交报告", owner="小王", due_date="2020-01-01"))
        await tools.due_reminders()
        assert not state.sends
        await tools.preferences(GROUP, False, True)
        await tools.groups.update_group(GROUP, {"blocked": True})
        await tools.due_reminders()
        assert not state.sends
        await tools.groups.update_group(GROUP, {"blocked": False})
        await tools.due_reminders()
        await tools.due_reminders()
        assert len(state.sends) == 1 and "提交报告" in state.sends[0]["msg"]


async def test_tools_api_validation_scope_and_persisted_state(tmp_path):
    app = create_app(douyin_service=make_service(tmp_path))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await app.state.groups.add_group(GROUP, "测试群")
        url = f"/api/manage/groups/{GROUP}/tools"
        first = await client.get(url)
        assert first.headers["cache-control"] == "no-store" and first.json()["auto_reports"] is False
        assert (await client.get("/api/manage/groups/missing@chatroom/tools")).status_code == 404
        rule = dict(name="故障", keyword="故障", enabled=True)
        rule_id = (await client.post(url + "/rules", json=rule)).json()["id"]
        groups_state = (await client.get("/api/manage/state")).json()["groups"]
        assert next(group for group in groups_state if group["id"] == GROUP)["tools_enabled"]
        assert (await client.put("/api/manage/groups/missing@chatroom/tools/rules/" + rule_id, json=rule)).status_code == 404
        assert (await client.post(url + "/rules", json={**rule, "action": "reply"})).status_code == 422
        assert (await client.post(url + "/rules", json={**rule, "cooldown": 0})).status_code == 422
        assert (await client.post(url + "/tasks", json={"title":"测试", "due_date":"2026-02-31"})).status_code == 422
        assert (await client.post(url + "/tasks", json={"title":"测试", "source_id":9999})).status_code == 409
        task_id = (await client.post(url + "/tasks", json={"title":"测试"})).json()["id"]
        assert (await client.patch(url + "/tasks/" + task_id, json={"status":"done"})).status_code == 200
        assert (await client.patch(url + "/preferences", json={"auto_reports":True})).status_code == 200
        assert (await client.patch(url + "/preferences", json={"task_reminders":True})).status_code == 200
        assert (await client.get(url)).json()["auto_reports"] is True
        assert (await client.patch(url + "/preferences", json={"task_reminders":False})).status_code == 200
        assert (await client.get(url)).json()["tasks"][0]["status"] == "done"
        assert (await client.delete(url + "/rules/" + rule_id)).status_code == 200
        assert not (await client.get(url)).json()["rules"]
    async with LifespanManager(app):
        data = await app.state.group_tools.snapshot(GROUP)
        assert data["auto_reports"] and data["tasks"][0]["id"] == task_id
