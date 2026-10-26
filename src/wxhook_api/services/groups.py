from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

from wxhook_api.core.config import PROJECT_ROOT
from wxhook_api.core.errors import AccountMismatchError
from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.core.schema import apply_management_schema
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.word_cloud import frequent_words


GROUP_PATTERN = r"[A-Za-z0-9_-]{1,128}@chatroom"
SELF_MESSAGE_FIELDS = ("is_self", "isSelf", "is_sender", "isSender", "IsSender", "is_send", "isSend", "IsSend")
SELF_MESSAGE_SQL = " OR ".join(
    f"COALESCE(lower(trim(CAST(json_extract(raw_json,'$.{field}') AS TEXT))), '') IN ('1','true')"
    for field in SELF_MESSAGE_FIELDS
)


def self_message(raw: dict) -> bool:
    return any(str(raw.get(field, "")).strip().lower() in {"1", "true"} for field in SELF_MESSAGE_FIELDS)


def event_group(event: CallbackEvent) -> str:
    group = event.roomid or (event.wxid if event.wxid.endswith("@chatroom") else "")
    return group if re.fullmatch(GROUP_PATTERN, group) else ""


def event_sender(event: CallbackEvent) -> str:
    group = event_group(event)
    if group and event.sender in ("", group):
        prefix = re.match(r"^([A-Za-z0-9_.-]{1,256}):\r?\n", event.content)
        if prefix:
            return prefix[1]
    return event.sender


def normalize_group_sender(event: CallbackEvent) -> CallbackEvent:
    sender = event_sender(event)
    return event.model_copy(update={"sender": sender}) if sender != event.sender else event


def event_text(event: CallbackEvent) -> str:
    for newline in ("\r\n", "\n"):
        prefix = event.sender + ":" + newline
        if event.content.startswith(prefix):
            return event.content[len(prefix):]
    return event.content


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _day_bounds(summary_date: str) -> tuple[int, int]:
    local = datetime.strptime(summary_date, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
    return int(local.timestamp()), int((local + timedelta(days=1)).timestamp())


class GroupManager:
    """Durable local group policies. Hot-path policy checks never touch disk."""

    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.getenv("WXHOOK_MANAGEMENT_DB", str(PROJECT_ROOT / "data" / "management.sqlite3")))
        self._lock = asyncio.Lock()
        # WAL readers do not block the single writer. Cap concurrent scans so a
        # large radar/word-cloud query cannot open unbounded SQLite handles.
        self._readers = asyncio.Semaphore(2)
        self.fts_available = False
        self._policies: dict[str, dict] = {}
        self._blocked_members: set[tuple[str, str]] = set()
        self._word_cloud_cache: OrderedDict[str, tuple[str, dict]] = OrderedDict()
        self.account_error = ""

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _read_connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            with connection:
                yield connection
        finally:
            connection.close()

    async def read(self, action):
        def execute():
            with self._read_connect() as db:
                return action(db)
        async with self._readers:
            return await asyncio.to_thread(execute)

    async def start(self, legacy_groups: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        def initialize(db):
            apply_management_schema(db)
            migrations = {
                "groups": {"chat_enabled": "INTEGER", "summary_enabled": "INTEGER NOT NULL DEFAULT 0", "wechat_name": "TEXT NOT NULL DEFAULT ''", "members_sync": "TEXT NOT NULL DEFAULT 'pending'",
                           "members_synced_at": "TEXT NOT NULL DEFAULT ''", "history_complete": "INTEGER NOT NULL DEFAULT 0",
                           "history_next_page": "INTEGER NOT NULL DEFAULT 1", "history_error": "TEXT NOT NULL DEFAULT ''",
                           "history_synced_at": "TEXT NOT NULL DEFAULT ''"},
                "members": {"wechat_name": "TEXT NOT NULL DEFAULT ''", "membership": "TEXT NOT NULL DEFAULT 'unknown'"},
                "chat_messages": {"is_self": "INTEGER NOT NULL DEFAULT 0"},
            }
            for table, fields in migrations.items():
                columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, definition in fields.items():
                    if column not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            # Keep the existing count/policy queries covered by small
            # subsets, rather than reading every member's full row.
            db.execute("CREATE INDEX IF NOT EXISTS members_blocked ON members(id) WHERE blocked=1")
            db.execute("CREATE INDEX IF NOT EXISTS members_current ON members(group_id,id) WHERE membership='current'")
            db.execute("CREATE INDEX IF NOT EXISTS chat_messages_self ON chat_messages(group_id,is_self,timestamp)")
            if not db.execute("SELECT 1 FROM sync_metadata WHERE key='is_self_flags_v1'").fetchone():
                # One-time normalization so word clouds and radar no longer
                # extract the same JSON flag from every message row.
                db.execute(f"UPDATE chat_messages SET is_self=1 WHERE is_self=0 AND ({SELF_MESSAGE_SQL})")
                db.execute("INSERT INTO sync_metadata(key,value) VALUES ('is_self_flags_v1',?)", (now(),))
            account_row = db.execute("SELECT value FROM sync_metadata WHERE key='account'").fetchone()
            if account_row and account_row[0]:
                self._backfill_account_self(db, account_row[0])
            for group in legacy_groups:
                db.execute("INSERT OR IGNORE INTO groups (id, created_at) VALUES (?, ?)", (group, now()))
            if not db.execute("SELECT 1 FROM sync_metadata WHERE key='member_message_counts_v1'").fetchone():
                # Repair counts left by earlier sender enrichment without
                # changing member names, membership or blacklist policies.
                db.execute("UPDATE members SET message_count=0")
                db.executemany("UPDATE members SET message_count=? WHERE group_id=? AND id=?",
                    ((row["total"], row["group_id"], row["sender"]) for row in db.execute(
                        "SELECT group_id,sender,count(*) AS total FROM chat_messages GROUP BY group_id,sender")))
                db.execute("INSERT INTO sync_metadata(key,value) VALUES ('member_message_counts_v1',?)", (now(),))
            fts_available = False
            fts_enabled = os.getenv("WXHOOK_FTS_ENABLED", "true").lower() not in {"false", "0", "off"}
            try:
                if not fts_enabled:
                    raise sqlite3.OperationalError("FTS disabled")
                db.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5(
                        content, content='chat_messages', content_rowid='id', tokenize='trigram');
                    CREATE TRIGGER IF NOT EXISTS chat_messages_fts_ai AFTER INSERT ON chat_messages BEGIN
                        INSERT INTO chat_messages_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS chat_messages_fts_ad AFTER DELETE ON chat_messages BEGIN
                        INSERT INTO chat_messages_fts(chat_messages_fts, rowid, content) VALUES('delete', old.id, old.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS chat_messages_fts_au AFTER UPDATE OF content ON chat_messages BEGIN
                        INSERT INTO chat_messages_fts(chat_messages_fts, rowid, content) VALUES('delete', old.id, old.content);
                        INSERT INTO chat_messages_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                """)
                if not db.execute("SELECT 1 FROM sync_metadata WHERE key='fts_backfill_v1'").fetchone():
                    db.execute("INSERT INTO chat_messages_fts(chat_messages_fts) VALUES('rebuild')")
                    db.execute("INSERT INTO sync_metadata(key,value) VALUES ('fts_backfill_v1',?)", (now(),))
                fts_available = True
            except sqlite3.OperationalError:
                # Older SQLite builds may lack FTS5/trigram; search falls back to instr().
                fts_available = False
            return fts_available
        self.fts_available = await self._write_policy(initialize)
        def enable_wal():
            with closing(sqlite3.connect(self.path, timeout=10)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
        await asyncio.to_thread(enable_wal)

    @staticmethod
    def _read_policies(db):
        policies = {row["id"]: dict(row) for row in db.execute("SELECT * FROM groups")}
        blocked = {(row["group_id"], row["id"]) for row in db.execute("SELECT group_id, id FROM members WHERE blocked=1")}
        return policies, blocked

    def _reload(self):
        with self._connect() as db:
            snapshot = self._read_policies(db)
        self._policies, self._blocked_members = snapshot

    async def _write_policy(self, action):
        def write():
            with self._connect() as db:
                result = action(db)
                snapshot = self._read_policies(db)
            return result, snapshot

        async def commit():
            result, snapshot = await asyncio.to_thread(write)
            # Publish on the event loop only after both the snapshot read and
            # transaction commit succeed. Read/commit failure keeps old policy.
            self._policies, self._blocked_members = snapshot
            return result

        # A cancelled request cannot stop a worker thread's database commit.
        # Keep the lock until both disk and the hot-path cache have settled.
        async with self._lock:
            return await finish_before_cancel(commit())

    def allowed(self, group: str, sender: str = "") -> bool:
        policy = self._policies.get(group, {})
        return bool(not self.account_error and policy.get("enabled", True) and not policy.get("blocked") and (group, sender) not in self._blocked_members)

    def suspend_account(self, message: str) -> None:
        # Latch until restart: queued work must not resume under another account.
        self.account_error = message

    def feature(self, group: str, name: str, fallback: bool) -> bool:
        value = self._policies.get(group, {}).get(name)
        return fallback if value is None else bool(value)

    async def receive(self, event: CallbackEvent) -> None:
        group = event_group(event)
        if not group:
            return
        if self.account_error:
            raise AccountMismatchError(self.account_error)
        def record():
            with self._connect() as db:
                message_id = str(event.msgid)
                self._save_messages(db, group, [{"key": f"server:{message_id}" if message_id not in {"", "0"} else f"callback:{event._receipt_key or uuid4().hex}",
                    "msgid": message_id, "sender": event_sender(event), "type": event.type,
                    "timestamp": event.timestamp, "content": event.content, "source": "callback", "raw": event.model_dump()}])
        async with self._lock:
            # Another callback can detect an account change while this one
            # waits for a database writer. Do not archive after suspension.
            if self.account_error:
                raise AccountMismatchError(self.account_error)
            await finish_before_cancel(asyncio.to_thread(record))

    @staticmethod
    def _audit(db, group: str, target: str, action: str):
        db.execute("INSERT INTO audit (group_id, target, action, created_at) VALUES (?, ?, ?, ?)", (group, target, action, now()))
        db.execute("DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 500)")

    async def groups(self, legacy) -> list[dict[str, Any]]:
        def read(db):
            return [dict(row) for row in db.execute("""SELECT g.*,
                (SELECT count(*) FROM members m WHERE m.group_id=g.id) AS member_count,
                (SELECT count(*) FROM members m WHERE m.group_id=g.id AND m.blocked=1) AS blocked_member_count
                , (SELECT count(*) FROM members m WHERE m.group_id=g.id AND m.membership='current') AS current_member_count
                FROM groups g ORDER BY pinned DESC, last_seen DESC, created_at DESC, id""")]
        rows = await self.read(read)
        for row in rows:
            row["local_name"] = row["name"]
            row["name"] = row["name"] or row["wechat_name"]
            row["history_complete"] = bool(row["history_complete"])
            for key in ("pinned", "enabled", "blocked", "summary_enabled"):
                row[key] = bool(row[key])
            row["douyin_enabled"] = self.feature(row["id"], "douyin_enabled", legacy.enabled and row["id"] in legacy.groups)
            row["chat_enabled"] = self.feature(row["id"], "chat_enabled", row["douyin_enabled"])
            row["auto_download"] = self.feature(row["id"], "auto_download", legacy.auto_download)
            row["active"] = not self.account_error and row["enabled"] and not row["blocked"]
        return rows

    async def add_group(self, group: str, name: str) -> bool:
        def write(db):
            inserted = db.execute("INSERT OR IGNORE INTO groups (id, name, created_at) VALUES (?, ?, ?)", (group, name, now())).rowcount
            if inserted:
                self._audit(db, group, name or group, "添加群聊")
            return bool(inserted)
        return await self._write_policy(write)

    async def claim_chat_reply(self, group: str, key: str) -> bool:
        def claim():
            with self._connect() as db:
                return bool(db.execute("INSERT OR IGNORE INTO chat_reply_claims (group_id,message_key,status,created_at,updated_at) VALUES (?,?,'claimed',?,?)", (group, key, now(), now())).rowcount)
        async with self._lock:
            return await finish_before_cancel(asyncio.to_thread(claim))

    async def finish_chat_reply(self, group: str, key: str, status: str) -> None:
        def finish():
            with self._connect() as db:
                db.execute("UPDATE chat_reply_claims SET status=?,updated_at=? WHERE group_id=? AND message_key=?", (status, now(), group, key))
        async with self._lock:
            await finish_before_cancel(asyncio.to_thread(finish))

    async def update_group(self, group: str, changes: dict) -> bool:
        labels = {"name": "修改群名称", "note": "修改群备注", "pinned": "置顶群聊", "enabled": "群功能总开关", "blocked": "群聊黑名单", "douyin_enabled": "抖音解析", "auto_download": "自动保存媒体", "chat_enabled": "@机器人问答", "summary_enabled": "自动 AI 日总结"}
        assert changes.keys() <= labels.keys()

        def write(db):
            row = db.execute("SELECT * FROM groups WHERE id=?", (group,)).fetchone()
            if row is None:
                return False
            for key, value in changes.items():
                if row[key] == value:
                    continue
                db.execute(f"UPDATE groups SET {key}=? WHERE id=?", (value, group))
                status = ("开启" if value else "关闭") if isinstance(value, bool) else "已更新"
                if key == "blocked":
                    status = "已拉黑" if value else "已解除"
                self._audit(db, group, row["name"] or group, f"{labels[key]} · {status}")
            return True
        return await self._write_policy(write)

    async def members(self, group: str | None = None, *, blocked_only: bool = False) -> list[dict]:
        def read(db):
            clauses, parameters = [], []
            if group is not None:
                clauses.append("m.group_id=?")
                parameters.append(group)
            if blocked_only:
                clauses.append("m.blocked=1")
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = db.execute("SELECT m.*, COALESCE(NULLIF(g.name,''),g.wechat_name) AS group_name FROM members m JOIN groups g ON g.id=m.group_id" + where + " ORDER BY m.blocked DESC, m.last_seen DESC, m.id", parameters)
            return [{**dict(row), "local_name": row["name"], "name": row["name"] or row["wechat_name"], "blocked": bool(row["blocked"])} for row in rows]
        return await self.read(read)

    async def save_member(self, group: str, member: str, changes: dict, *, create: bool = False) -> bool:
        assert changes.keys() <= {"name", "blocked"}

        def write(db):
            if not db.execute("SELECT 1 FROM groups WHERE id=?", (group,)).fetchone():
                return False
            row = db.execute("SELECT * FROM members WHERE group_id=? AND id=?", (group, member)).fetchone()
            if create and row is not None:
                return True
            if row is None:
                if not create:
                    return False
                db.execute("INSERT INTO members (group_id, id) VALUES (?, ?)", (group, member))
                self._audit(db, group, member, "添加群成员")
            for key, value in changes.items():
                if row is not None and row[key] == value:
                    continue
                db.execute(f"UPDATE members SET {key}=? WHERE group_id=? AND id=?", (value, group, member))
                action = ("拉黑群成员" if value else "解除成员黑名单") if key == "blocked" else "修改成员备注"
                self._audit(db, group, changes.get("name") or (row["name"] if row else "") or member, action)
            return True
        return await self._write_policy(write)

    async def audit(self, group: str | None = None) -> list[dict]:
        def read(db):
            rows = db.execute("SELECT * FROM audit" + (" WHERE group_id=?" if group else "") + " ORDER BY id DESC LIMIT 100", (group,) if group else ())
            return [dict(row) for row in rows]
        return await self.read(read)

    @staticmethod
    def _save_messages(db, group: str, messages: list[dict]) -> tuple[int, int]:
        db.execute("INSERT OR IGNORE INTO groups (id, created_at) VALUES (?, ?)", (group, now()))
        account_row = db.execute("SELECT value FROM sync_metadata WHERE key='account'").fetchone()
        account = account_row[0] if account_row else ""
        added, unseen_history = 0, 0
        for message in messages:
            existing = None
            if message["source"] == "history":
                existing = db.execute("SELECT source,sender,raw_json FROM chat_messages WHERE group_id=? AND msg_key=?", (group, message["key"])).fetchone()
                if not existing or existing["source"] != "history":
                    unseen_history += 1
            raw = message["raw"]
            own = self_message(raw) or bool(account and message["sender"] == account)
            if existing:
                # A history row with fewer fields must never erase a known
                # self marker from the callback or an earlier history page.
                own = own or self_message(json.loads(existing["raw_json"])) or bool(account and existing["sender"] == account)
            if own:
                raw = {**raw, "is_self": True}
            timestamp = int(message["timestamp"]) or int(datetime.now(timezone.utc).timestamp())
            timestamp = max(0, min(timestamp, 253402300799))
            seen_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            raw_json = json.dumps(raw, ensure_ascii=False)
            values = (group, message["key"], message["msgid"], message["sender"], message["type"], timestamp,
                      message["content"], now(), message["source"], raw_json, int(own))
            inserted = db.execute("""INSERT OR IGNORE INTO chat_messages
                (group_id,msg_key,msgid,sender,type,timestamp,content,received_at,source,raw_json,is_self)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", values).rowcount
            if not inserted:
                if own and message["source"] != "history":
                    # A callback may arrive after its history row. Enrich only
                    # the self marker, preserving the archived text and sender.
                    db.execute("UPDATE chat_messages SET raw_json=json_set(raw_json,'$.is_self',json('true')),is_self=1 WHERE group_id=? AND msg_key=?",
                               (group, message["key"]))
                if message["source"] == "history":
                    db.execute("""UPDATE chat_messages SET raw_json=?, source='history', type=?, timestamp=?,
                        sender=CASE WHEN ?<>'' THEN ? ELSE sender END,
                        content=CASE WHEN ?<>'' THEN ? ELSE content END,
                        is_self=CASE WHEN ? THEN 1 ELSE is_self END WHERE group_id=? AND msg_key=?""",
                        (raw_json, message["type"], timestamp, message["sender"], message["sender"], message["content"],
                         message["content"], int(own), group, message["key"]))
                    sender = message["sender"]
                    sender_changed = bool(sender and sender != existing["sender"])
                    if sender_changed:
                        db.execute("UPDATE members SET message_count=MAX(0,message_count-1) WHERE group_id=? AND id=?",
                                   (group, existing["sender"]))
                    if sender and sender != group and len(sender) <= 256:
                        db.execute("""INSERT INTO members (group_id,id,last_seen,message_count) VALUES (?,?,?,?)
                            ON CONFLICT(group_id,id) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen),
                            message_count=message_count+excluded.message_count""", (group, sender, seen_at, int(sender_changed)))
                        if message.get("sender_name"):
                            db.execute("UPDATE members SET wechat_name=? WHERE group_id=? AND id=?", (message["sender_name"], group, sender))
                continue
            added += 1
            db.execute("""UPDATE groups SET message_count=message_count+1,
                last_message=CASE WHEN last_seen<=? THEN ? ELSE last_message END,
                last_seen=MAX(last_seen,?) WHERE id=?""", (seen_at, message["content"][:300], seen_at, group))
            sender = message["sender"]
            if sender and sender != group and len(sender) <= 256:
                db.execute("""INSERT INTO members (group_id,id,last_seen,message_count) VALUES (?,?,?,1)
                    ON CONFLICT(group_id,id) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen), message_count=message_count+1""", (group, sender, seen_at))
                if message.get("sender_name"):
                    db.execute("UPDATE members SET wechat_name=? WHERE group_id=? AND id=?", (message["sender_name"], group, sender))
        latest = db.execute("SELECT content,timestamp FROM chat_messages WHERE group_id=? ORDER BY timestamp DESC,id DESC LIMIT 1", (group,)).fetchone()
        if latest:
            db.execute("UPDATE groups SET last_message=?,last_seen=? WHERE id=?", (latest["content"][:300], datetime.fromtimestamp(latest["timestamp"], timezone.utc).isoformat(), group))
        return added, unseen_history

    async def sync_contacts(self, groups: list[dict], rosters: dict[str, list[dict]]) -> None:
        def write():
            with self._connect() as db:
                timestamp = now()
                for group in groups:
                    db.execute("""INSERT INTO groups (id,wechat_name,created_at) VALUES (?,?,?)
                        ON CONFLICT(id) DO UPDATE SET wechat_name=excluded.wechat_name""", (group["id"], group["name"], timestamp))
                    status = "complete" if group["id"] in rosters else "unsupported"
                    db.execute("UPDATE groups SET members_sync=?, members_synced_at=? WHERE id=?", (status, timestamp if status == "complete" else "", group["id"]))
                for group, people in rosters.items():
                    incoming = {member["id"] for member in people}
                    current = {row[0] for row in db.execute(
                        "SELECT id FROM members WHERE group_id=? AND membership='current'", (group,))}
                    db.executemany("UPDATE members SET membership='left' WHERE group_id=? AND id=?",
                                   ((group, member) for member in current - incoming))
                    db.executemany("""INSERT INTO members (group_id,id,wechat_name,membership) VALUES (?,?,?,'current')
                        ON CONFLICT(group_id,id) DO UPDATE SET wechat_name=excluded.wechat_name,membership='current'
                        WHERE members.wechat_name<>excluded.wechat_name OR members.membership<>'current'""",
                        ((group, member["id"], member["name"]) for member in people))
        async with self._lock:
            if self.account_error:
                raise AccountMismatchError(self.account_error)
            await finish_before_cancel(asyncio.to_thread(write))

    @staticmethod
    def _backfill_account_self(db, account: str):
        key = "is_self_account:" + account
        if db.execute("SELECT 1 FROM sync_metadata WHERE key=?", (key,)).fetchone():
            return
        db.execute("UPDATE chat_messages SET is_self=1 WHERE is_self=0 AND sender=?", (account,))
        db.execute("""UPDATE chat_messages SET is_self=1 WHERE is_self=0 AND sender IN ('', group_id)
            AND (instr(content, ? || char(58,10))=1 OR instr(content, ? || char(58,13,10))=1)""", (account, account))
        db.execute("INSERT OR IGNORE INTO sync_metadata(key,value) VALUES (?,?)", (key, now()))

    async def claim_account(self, account: str) -> None:
        if self.account_error:
            raise AccountMismatchError(self.account_error)
        def write():
            with self._connect() as db:
                row = db.execute("SELECT value FROM sync_metadata WHERE key='account'").fetchone()
                if row and row[0] != account:
                    raise AccountMismatchError("微信账号与本地资料库不一致，请为新账号设置独立的 WXHOOK_MANAGEMENT_DB 后重启")
                db.execute("INSERT OR IGNORE INTO sync_metadata (key,value) VALUES ('account',?)", (account,))
                self._backfill_account_self(db, account)
        async def verify():
            if self.account_error:
                raise AccountMismatchError(self.account_error)
            try:
                await asyncio.to_thread(write)
            except AccountMismatchError as exc:
                self.suspend_account(str(exc))
                raise

        async with self._lock:
            # Preserve a mismatch discovered by the database thread even if
            # its caller was cancelled while waiting for account verification.
            await finish_before_cancel(verify())

    async def history_checkpoint(self, group: str, *, next_page: int, complete: bool, error: str = "") -> None:
        def write():
            with self._connect() as db:
                db.execute("UPDATE groups SET history_next_page=?,history_complete=?,history_error=?,history_synced_at=? WHERE id=?",
                           (next_page, complete, error, now() if not error else "", group))
        async with self._lock:
            await finish_before_cancel(asyncio.to_thread(write))

    async def history_failure(self, group: str, error: str) -> None:
        def write():
            with self._connect() as db:
                db.execute("UPDATE groups SET history_error=? WHERE id=?", (error, group))
        async with self._lock:
            await finish_before_cancel(asyncio.to_thread(write))

    async def import_history(self, group: str, rows: list[dict]) -> tuple[int, int]:
        if self.account_error:
            raise AccountMismatchError(self.account_error)
        normalized = []
        for row in rows:
            try:
                message_type = int(row.get("local_type") or row.get("type") or 0)
                timestamp = int(row.get("create_time") or row.get("timestamp") or 0)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("微信历史记录的消息类型或时间格式无效，未导入该页") from None
            if not -(2**63) <= message_type <= 2**63 - 1:
                raise ValueError("微信历史记录的消息类型超出支持范围，未导入该页")
            server = str(row.get("server_id") or row.get("msgid") or "0")
            if server == "0":
                server = str(row.get("msgid") or "0")
            if server != "0":
                key = "server:" + server
            elif str(row.get("local_id") or "0") != "0" and row.get("db_name"):
                key = f'local:{row["db_name"]}:{row.get("table_name", "")}:{row["local_id"]}'
            else:
                key = "digest:" + hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            # Shipped Hook history rows are normalized to msgid/timestamp/type/content/sender.
            # Keep support for the raw column names documented by earlier Hook builds.
            content = str(row.get("content") or row.get("message_content") or row.get("compress_content") or "")
            sender = str(row.get("real_sender") or row.get("sender") or "")
            if sender == "0":
                sender = ""
            if sender in ("", group):
                prefix = re.match(r"^([A-Za-z0-9_.-]{1,256}):\r?\n", content)
                if prefix:
                    sender = prefix[1]
            normalized.append({"key": key, "msgid": server, "sender": sender, "sender_name": str(row.get("sender_nickname") or "")[:256],
                               "type": message_type, "timestamp": timestamp, "content": content, "source": "history", "raw": row})
        def write():
            with self._connect() as db:
                return self._save_messages(db, group, normalized)
        async with self._lock:
            if self.account_error:
                raise AccountMismatchError(self.account_error)
            return await finish_before_cancel(asyncio.to_thread(write))

    async def word_cloud(self, group: str, *, bot_wxid: str = "") -> dict:
        def read(db):
            account = db.execute("SELECT value FROM sync_metadata WHERE key='account'").fetchone()
            identity = account[0] if account else bot_wxid
            texts = []
            rows = db.execute(f"""SELECT substr(content,1,2000) AS content,sender FROM chat_messages
                WHERE group_id=? AND type=1 AND (?='' OR sender<>?) AND is_self=0
                AND NOT ({SELF_MESSAGE_SQL})
                ORDER BY timestamp DESC,id DESC""", (group, identity, identity))
            for row in rows:
                if identity and row["sender"] in ("", group) and row["content"].startswith((identity + ":\n", identity + ":\r\n")):
                    continue
                texts.append(row["content"])
                if len(texts) == 1000:
                    break
            return texts
        texts = await self.read(read)
        signature = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
        cached = self._word_cloud_cache.get(group)
        if cached and cached[0] == signature:
            self._word_cloud_cache.move_to_end(group)
            return cached[1]
        words = await asyncio.to_thread(frequent_words, texts)
        result = {"group_id": group, "words": words, "sampled_messages": len(texts), "message_limit": 1000}
        self._word_cloud_cache[group] = (signature, result)
        self._word_cloud_cache.move_to_end(group)
        while len(self._word_cloud_cache) > 32:
            self._word_cloud_cache.popitem(last=False)
        return result

    async def radar(self, group: str, *, bot_wxid: str = "", days: int = 14) -> dict:
        """Return local activity statistics for a group without calling Hook or a model."""
        days = max(1, min(int(days), 30))
        china = timezone(timedelta(hours=8))
        today = datetime.now(china).date()
        start_local = datetime.combine(today - timedelta(days=days - 1), datetime.min.time(), tzinfo=china)
        start_timestamp = int(start_local.timestamp())
        end_timestamp = int((datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=china)).timestamp())

        def read(db):
            account_row = db.execute("SELECT value FROM sync_metadata WHERE key='account'").fetchone()
            identity = account_row[0] if account_row else bot_wxid
            rows = db.execute("""SELECT sender,type,timestamp,content,is_self
                FROM chat_messages WHERE group_id=? AND type<10000 AND timestamp>=? AND timestamp<?
                ORDER BY timestamp ASC,id ASC""", (group, start_timestamp, end_timestamp))
            member_names = {row["id"]: (row["name"] or row["wechat_name"] or row["id"])
                            for row in db.execute("SELECT id,name,wechat_name FROM members WHERE group_id=?", (group,))}
            daily = [{"date": str(today - timedelta(days=offset)), "count": 0, "text_count": 0, "active_members": set()}
                     for offset in range(days - 1, -1, -1)]
            hourly = [0] * 24
            members = {}
            texts = deque(maxlen=1000)
            total = text_total = 0
            for row in rows:
                sender = row["sender"] or ""
                if row["is_self"] in (True, 1, "1", "true") or (identity and sender == identity):
                    continue
                if identity and sender in ("", group) and str(row["content"] or "").startswith((identity + ":\n", identity + ":\r\n")):
                    continue
                timestamp = int(row["timestamp"] or 0)
                # The window starts at Beijing midnight (fixed UTC+8).
                # Bucket integers instead of formatting a date per message.
                day, within_day = divmod(timestamp - start_timestamp, 86400)
                if not 0 <= day < days:
                    continue
                total += 1
                daily[day]["count"] += 1
                hourly[within_day // 3600] += 1
                if sender and sender != group:
                    entry = members.setdefault(sender, {"id": sender, "name": member_names.get(sender, sender), "count": 0, "last_seen": 0})
                    entry["count"] += 1
                    entry["last_seen"] = timestamp  # Rows arrive oldest first.
                    daily[day]["active_members"].add(sender)
                if int(row["type"] or 0) == 1:
                    text_total += 1
                    texts.append(str(row["content"] or "")[:2000])
                    daily[day]["text_count"] += 1
            return {
                "daily": [{**item, "active_members": len(item["active_members"])} for item in daily],
                "hourly": [{"hour": hour, "count": count} for hour, count in enumerate(hourly)],
                "members": [{**item, "last_seen": datetime.fromtimestamp(item["last_seen"], timezone.utc).isoformat()}
                            for item in sorted(members.values(), key=lambda item: (-item["count"], item["name"]))[:10]],
                "active_member_count": len(members),
                "texts": list(texts),
                "total": total,
                "text_total": text_total,
            }

        raw = await self.read(read)
        words = await asyncio.to_thread(frequent_words, raw.pop("texts"))
        peak_hour = max(raw["hourly"], key=lambda item: item["count"]) if raw["hourly"] else {"hour": 0, "count": 0}
        peak_day = max(raw["daily"], key=lambda item: item["count"]) if raw["daily"] else {"date": "", "count": 0}
        return {
            "group_id": group,
            "days": days,
            "from": str(start_local.date()),
            "to": str(today),
            "messages": raw["total"],
            "text_messages": raw["text_total"],
            "active_members": raw["active_member_count"],
            "daily": raw["daily"],
            "hourly": raw["hourly"],
            "top_members": raw["members"],
            "words": words[:8],
            "peak_hour": peak_hour,
            "peak_day": peak_day,
        }

    async def summary_messages(self, group: str, summary_date: str, *, bot_wxid: str = "") -> list[dict]:
        """Return today's text messages in Asia/Shanghai, excluding the bot."""
        def read(db):
            row = db.execute("SELECT value FROM sync_metadata WHERE key='account'").fetchone()
            account = row[0] if row else bot_wxid
            rows = db.execute(f"""SELECT group_id,sender,timestamp,substr(content,1,1200) AS content,is_self
                FROM chat_messages WHERE group_id=? AND type=1
                AND timestamp>=? AND timestamp<? AND (?='' OR sender<>?)
                AND is_self=0 AND NOT ({SELF_MESSAGE_SQL})
                ORDER BY timestamp DESC,id DESC LIMIT 800""", (group, *_day_bounds(summary_date), account, account))
            result = []
            for item in rows:
                if item["is_self"] in (True, 1, "1", "true"):
                    continue
                if account and item["sender"] in ("", group) and item["content"].startswith((account + ":\n", account + ":\r\n")):
                    continue
                result.append(dict(item))
            return list(reversed(result))
        return await self.read(read)

    async def get_summary(self, group: str, summary_date: str) -> dict | None:
        def read(db):
            row = db.execute("SELECT group_id,summary_date,summary,message_count,created_at,updated_at FROM chat_summaries WHERE group_id=? AND summary_date=?", (group, summary_date)).fetchone()
            return dict(row) if row else None
        return await self.read(read)

    async def save_summary(self, group: str, summary_date: str, summary: str, message_count: int) -> dict:
        timestamp = now()
        def write():
            with self._connect() as db:
                db.execute("""INSERT INTO chat_summaries(group_id,summary_date,summary,message_count,created_at,updated_at)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(group_id,summary_date) DO UPDATE SET summary=excluded.summary,message_count=excluded.message_count,updated_at=excluded.updated_at""",
                    (group, summary_date, summary, message_count, timestamp, timestamp))
                row = db.execute("SELECT group_id,summary_date,summary,message_count,created_at,updated_at FROM chat_summaries WHERE group_id=? AND summary_date=?", (group, summary_date)).fetchone()
                return dict(row)
        async with self._lock:
            return await finish_before_cancel(asyncio.to_thread(write))

    async def summary_group_ids(self) -> list[str]:
        def read(db):
            return [row[0] for row in db.execute("SELECT id FROM groups ORDER BY id")]
        return await self.read(read)

    async def messages(self, group: str, *, page: int = 1, size: int = 50, query: str = "") -> dict:
        def read(db):
            where, args = "group_id=?", [group]
            if query:
                if self.fts_available and len(query.strip()) >= 3:
                    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    where += " AND id IN (SELECT rowid FROM chat_messages_fts WHERE chat_messages_fts.content LIKE ? ESCAPE '\\')"
                    args.append("%" + escaped + "%")
                else:
                    where += " AND instr(lower(content),lower(?))>0"
                    args.append(query)
            total = db.execute(f"SELECT count(*) FROM chat_messages WHERE {where}", args).fetchone()[0]
            offset = (page - 1) * size
            # Out-of-range pages are empty; never bind an unbounded Python
            # integer as SQLite's signed 64-bit OFFSET.
            rows = [] if offset >= total else db.execute(f"SELECT id,msgid,sender,type,timestamp,content,received_at,source FROM chat_messages WHERE {where} ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?", (*args,size,offset))
            return {"messages": [dict(row) for row in rows], "total": total, "page": page, "size": size, "has_more": total > page*size}
        return await self.read(read)
