from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from uuid import uuid4

from wxhook_api.services.chat_model import ModelError
from wxhook_api.services.chat_summary import CHINA
from wxhook_api.services.groups import event_group, now
from wxhook_api.services.reminders import ReminderService
from wxhook_api.core.ai_safety import DATA_SYSTEM, bounded_records, ai_permission
from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.services.douyin_delivery import DeliveryBlocked


def report_period(kind: str, today: date | None = None) -> tuple[date, date]:
    today = today or datetime.now(CHINA).date()
    if kind == "week":
        end = today - timedelta(days=today.weekday())
        return end - timedelta(days=7), end
    end = today.replace(day=1)
    return (end - timedelta(days=1)).replace(day=1), end


def task_deadline(body):
    if body.get("due_at"):
        due = datetime.fromisoformat(body["due_at"])
        due = due.replace(tzinfo=CHINA) if due.tzinfo is None else due.astimezone(CHINA)
        return due.date().isoformat(), due.timestamp()
    if body.get("due_date"):
        due = datetime.combine(date.fromisoformat(body["due_date"]), datetime.max.time().replace(microsecond=0), CHINA)
        return due.date().isoformat(), due.timestamp()
    return "", None


class GroupToolsService:
    def __init__(self, groups, models, chatbot):
        self.groups, self.models, self.chatbot = groups, models, chatbot
        self._generation_lock = asyncio.Lock()
        self._queue = asyncio.Queue(maxsize=100)
        self._worker = self._scheduler = None
        self._auto_report_groups = set()
        self.last_error = ""
        self.reminders = ReminderService(self)

    async def db(self, action):
        def execute():
            with self.groups._connect() as connection:
                return action(connection)
        async with self.groups._lock:
            return await asyncio.to_thread(execute)

    async def start(self):
        def initialize(db):
            db.executescript("""
                CREATE TABLE IF NOT EXISTS group_reports (
                    group_id TEXT NOT NULL, kind TEXT NOT NULL, start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL, content TEXT NOT NULL, message_count INTEGER NOT NULL,
                    sampled_count INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(group_id,kind,start_date));
                CREATE TABLE IF NOT EXISTS group_tool_preferences (
                    group_id TEXT PRIMARY KEY, auto_reports INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS keyword_rules (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, name TEXT NOT NULL,
                    keyword TEXT NOT NULL, match_mode TEXT NOT NULL, action TEXT NOT NULL,
                    reply TEXT NOT NULL, enabled INTEGER NOT NULL, cooldown INTEGER NOT NULL,
                    last_triggered TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS keyword_rules_group ON keyword_rules(group_id);
                CREATE TABLE IF NOT EXISTS keyword_hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL, rule_name TEXT NOT NULL, message_key TEXT NOT NULL,
                    sender TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(group_id,rule_id,message_key));
                CREATE TABLE IF NOT EXISTS group_tasks (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, title TEXT NOT NULL,
                    owner TEXT NOT NULL, due_date TEXT NOT NULL, status TEXT NOT NULL,
                    source_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS group_tasks_group ON group_tasks(group_id,status,due_date);
                CREATE UNIQUE INDEX IF NOT EXISTS group_tasks_source ON group_tasks(group_id,source_id,title)
                    WHERE source_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS group_tool_reminders (
                    group_id TEXT PRIMARY KEY, reminder_date TEXT NOT NULL, status TEXT NOT NULL);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(group_tool_preferences)")}
            if "task_reminders" not in columns:
                db.execute("ALTER TABLE group_tool_preferences ADD COLUMN task_reminders INTEGER NOT NULL DEFAULT 0")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(group_tasks)")}
            fields = {"due_at": "REAL", "creator_id": "TEXT NOT NULL DEFAULT ''",
                      "creator_name": "TEXT NOT NULL DEFAULT '未知（历史记录）'",
                      "source_kind": "TEXT NOT NULL DEFAULT 'manual'", "reminder_id": "TEXT",
                      "completed_at": "TEXT NOT NULL DEFAULT ''", "completion_reason": "TEXT NOT NULL DEFAULT ''",
                      "digest_attempted_at": "TEXT NOT NULL DEFAULT ''"}
            for field, definition in fields.items():
                if field not in columns:
                    db.execute(f"ALTER TABLE group_tasks ADD COLUMN {field} {definition}")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS group_tasks_reminder ON group_tasks(reminder_id) WHERE reminder_id IS NOT NULL")
            db.execute("CREATE INDEX IF NOT EXISTS group_tasks_due ON group_tasks(status,due_at)")
            for row in db.execute("SELECT id,due_date FROM group_tasks WHERE due_at IS NULL AND due_date<>''").fetchall():
                _, deadline = task_deadline(dict(due_date=row["due_date"]))
                db.execute("UPDATE group_tasks SET due_at=? WHERE id=?", (deadline, row["id"]))
            db.execute("UPDATE group_tasks SET completed_at=updated_at WHERE status='done' AND completed_at=''")
            db.execute("UPDATE group_tasks SET source_kind='ai' WHERE source_id IS NOT NULL AND reminder_id IS NULL")
        await self.db(initialize)
        self._auto_report_groups = set(await self.db(lambda db: [row[0] for row in db.execute(
            "SELECT group_id FROM group_tool_preferences WHERE auto_reports=1")]))
        await self.reminders.start()
        self._worker = asyncio.create_task(self._run_rules())
        self._scheduler = asyncio.create_task(self._run_schedule())

    async def stop(self):
        await self.reminders.stop()
        tasks = [task for task in (self._worker, self._scheduler) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._worker = self._scheduler = None

    async def snapshot(self, group):
        def read(db):
            preferences = db.execute("SELECT auto_reports,task_reminders FROM group_tool_preferences WHERE group_id=?", (group,)).fetchone()
            return {
                "group_id": group, "auto_reports": bool(preferences and preferences[0]),
                "task_reminders": bool(preferences and preferences[1]),
                "reports": [dict(row) for row in db.execute("SELECT * FROM group_reports WHERE group_id=? ORDER BY start_date DESC,kind LIMIT 12", (group,))],
                "rules": [{**dict(row), "enabled": bool(row["enabled"])} for row in db.execute("SELECT * FROM keyword_rules WHERE group_id=? ORDER BY name,id", (group,))],
                "hits": [dict(row) for row in db.execute("SELECT * FROM keyword_hits WHERE group_id=? ORDER BY id DESC LIMIT 30", (group,))],
                "tasks": [dict(row) for row in db.execute("""SELECT t.*,COALESCE(NULLIF(m.wechat_name,''),NULLIF(m.name,''),t.creator_name) AS creator_display_name,
                    r.status AS reminder_status FROM group_tasks t
                    LEFT JOIN members m ON m.group_id=t.group_id AND m.id=t.creator_id
                    LEFT JOIN member_reminders r ON r.id=t.reminder_id
                    WHERE t.group_id=? ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'done' THEN 1 ELSE 2 END,
                    t.due_at IS NULL,t.due_at,t.created_at DESC""", (group,))],
                "today": datetime.now(CHINA).date().isoformat(),
            }
        return await self.db(read)

    async def enabled_groups(self):
        return await self.db(lambda db: {row[0] for row in db.execute("SELECT DISTINCT group_id FROM keyword_rules WHERE enabled=1 UNION SELECT group_id FROM group_tool_preferences WHERE task_reminders=1")})

    async def preferences(self, group, enabled=None, reminders=None):
        def save(db):
            current = db.execute("SELECT auto_reports,task_reminders FROM group_tool_preferences WHERE group_id=?", (group,)).fetchone()
            reports_value = enabled if enabled is not None else bool(current and current[0])
            reminders_value = reminders if reminders is not None else bool(current and current[1])
            db.execute("INSERT INTO group_tool_preferences(group_id,auto_reports,task_reminders) VALUES (?,?,?) ON CONFLICT(group_id) DO UPDATE SET auto_reports=excluded.auto_reports,task_reminders=excluded.task_reminders", (group, reports_value, reminders_value))
            if enabled is not None:
                self.groups._audit(db, group, group, "自动周报/月报 · " + ("开启" if enabled else "关闭"))
            if reminders is not None:
                self.groups._audit(db, group, group, "待办到期提醒 · " + ("开启" if reminders else "关闭"))
            return reports_value
        async def commit():
            reports_value = await self.db(save)
            if reports_value:
                self._auto_report_groups.add(group)
            else:
                self._auto_report_groups.discard(group)
        await finish_before_cancel(commit())

    def automatic_report_allowed(self, group):
        return (group in self._auto_report_groups and self.groups.allowed(group)
                and self.chatbot.options.enabled)

    async def text_window(self, group, start, end, limit=800):
        def read(db):
            identity_row = db.execute("SELECT value FROM sync_metadata WHERE key='account'").fetchone()
            identity = identity_row[0] if identity_row else self.chatbot.identity
            where = """group_id=? AND type=1 AND timestamp>=? AND timestamp<?
                AND (?='' OR sender<>?) AND COALESCE(json_extract(raw_json,'$.is_self'),'') NOT IN (1,'1','true')
                AND NOT (?<>'' AND sender IN ('',?) AND (instr(content,?||char(58,10))=1 OR instr(content,?||char(58,13,10))=1))"""
            args = (group, int(datetime.combine(start, datetime.min.time(), CHINA).timestamp()),
                    int(datetime.combine(end, datetime.min.time(), CHINA).timestamp()), identity, identity, identity, group, identity, identity)
            total = db.execute("SELECT count(*) FROM chat_messages WHERE " + where, args).fetchone()[0]
            rows = db.execute("SELECT id,sender,timestamp,substr(content,1,1200) AS content FROM chat_messages WHERE " + where + " ORDER BY timestamp DESC,id DESC LIMIT ?", (*args, limit))
            return total, list(reversed([dict(row) for row in rows]))
        return await self.db(read)

    async def report(self, group, kind, force=False, automatic=False):
        start, end = report_period(kind)
        async with self._generation_lock:
            if not self.groups.allowed(group) or (automatic and not self.automatic_report_allowed(group)):
                raise ModelError("此群的 AI 报表已暂停。")
            cached = await self.db(lambda db: db.execute("SELECT * FROM group_reports WHERE group_id=? AND kind=? AND start_date=?", (group, kind, str(start))).fetchone())
            if cached and not force:
                return dict(cached)
            count, messages = await self.text_window(group, start, end)
            label = "周报" if kind == "week" else "月报"
            prompt, sampled_count = bounded_records(messages)
            if not count:
                content = "本期暂无可总结的成员文本消息。"
            else:
                if not self.models.settings:
                    raise ModelError("AI 模型尚未配置，无法生成群聊报表。")
                system = DATA_SYSTEM + f"生成中文{label}，范围 {start} 至 {end - timedelta(days=1)}。包含主要话题、明确结论、待办和未解决问题，缺失项省略。最多 600 字。样本 {sampled_count} 条，总计 {count} 条。"
                if not self.groups.allowed(group):
                    raise ModelError("此群的 AI 报表已暂停。")
                with ai_permission(lambda: self.groups.allowed(group) and (not automatic or self.automatic_report_allowed(group))):
                    content = (await self.models.answer(prompt, system=system, group=group)).strip()[:6000]
                if not content:
                    raise ModelError("模型未返回有效报表。")
            result = dict(group_id=group, kind=kind, start_date=str(start), end_date=str(end - timedelta(days=1)), content=content, message_count=count, sampled_count=sampled_count, updated_at=now())
            await self.db(lambda db: db.execute("INSERT INTO group_reports VALUES (:group_id,:kind,:start_date,:end_date,:content,:message_count,:sampled_count,:updated_at) ON CONFLICT(group_id,kind,start_date) DO UPDATE SET content=excluded.content,message_count=excluded.message_count,sampled_count=excluded.sampled_count,updated_at=excluded.updated_at", result))
            return result

    async def scheduled_reports(self):
        if not self.models.settings or not self.chatbot.options.enabled:
            return
        groups = await self.db(lambda db: [row[0] for row in db.execute("SELECT group_id FROM group_tool_preferences WHERE auto_reports=1")])
        for group in groups:
            if self.automatic_report_allowed(group):
                for kind in ("week", "month"):
                    try:
                        await self.report(group, kind, automatic=True)
                    except Exception:
                        self.last_error = "自动报表生成失败，将稍后重试。"

    async def due_reminders(self):
        await self.complete_due_tasks()
        today = datetime.now(CHINA).date().isoformat()
        groups = await self.db(lambda db: [row[0] for row in db.execute("SELECT group_id FROM group_tool_preferences WHERE task_reminders=1")])
        for group in groups:
            if not self.groups.allowed(group):
                continue
            if not await self.chatbot.verify_account():
                continue
            def claim(db):
                pref = db.execute("SELECT task_reminders FROM group_tool_preferences WHERE group_id=?", (group,)).fetchone()
                if not pref or not pref[0]:
                    return []
                tasks = [dict(row) for row in db.execute("""SELECT id,title,owner,due_date FROM group_tasks WHERE group_id=?
                    AND (status='open' OR (status='done' AND completion_reason='deadline'))
                    AND reminder_id IS NULL AND digest_attempted_at='' AND due_at<=? ORDER BY due_at LIMIT 20""", (group, datetime.now(CHINA).timestamp()))]
                if not tasks:
                    return []
                result = db.execute("INSERT INTO group_tool_reminders VALUES (?,?,'sending') ON CONFLICT(group_id) DO UPDATE SET reminder_date=excluded.reminder_date,status='sending' WHERE reminder_date<>excluded.reminder_date", (group, today)).rowcount
                if result:
                    db.executemany("UPDATE group_tasks SET digest_attempted_at=? WHERE id=?", [(now(), task["id"]) for task in tasks])
                return tasks if result else []
            tasks = await self.db(claim)
            if not tasks:
                continue
            status = "skipped"
            if self.groups.allowed(group):
                claimed_ids = tuple(item["id"] for item in tasks)
                async def current_text():
                    def read(db):
                        pref = db.execute("SELECT task_reminders FROM group_tool_preferences WHERE group_id=?", (group,)).fetchone()
                        if not pref or not pref[0]:
                            return []
                        return [dict(row) for row in db.execute("""SELECT title,owner,due_date FROM group_tasks
                            WHERE group_id=? AND id IN (""" + ",".join("?" for _ in claimed_ids) + """)
                            AND reminder_id IS NULL AND due_at<=?
                            AND (status='open' OR (status='done' AND completion_reason='deadline'))
                            ORDER BY due_at,id""", (group, *claimed_ids, datetime.now(CHINA).timestamp()))]
                    current = await self.db(read)
                    if not current:
                        return None
                    return ("待办到期提醒（已自动完成）：\n" + "\n".join(
                        f"{item['title']}（{item['owner'] or '未分配'} / {item['due_date']}）" for item in current))[:3500]
                try:
                    await self.chatbot._messenger.text(group, "", build_text=current_text,
                        allowed=lambda: self.groups.allowed(group))
                    status = "sent"
                except DeliveryBlocked:
                    status = "skipped"
                except Exception:
                    status = "unknown"
                    self.last_error = "待办提醒发送未确认，今日不会自动重发。"
            await self.db(lambda db: db.execute("UPDATE group_tool_reminders SET status=? WHERE group_id=? AND reminder_date=?", (status, group, today)))

    async def _run_schedule(self):
        while True:
            try:
                await self.scheduled_reports()
                if datetime.now(CHINA).hour >= 9:
                    await self.due_reminders()
            except Exception:
                self.last_error = "无法读取自动报表配置。"
            await asyncio.sleep(60)

    async def save_rule(self, group, body, rule_id=None):
        rule_id = rule_id or uuid4().hex
        def save(db):
            existing = db.execute("SELECT 1 FROM keyword_rules WHERE group_id=? AND id=?", (group, rule_id)).fetchone()
            if not existing and db.execute("SELECT count(*) FROM keyword_rules WHERE group_id=?", (group,)).fetchone()[0] >= 50:
                raise ValueError("每群最多保存 50 条规则。")
            db.execute("""INSERT INTO keyword_rules(id,group_id,name,keyword,match_mode,action,reply,enabled,cooldown)
                VALUES (:id,:group_id,:name,:keyword,:match_mode,:action,:reply,:enabled,:cooldown)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,keyword=excluded.keyword,match_mode=excluded.match_mode,
                action=excluded.action,reply=excluded.reply,enabled=excluded.enabled,cooldown=excluded.cooldown""", {**body, "id": rule_id, "group_id": group})
            self.groups._audit(db, group, body["name"], "保存关键词规则")
        await self.db(save)
        return rule_id

    async def delete_rule(self, group, rule_id):
        def delete(db):
            db.execute("DELETE FROM keyword_rules WHERE group_id=? AND id=?", (group, rule_id))
            self.groups._audit(db, group, rule_id, "删除关键词规则")
        await self.db(delete)

    async def receive(self, event):
        group = event_group(event)
        if not group or event.type != 1 or self.chatbot.own_message(event) or not self.groups.allowed(group, event.sender):
            return
        rules = await self.db(lambda db: [dict(row) for row in db.execute("SELECT * FROM keyword_rules WHERE group_id=? AND enabled=1 ORDER BY name,id", (group,))])
        if not rules:
            return
        # Identification is required even for an account that only uses keyword replies.
        if not await self.chatbot.verify_account() or self.chatbot.own_message(event):
            return
        identity = self.chatbot.identity
        if identity and event.sender in ("", group) and event.content.startswith((identity + ":\n", identity + ":\r\n")):
            return
        for rule in rules:
            text = event.content.removeprefix(event.sender + ":\n").strip()
            keyword = rule["keyword"].casefold()
            if not (text.casefold() == keyword if rule["match_mode"] == "exact" else keyword in text.casefold()):
                continue
            key = str(event.msgid) if str(event.msgid) not in ("", "0") else hashlib.sha256(f"{event.sender}:{event.timestamp}:{text}".encode()).hexdigest()
            try:
                self._queue.put_nowait((group, rule["id"], key, event.sender, text[:1000]))
            except asyncio.QueueFull:
                self.last_error = "关键词处理队列已满。"
            return True  # A message triggers at most one rule.

    async def _process_rule(self, job):
        group, rule_id, key, sender, text = job
        if not self.groups.allowed(group, sender):
            return
        def claim(db):
            row = db.execute("SELECT * FROM keyword_rules WHERE group_id=? AND id=? AND enabled=1", (group, rule_id)).fetchone()
            if not row:
                return None
            rule = dict(row)
            if rule["last_triggered"] and (datetime.now(CHINA) - datetime.fromisoformat(rule["last_triggered"])).total_seconds() < rule["cooldown"]:
                return None
            claim_key = "keyword:" + rule_id + ":" + key
            if not db.execute("INSERT OR IGNORE INTO chat_reply_claims VALUES (?,?,'keyword',?,?)", (group, claim_key, now(), now())).rowcount:
                return None
            inserted = db.execute("INSERT OR IGNORE INTO keyword_hits(group_id,rule_id,rule_name,message_key,sender,content,status,created_at) VALUES (?,?,?,?,?,?,?,?)", (group, rule_id, rule["name"], key, sender, text, "recorded" if rule["action"] == "record" else "sending", now())).rowcount
            if not inserted:
                return None
            db.execute("UPDATE keyword_rules SET last_triggered=? WHERE id=?", (now(), rule_id))
            db.execute("DELETE FROM keyword_hits WHERE group_id=? AND id NOT IN (SELECT id FROM keyword_hits WHERE group_id=? ORDER BY id DESC LIMIT 500)", (group, group))
            return rule
        rule = await self.db(claim)
        if not rule or rule["action"] == "record":
            return
        status = "skipped"
        live = await self.db(lambda db: db.execute("SELECT enabled,reply FROM keyword_rules WHERE group_id=? AND id=?", (group, rule_id)).fetchone())
        if live and live["enabled"] and self.groups.allowed(group, sender):
            async def still_enabled():
                current = await self.db(lambda db: db.execute(
                    "SELECT enabled,reply,action FROM keyword_rules WHERE group_id=? AND id=?", (group, rule_id)).fetchone())
                return bool(current and current["enabled"] and current["action"] == "reply"
                            and current["reply"] == live["reply"] and self.groups.allowed(group, sender))
            try:
                await self.chatbot._messenger.text(group, live["reply"], allowed=still_enabled)
                status = "sent"
            except DeliveryBlocked:
                status = "skipped"
            except Exception:
                status = "unknown"
                self.last_error = "关键词回复发送未确认，同一消息不会重发。"
        await self.db(lambda db: db.execute("UPDATE keyword_hits SET status=? WHERE group_id=? AND rule_id=? AND message_key=?", (status, group, rule_id, key)))

    async def _run_rules(self):
        while True:
            job = await self._queue.get()
            try:
                await self._process_rule(job)
            except Exception:
                self.last_error = "关键词规则处理失败。"
            finally:
                self._queue.task_done()

    def record_reminder_task(self, db, reminder):
        member = db.execute("SELECT wechat_name,name FROM members WHERE group_id=? AND id=?", (reminder["group_id"], reminder["sender"])).fetchone()
        name = (member[0] or member[1]) if member else ""
        due_date = datetime.fromtimestamp(reminder["due_at"], CHINA).date().isoformat()
        db.execute("""INSERT OR IGNORE INTO group_tasks
            (id,group_id,title,owner,due_date,status,created_at,updated_at,due_at,creator_id,creator_name,source_kind,reminder_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'reminder',?)""",
            (reminder["id"], reminder["group_id"], reminder["title"], name or reminder["sender"], due_date,
             "cancelled" if reminder["status"] == "cancelled" else "open", reminder["created_at"], reminder["updated_at"],
             reminder["due_at"], reminder["sender"], name or reminder["sender"], reminder["id"]))

    def check_reminder_capacity(self, db, group, sender):
        count = db.execute("SELECT count(*),sum(sender=?) FROM member_reminders WHERE group_id=? AND status='pending'", (sender, group)).fetchone()
        if count[0] >= 200 or (count[1] or 0) >= 50:
            raise ValueError("本群待提醒事项已达上限（每人50条、每群200条），请先取消部分提醒。")

    async def save_task(self, group, body, task_id=None):
        due_date, due_at = task_deadline(body)
        def save(db):
            if task_id is not None:
                task = db.execute("SELECT * FROM group_tasks WHERE group_id=? AND id=?", (group, task_id)).fetchone()
                if not task:
                    return None
                if task["reminder_id"] and due_at is None:
                    raise ValueError("群提醒待办必须保留截止时间。")
                reopen = task["status"] == "done" and task["completion_reason"] == "deadline" and due_at is not None and due_at > datetime.now(CHINA).timestamp()
                if task["reminder_id"]:
                    reminder = db.execute("SELECT * FROM member_reminders WHERE id=?", (task["reminder_id"],)).fetchone()
                    status = reminder["status"]
                    if status == "sending":
                        raise ValueError("提醒正在发送，请稍后修改。")
                    if due_at != task["due_at"] and (task["status"] == "open" or reopen):
                        if due_at <= datetime.now(CHINA).timestamp():
                            raise ValueError("请将群提醒修改为未来的截止时间。")
                        if status != "pending":
                            self.check_reminder_capacity(db, group, reminder["sender"])
                        status = "pending"
                    db.execute("UPDATE member_reminders SET title=?,due_at=?,status=?,updated_at=? WHERE id=?",
                               (body["title"], due_at, status, now(), task["reminder_id"]))
                db.execute("UPDATE group_tasks SET title=?,owner=?,due_date=?,due_at=?,updated_at=? WHERE id=? AND group_id=?",
                           (body["title"], body["owner"], due_date, due_at, now(), task_id, group))
                if due_at != task["due_at"]:
                    db.execute("UPDATE group_tasks SET digest_attempted_at='' WHERE id=?", (task_id,))
                if reopen:
                    db.execute("UPDATE group_tasks SET status='open',completed_at='',completion_reason='' WHERE id=?", (task_id,))
                self.groups._audit(db, group, body["title"], "修改群待办")
                return task_id
            source = body.get("source_id")
            if source is not None and not db.execute("SELECT 1 FROM chat_messages WHERE group_id=? AND id=?", (group, source)).fetchone():
                raise ValueError("待办来源消息不属于当前群。")
            if source is not None:
                existing = db.execute("SELECT id FROM group_tasks WHERE group_id=? AND source_id=? AND title=?", (group, source, body["title"])).fetchone()
                if existing:
                    return existing[0]
            if db.execute("SELECT count(*) FROM group_tasks WHERE group_id=?", (group,)).fetchone()[0] >= 1000:
                raise ValueError("每群最多保存 1000 条待办。")
            new_id, created = uuid4().hex, now()
            db.execute("""INSERT INTO group_tasks
                (id,group_id,title,owner,due_date,status,source_id,created_at,updated_at,due_at,creator_id,creator_name,source_kind)
                VALUES (?,?,?,?,?,'open',?,?,?,?, 'local-admin','本地管理员',?)""",
                (new_id, group, body["title"], body["owner"], due_date, source, created, created, due_at, "ai" if source is not None else "manual"))
            self.groups._audit(db, group, body["title"], "添加群待办")
            return new_id
        return await self.db(save)

    async def complete_due_tasks(self, current=None):
        current = current or datetime.now(CHINA)
        return await self.db(lambda db: db.execute("""UPDATE group_tasks
            SET status='done',completed_at=?,completion_reason='deadline',updated_at=?
            WHERE status='open' AND due_at IS NOT NULL AND due_at<=?""",
            (current.isoformat(), current.isoformat(), current.timestamp())).rowcount)

    async def task_status(self, group, task_id, status):
        def save(db):
            task = db.execute("SELECT * FROM group_tasks WHERE group_id=? AND id=?", (group, task_id)).fetchone()
            if not task:
                return False
            if task["status"] == status and not (status == "done" and task["completion_reason"] == "deadline"):
                return True
            timestamp = now()
            if task["reminder_id"]:
                reminder = db.execute("SELECT * FROM member_reminders WHERE id=?", (task["reminder_id"],)).fetchone()
                if status != "open":
                    db.execute("UPDATE member_reminders SET status='cancelled',updated_at=? WHERE id=? AND status='pending'", (timestamp, task["reminder_id"]))
                elif reminder["status"] == "cancelled" and reminder["due_at"] > datetime.now(CHINA).timestamp():
                    self.check_reminder_capacity(db, group, reminder["sender"])
                    db.execute("UPDATE member_reminders SET status='pending',updated_at=? WHERE id=?", (timestamp, task["reminder_id"]))
            result = db.execute("UPDATE group_tasks SET status=?,updated_at=?,completed_at=?,completion_reason=? WHERE group_id=? AND id=?",
                                (status, timestamp, timestamp if status == "done" else "", "manual" if status == "done" else "", group, task_id)).rowcount
            if result:
                self.groups._audit(db, group, task_id, "更新待办状态 · " + status)
            return bool(result)
        return await self.db(save)

    async def extract_tasks(self, group):
        if not self.groups.allowed(group):
            raise ModelError("此群的 AI 待办提取已暂停。")
        today = datetime.now(CHINA).date()
        _, messages = await self.text_window(group, today - timedelta(days=6), today + timedelta(days=1), limit=200)
        if not messages:
            return []
        if not self.models.settings:
            raise ModelError("AI 模型尚未配置，无法提取待办。")
        payload = [{**item, "content": item["content"][:400], "time": datetime.fromtimestamp(item["timestamp"], CHINA).isoformat()} for item in messages]
        prompt, _ = bounded_records(payload)
        system = DATA_SYSTEM + '从最近 7 天群聊记录中提取明确待办，最多 10 条。今天是 ' + str(today) + '。只输出 JSON 数组，每项包含 title（简短任务）, owner（明确负责人，没有则为空字符串）, due_date（明确截止日，YYYY-MM-DD，没有则为空字符串）, source_id（原始消息 id）。'
        if not self.groups.allowed(group):
            raise ModelError("此群的 AI 待办提取已暂停。")
        with ai_permission(lambda: self.groups.allowed(group)):
            answer = (await self.models.answer(prompt, system=system, group=group)).strip()
        answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer)
        try:
            raw = json.loads(answer)
        except (TypeError, ValueError):
            raise ModelError("模型未返回有效待办列表，请重试。") from None
        if not isinstance(raw, list):
            raise ModelError("模型未返回有效待办列表，请重试。")
        sources = {item["id"]: item for item in json.loads(prompt)}
        candidates = []
        for item in raw[:10]:
            if not isinstance(item, dict) or type(item.get("source_id")) is not int or item["source_id"] not in sources:
                continue
            if not isinstance(item.get("title"), str) or not item["title"].strip():
                continue
            due = item.get("due_date") or ""
            try:
                if due:
                    due = date.fromisoformat(due).isoformat()
            except (TypeError, ValueError):
                due = ""
            candidates.append({"title": item["title"].strip()[:200], "owner": str(item.get("owner") or "")[:80], "due_date": due,
                               "source_id": item["source_id"], "source_content": sources[item["source_id"]]["content"]})
        return candidates

    async def chat_command(self, question, group):
        command = re.sub(r"\s+", "", question).rstrip("。！!？?")
        if command in ("周报", "月报"):
            report = await self.report(group, "week" if command == "周报" else "month")
            return f"{command}（{report['start_date']} 至 {report['end_date']}）\n{report['content']}"
        if command in ("待办", "本群待办", "到期待办"):
            data = await self.snapshot(group)
            tasks = [task for task in data["tasks"] if task["status"] == "open" and (command != "到期待办" or (task["due_at"] is not None and task["due_at"] <= datetime.now(CHINA).timestamp()))]
            return "本群待办：\n" + "\n".join(f"{i+1}. {task['title']}（{task['owner'] or '未分配'} / {datetime.fromtimestamp(task['due_at'], CHINA).strftime('%m-%d %H:%M:%S') if task['due_at'] is not None else '无截止日'}）" for i, task in enumerate(tasks[:20])) if tasks else "本群暂无符合条件的待办。"
        return None
