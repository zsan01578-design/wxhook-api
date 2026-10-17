from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

from wxhook_api.core.config import PROJECT_ROOT
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.word_cloud import frequent_words


GROUP_PATTERN = r"[A-Za-z0-9_-]{1,128}@chatroom"


def event_group(event: CallbackEvent) -> str:
    group = event.roomid or (event.wxid if event.wxid.endswith("@chatroom") else "")
    return group if re.fullmatch(GROUP_PATTERN, group) else ""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GroupManager:
    """Durable local group policies. Hot-path policy checks never touch disk."""

    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.getenv("WXHOOK_MANAGEMENT_DB", str(PROJECT_ROOT / "data" / "management.sqlite3")))
        self._lock = asyncio.Lock()
        self._policies: dict[str, dict] = {}
        self._blocked_members: set[tuple[str, str]] = set()
        self._word_cloud_cache: OrderedDict[str, tuple[str, dict]] = OrderedDict()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def start(self, legacy_groups: list[str]) -> None:
        def initialize():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS groups (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                        pinned INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
                        blocked INTEGER NOT NULL DEFAULT 0, douyin_enabled INTEGER, auto_download INTEGER,
                        created_at TEXT NOT NULL, last_seen TEXT NOT NULL DEFAULT '',
                        last_message TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS members (
                        group_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                        blocked INTEGER NOT NULL DEFAULT 0, last_seen TEXT NOT NULL DEFAULT '',
                        message_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, id)
                    );
                    CREATE TABLE IF NOT EXISTS audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
                        target TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, msg_key TEXT NOT NULL,
                        msgid TEXT NOT NULL, sender TEXT NOT NULL, type INTEGER NOT NULL,
                        timestamp INTEGER NOT NULL, content TEXT NOT NULL, received_at TEXT NOT NULL,
                        source TEXT NOT NULL, raw_json TEXT NOT NULL,
                        UNIQUE(group_id, msg_key)
                    );
                    CREATE INDEX IF NOT EXISTS chat_messages_timeline ON chat_messages(group_id, timestamp DESC, id DESC);
                    CREATE TABLE IF NOT EXISTS sync_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS chat_reply_claims (
                        group_id TEXT NOT NULL, message_key TEXT NOT NULL, status TEXT NOT NULL,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY (group_id, message_key)
                    );
                """)
                migrations = {
                    "groups": {"chat_enabled": "INTEGER", "wechat_name": "TEXT NOT NULL DEFAULT ''", "members_sync": "TEXT NOT NULL DEFAULT 'pending'",
                               "members_synced_at": "TEXT NOT NULL DEFAULT ''", "history_complete": "INTEGER NOT NULL DEFAULT 0",
                               "history_next_page": "INTEGER NOT NULL DEFAULT 1", "history_error": "TEXT NOT NULL DEFAULT ''",
                               "history_synced_at": "TEXT NOT NULL DEFAULT ''"},
                    "members": {"wechat_name": "TEXT NOT NULL DEFAULT ''", "membership": "TEXT NOT NULL DEFAULT 'unknown'"},
                }
                for table, fields in migrations.items():
                    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                    for column, definition in fields.items():
                        if column not in columns:
                            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                for group in legacy_groups:
                    db.execute("INSERT OR IGNORE INTO groups (id, created_at) VALUES (?, ?)", (group, now()))
        async with self._lock:
            await asyncio.to_thread(initialize)
            await asyncio.to_thread(self._reload)

    def _reload(self):
        with self._connect() as db:
            policies = {row["id"]: dict(row) for row in db.execute("SELECT * FROM groups")}
            blocked = {(row["group_id"], row["id"]) for row in db.execute("SELECT group_id, id FROM members WHERE blocked=1")}
        self._policies = policies
        self._blocked_members = blocked

    def allowed(self, group: str, sender: str = "") -> bool:
        policy = self._policies.get(group, {})
        return bool(policy.get("enabled", True) and not policy.get("blocked") and (group, sender) not in self._blocked_members)

    def feature(self, group: str, name: str, fallback: bool) -> bool:
        value = self._policies.get(group, {}).get(name)
        return fallback if value is None else bool(value)

    async def receive(self, event: CallbackEvent) -> None:
        group = event_group(event)
        if not group:
            return
        def record():
            with self._connect() as db:
                message_id = str(event.msgid)
                self._save_messages(db, group, [{"key": f"server:{message_id}" if message_id not in {"", "0"} else f"callback:{uuid4().hex}",
                    "msgid": message_id, "sender": event.sender, "type": event.type,
                    "timestamp": event.timestamp, "content": event.content, "source": "callback", "raw": event.model_dump()}])
        async with self._lock:
            await asyncio.to_thread(record)

    @staticmethod
    def _audit(db, group: str, target: str, action: str):
        db.execute("INSERT INTO audit (group_id, target, action, created_at) VALUES (?, ?, ?, ?)", (group, target, action, now()))
        db.execute("DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 500)")

    async def groups(self, legacy) -> list[dict[str, Any]]:
        def read():
            with self._connect() as db:
                return [dict(row) for row in db.execute("""SELECT g.*,
                    (SELECT count(*) FROM members m WHERE m.group_id=g.id) AS member_count,
                    (SELECT count(*) FROM members m WHERE m.group_id=g.id AND m.blocked=1) AS blocked_member_count
                    , (SELECT count(*) FROM members m WHERE m.group_id=g.id AND m.membership='current') AS current_member_count
                    FROM groups g ORDER BY pinned DESC, last_seen DESC, created_at DESC, id""")]
        async with self._lock:
            rows = await asyncio.to_thread(read)
        for row in rows:
            row["local_name"] = row["name"]
            row["name"] = row["name"] or row["wechat_name"]
            row["history_complete"] = bool(row["history_complete"])
            for key in ("pinned", "enabled", "blocked"):
                row[key] = bool(row[key])
            row["douyin_enabled"] = self.feature(row["id"], "douyin_enabled", legacy.enabled and row["id"] in legacy.groups)
            row["chat_enabled"] = self.feature(row["id"], "chat_enabled", row["douyin_enabled"])
            row["auto_download"] = self.feature(row["id"], "auto_download", legacy.auto_download)
            row["active"] = row["enabled"] and not row["blocked"]
        return rows

    async def add_group(self, group: str, name: str) -> bool:
        def write():
            with self._connect() as db:
                inserted = db.execute("INSERT OR IGNORE INTO groups (id, name, created_at) VALUES (?, ?, ?)", (group, name, now())).rowcount
                if inserted:
                    self._audit(db, group, name or group, "添加群聊")
                return bool(inserted)
        async with self._lock:
            added = await asyncio.to_thread(write)
            await asyncio.to_thread(self._reload)
            return added

    async def claim_chat_reply(self, group: str, key: str) -> bool:
        def claim():
            with self._connect() as db:
                return bool(db.execute("INSERT OR IGNORE INTO chat_reply_claims (group_id,message_key,status,created_at,updated_at) VALUES (?,?,'claimed',?,?)", (group, key, now(), now())).rowcount)
        async with self._lock:
            return await asyncio.to_thread(claim)

    async def finish_chat_reply(self, group: str, key: str, status: str) -> None:
        def finish():
            with self._connect() as db:
                db.execute("UPDATE chat_reply_claims SET status=?,updated_at=? WHERE group_id=? AND message_key=?", (status, now(), group, key))
        async with self._lock:
            await asyncio.to_thread(finish)

    async def update_group(self, group: str, changes: dict) -> bool:
        labels = {"name": "修改群名称", "note": "修改群备注", "pinned": "置顶群聊", "enabled": "群功能总开关", "blocked": "群聊黑名单", "douyin_enabled": "抖音解析", "auto_download": "自动保存媒体", "chat_enabled": "@机器人问答"}
        assert changes.keys() <= labels.keys()

        def write():
            with self._connect() as db:
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
        async with self._lock:
            updated = await asyncio.to_thread(write)
            await asyncio.to_thread(self._reload)
            return updated

    async def members(self, group: str | None = None, *, blocked_only: bool = False) -> list[dict]:
        def read():
            clauses, parameters = [], []
            if group is not None:
                clauses.append("m.group_id=?")
                parameters.append(group)
            if blocked_only:
                clauses.append("m.blocked=1")
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            with self._connect() as db:
                rows = db.execute("SELECT m.*, COALESCE(NULLIF(g.name,''),g.wechat_name) AS group_name FROM members m JOIN groups g ON g.id=m.group_id" + where + " ORDER BY m.blocked DESC, m.last_seen DESC, m.id", parameters)
                return [{**dict(row), "local_name": row["name"], "name": row["name"] or row["wechat_name"], "blocked": bool(row["blocked"])} for row in rows]
        async with self._lock:
            return await asyncio.to_thread(read)

    async def save_member(self, group: str, member: str, changes: dict, *, create: bool = False) -> bool:
        assert changes.keys() <= {"name", "blocked"}

        def write():
            with self._connect() as db:
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
        async with self._lock:
            updated = await asyncio.to_thread(write)
            await asyncio.to_thread(self._reload)
            return updated

    async def audit(self, group: str | None = None) -> list[dict]:
        def read():
            with self._connect() as db:
                rows = db.execute("SELECT * FROM audit" + (" WHERE group_id=?" if group else "") + " ORDER BY id DESC LIMIT 100", (group,) if group else ())
                return [dict(row) for row in rows]
        async with self._lock:
            return await asyncio.to_thread(read)

    @staticmethod
    def _save_messages(db, group: str, messages: list[dict]) -> tuple[int, int]:
        db.execute("INSERT OR IGNORE INTO groups (id, created_at) VALUES (?, ?)", (group, now()))
        added, unseen_history = 0, 0
        for message in messages:
            if message["source"] == "history":
                existing = db.execute("SELECT source FROM chat_messages WHERE group_id=? AND msg_key=?", (group, message["key"])).fetchone()
                if not existing or existing["source"] != "history":
                    unseen_history += 1
            timestamp = int(message["timestamp"]) or int(datetime.now(timezone.utc).timestamp())
            timestamp = max(0, min(timestamp, 253402300799))
            seen_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            values = (group, message["key"], message["msgid"], message["sender"], message["type"], timestamp,
                      message["content"], now(), message["source"], json.dumps(message["raw"], ensure_ascii=False))
            inserted = db.execute("""INSERT OR IGNORE INTO chat_messages
                (group_id,msg_key,msgid,sender,type,timestamp,content,received_at,source,raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", values).rowcount
            if not inserted:
                if message["source"] == "history":
                    db.execute("""UPDATE chat_messages SET raw_json=?, source='history', type=?, timestamp=?,
                        sender=CASE WHEN ?<>'' THEN ? ELSE sender END,
                        content=CASE WHEN ?<>'' THEN ? ELSE content END WHERE group_id=? AND msg_key=?""",
                        (values[-1], message["type"], timestamp, message["sender"], message["sender"], message["content"], message["content"], group, message["key"]))
                    sender = message["sender"]
                    if sender and sender != group and len(sender) <= 256:
                        db.execute("""INSERT INTO members (group_id,id,last_seen) VALUES (?,?,?)
                            ON CONFLICT(group_id,id) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)""", (group, sender, seen_at))
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
                    db.execute("UPDATE members SET membership='left' WHERE group_id=? AND membership='current'", (group,))
                    for member in people:
                        db.execute("""INSERT INTO members (group_id,id,wechat_name,membership) VALUES (?,?,?,'current')
                            ON CONFLICT(group_id,id) DO UPDATE SET wechat_name=excluded.wechat_name,membership='current'""", (group, member["id"], member["name"]))
        async with self._lock:
            await asyncio.to_thread(write)

    async def claim_account(self, account: str) -> None:
        def write():
            with self._connect() as db:
                row = db.execute("SELECT value FROM sync_metadata WHERE key='account'").fetchone()
                if row and row[0] != account:
                    raise ValueError("微信账号与本地资料库不一致，请为新账号设置独立的 WXHOOK_MANAGEMENT_DB 后重启")
                db.execute("INSERT OR IGNORE INTO sync_metadata (key,value) VALUES ('account',?)", (account,))
        async with self._lock:
            await asyncio.to_thread(write)

    async def history_checkpoint(self, group: str, *, next_page: int, complete: bool, error: str = "") -> None:
        def write():
            with self._connect() as db:
                db.execute("UPDATE groups SET history_next_page=?,history_complete=?,history_error=?,history_synced_at=? WHERE id=?",
                           (next_page, complete, error, now() if not error else "", group))
        async with self._lock:
            await asyncio.to_thread(write)

    async def history_failure(self, group: str, error: str) -> None:
        def write():
            with self._connect() as db:
                db.execute("UPDATE groups SET history_error=? WHERE id=?", (error, group))
        async with self._lock:
            await asyncio.to_thread(write)

    async def import_history(self, group: str, rows: list[dict]) -> tuple[int, int]:
        normalized = []
        for row in rows:
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
            if not sender:
                prefix = re.match(r"^([A-Za-z0-9_.-]{1,256}):\n", content)
                if prefix:
                    sender = prefix[1]
            normalized.append({"key": key, "msgid": server, "sender": sender, "sender_name": str(row.get("sender_nickname") or "")[:256],
                               "type": int(row.get("local_type") or row.get("type") or 0),
                               "timestamp": int(row.get("create_time") or row.get("timestamp") or 0), "content": content, "source": "history", "raw": row})
        def write():
            with self._connect() as db:
                return self._save_messages(db, group, normalized)
        async with self._lock:
            return await asyncio.to_thread(write)

    async def word_cloud(self, group: str, *, bot_wxid: str = "") -> dict:
        def read():
            with self._connect() as db:
                account = db.execute("SELECT value FROM sync_metadata WHERE key='account'").fetchone()
                identity = account[0] if account else bot_wxid
                texts = []
                rows = db.execute("""SELECT substr(content,1,2000) AS content,sender,
                    json_extract(raw_json,'$.is_self') AS is_self FROM chat_messages
                    WHERE group_id=? AND type=1 AND (?='' OR sender<>?)
                    ORDER BY timestamp DESC,id DESC""", (group, identity, identity))
                for row in rows:
                    if row["is_self"] in (True, 1, "1", "true"):
                        continue
                    if identity and row["sender"] in ("", group) and row["content"].startswith((identity + ":\n", identity + ":\r\n")):
                        continue
                    texts.append(row["content"])
                    if len(texts) == 1000:
                        break
                return texts
        async with self._lock:
            texts = await asyncio.to_thread(read)
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

    async def messages(self, group: str, *, page: int = 1, size: int = 50, query: str = "") -> dict:
        def read():
            where, args = "group_id=?", [group]
            if query:
                where += " AND instr(lower(content),lower(?))>0"
                args.append(query)
            with self._connect() as db:
                total = db.execute(f"SELECT count(*) FROM chat_messages WHERE {where}", args).fetchone()[0]
                rows = db.execute(f"SELECT id,msgid,sender,type,timestamp,content,received_at,source FROM chat_messages WHERE {where} ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?", (*args,size,(page-1)*size))
                return {"messages": [dict(row) for row in rows], "total": total, "page": page, "size": size, "has_more": total > page*size}
        async with self._lock:
            return await asyncio.to_thread(read)
