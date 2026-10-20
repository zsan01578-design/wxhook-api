from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import re
from uuid import uuid4

from wxhook_api.services.chat_summary import CHINA
from wxhook_api.services.douyin_delivery import DeliveryBlocked, DeliveryError
from wxhook_api.services.groups import now


HELP = "请这样设置提醒：提醒我一分钟后喝水、10分钟后提醒我开会、提醒我明天09:30交报告。时间按北京时间，支持1秒至365天后的单次提醒。"
NUMBER = r"(?:\d{1,6}|[零〇一二两三四五六七八九十百千]{1,8}|半)"
PART = re.compile(rf"({NUMBER})\s*(秒钟|秒|分钟|分|小时|钟头|天)")


def number(value: str) -> float:
    if value == "半":
        return 0.5
    if value.isdigit():
        return int(value)
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    total = current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        else:
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
    return total + current


def parse_reminder(question: str, current: datetime) -> tuple[datetime, str] | None:
    text = re.sub(r"^(?:请帮我|帮我|请)\s*", "", question.strip(), count=1)
    leading = re.match(r"提醒(?:我)?\s*", text)
    if leading:
        text = text[leading.end():]
    relative = re.match(rf"((?:{NUMBER}\s*(?:秒钟|秒|分钟|分|小时|钟头|天)\s*)+)(?:以后|之后|后)\s*", text)
    absolute = re.match(r"(今天|明天|后天)?\s*(\d{1,2})[:：](\d{2})\s*", text)
    match = relative or absolute
    if not match:
        if leading:
            raise ValueError(HELP)
        return None
    rest = text[match.end():]
    trailing = re.match(r"提醒(?:我)?\s*", rest)
    if not leading and not trailing:
        return None
    title = (rest[trailing.end():] if trailing else rest).strip(" \t\r\n，,：:")
    if not title or len(title) > 200:
        raise ValueError("请填写1至200字的提醒事项。" + HELP)
    if relative:
        units = {"秒": 1, "秒钟": 1, "分": 60, "分钟": 60, "小时": 3600, "钟头": 3600, "天": 86400}
        seconds = sum(number(value) * units[unit] for value, unit in PART.findall(relative[1]))
        if not 1 <= seconds <= 365 * 86400:
            raise ValueError("请设置1秒至365天后的提醒时间。")
        due = current + timedelta(seconds=seconds)
    else:
        day, hour, minute = absolute.groups()
        try:
            due = current.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        except ValueError:
            raise ValueError("提醒时间无效，请使用00:00至23:59。") from None
        due += timedelta(days={None: 0, "今天": 0, "明天": 1, "后天": 2}[day])
        if day is None and due <= current:
            due += timedelta(days=1)
    if not 1 <= (due - current).total_seconds() <= 365 * 86400:
        raise ValueError("请设置1秒至365天后的提醒时间，不能使用已过去的时间。")
    return due, title


class ReminderService:
    def __init__(self, tools):
        self.tools = tools
        self.chatbot = tools.chatbot
        self._worker = None

    async def start(self):
        def initialize(db):
            db.executescript("""
                CREATE TABLE IF NOT EXISTS member_reminders (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, sender TEXT NOT NULL,
                    message_key TEXT NOT NULL, title TEXT NOT NULL, due_at REAL NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(group_id,message_key));
                CREATE INDEX IF NOT EXISTS member_reminders_due ON member_reminders(status,due_at);
            """)
            db.execute("UPDATE member_reminders SET status='unknown',updated_at=? WHERE status='sending'", (now(),))
            for row in db.execute("SELECT * FROM member_reminders").fetchall():
                self.tools.record_reminder_task(db, row)
        await self.tools.db(initialize)
        self._worker = asyncio.create_task(self._run())

    async def stop(self):
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    async def chat_command(self, question, group, sender, key):
        command = question.strip().rstrip("。！!？?")
        if command in ("我的提醒", "提醒列表"):
            rows = await self.tools.db(lambda db: db.execute(
                "SELECT * FROM member_reminders WHERE group_id=? AND sender=? AND status='pending' ORDER BY due_at LIMIT 20",
                (group, sender)).fetchall())
            return "我的提醒（北京时间）：\n" + "\n".join(
                f"{row['id']} · {datetime.fromtimestamp(row['due_at'], CHINA):%m-%d %H:%M:%S} · {row['title']}" for row in rows
            ) if rows else "你在本群暂无待提醒事项。"
        cancel = re.fullmatch(r"取消提醒\s+([a-f0-9]{12})", command)
        if cancel:
            def cancel_reminder(db):
                changed = db.execute(
                    "UPDATE member_reminders SET status='cancelled',updated_at=? WHERE id=? AND group_id=? AND sender=? AND status='pending'",
                    (now(), cancel[1], group, sender)).rowcount
                if changed:
                    db.execute("UPDATE group_tasks SET status='cancelled',completed_at='',completion_reason='',updated_at=? WHERE group_id=? AND reminder_id=?", (now(), group, cancel[1]))
                return changed
            changed = await self.tools.db(cancel_reminder)
            return "已取消这条提醒。" if changed else "未找到你在本群的待提醒事项，请发送“我的提醒”查看编号。"
        if command == "取消提醒":
            return "请发送“我的提醒”查看编号，再发送“取消提醒 编号”。"
        try:
            if parse_reminder(command, datetime.now(CHINA)) is None:
                return None
            if not sender or sender.endswith("@chatroom") or sender == "notify@all":
                return "未能识别发起人的微信ID，无法创建提醒，请检查消息回调。"
            def save(db):
                existing = db.execute("SELECT * FROM member_reminders WHERE group_id=? AND message_key=?", (group, key)).fetchone()
                if existing:
                    return dict(existing)
                self.tools.check_reminder_capacity(db, group, sender)
                self.tools.check_task_capacity(db, group)
                # Queue and database waits must not consume the requested countdown.
                created_at = now()
                due, title = parse_reminder(command, datetime.fromisoformat(created_at).astimezone(CHINA))
                item = dict(id=uuid4().hex[:12], group_id=group, sender=sender, message_key=key,
                            title=title, due_at=due.timestamp(), status="pending", created_at=created_at, updated_at=created_at)
                db.execute("INSERT INTO member_reminders VALUES (:id,:group_id,:sender,:message_key,:title,:due_at,:status,:created_at,:updated_at)", item)
                self.tools.record_reminder_task(db, item)
                return item
            item = await self.tools.db(save)
            due = datetime.fromtimestamp(item['due_at'], CHINA)
            return f"已记录提醒：{item['title']}\n北京时间 {due:%Y-%m-%d %H:%M:%S}，我会在本群@你。\n编号：{item['id']}"
        except ValueError as exc:
            return str(exc)

    async def due_reminders(self, current: datetime | None = None):
        await self.tools.complete_due_tasks(current)
        if not self.chatbot._accepting:
            return
        timestamp = (current or datetime.now(CHINA)).timestamp()
        rows = await self.tools.db(lambda db: db.execute(
            """SELECT r.* FROM member_reminders r JOIN group_tasks t ON t.reminder_id=r.id
                WHERE r.status='pending' AND (t.status='open' OR (t.status='done' AND t.completion_reason='deadline'))
                AND r.due_at<=? ORDER BY r.due_at LIMIT 100""", (timestamp,)).fetchall())
        if not rows or not await self.chatbot.verify_account():
            return
        for row in rows:
            group, sender = row['group_id'], row['sender']
            members = await self.tools.groups.members(group)
            member = next((item for item in members if item['id'] == sender), {})
            name = (member.get('wechat_name') or member.get('name') or sender).replace("\n", " ").replace("\r", " ")
            def claim(db):
                changed = db.execute("""UPDATE member_reminders SET status='sending',updated_at=?
                    WHERE id=? AND status='pending' AND due_at<=?
                    AND EXISTS (SELECT 1 FROM group_tasks WHERE reminder_id=member_reminders.id
                        AND (status='open' OR (status='done' AND completion_reason='deadline')))""",
                    (now(), row['id'], timestamp)).rowcount
                return db.execute("SELECT * FROM member_reminders WHERE id=?", (row['id'],)).fetchone() if changed else None
            claimed = await self.tools.db(claim)
            if claimed is None:
                continue
            row = claimed
            status = "skipped"
            try:
                task_active = await self.tools.db(lambda db: db.execute("""SELECT 1 FROM group_tasks WHERE reminder_id=?
                    AND (status='open' OR (status='done' AND completion_reason='deadline'))""", (row['id'],)).fetchone())
                if task_active and self.chatbot._accepting and self.chatbot.allowed(group, sender):
                    async def still_active():
                        active = await self.tools.db(lambda db: db.execute("""SELECT 1 FROM group_tasks t
                            JOIN member_reminders r ON r.id=t.reminder_id
                            WHERE r.id=? AND r.status='sending'
                            AND (t.status='open' OR (t.status='done' AND t.completion_reason='deadline'))""", (row['id'],)).fetchone())
                        return bool(active and self.chatbot._accepting and self.chatbot.allowed(group, sender))
                    await self.chatbot._messenger.text(group, f"@{name}\u2005提醒时间到了：{row['title']}", atlist=[sender], allowed=still_active)
                    status = "sent"
            except asyncio.CancelledError:
                status = "unknown"
                raise
            except DeliveryBlocked:
                status = "skipped"
            except DeliveryError as exc:
                status = "unknown" if exc.uncertain else "failed"
                self.tools.last_error = "成员提醒发送未确认或失败，同一提醒不会自动重发。"
            except Exception:
                status = "unknown"
                self.tools.last_error = "成员提醒发送未确认，同一提醒不会自动重发。"
            finally:
                await self.tools.db(lambda db: db.execute(
                    "UPDATE member_reminders SET status=?,updated_at=? WHERE id=?", (status, now(), row['id'])))

    async def _run(self):
        while True:
            try:
                await self.due_reminders()
            except Exception:
                self.tools.last_error = "无法检查成员提醒，将稍后重试。"
            await asyncio.sleep(1)
