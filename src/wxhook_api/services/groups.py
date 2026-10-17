from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from wxhook_api.core.config import PROJECT_ROOT
from wxhook_api.schemas.events import CallbackEvent


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
                """)
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
        timestamp = now()

        def record():
            with self._connect() as db:
                db.execute("INSERT OR IGNORE INTO groups (id, created_at) VALUES (?, ?)", (group, timestamp))
                db.execute("UPDATE groups SET last_seen=?, last_message=?, message_count=message_count+1 WHERE id=?", (timestamp, event.content[:300], group))
                if event.sender and len(event.sender) <= 256 and event.sender != group:
                    db.execute("""INSERT INTO members (group_id, id, last_seen, message_count) VALUES (?, ?, ?, 1)
                        ON CONFLICT(group_id, id) DO UPDATE SET last_seen=excluded.last_seen, message_count=message_count+1""", (group, event.sender, timestamp))
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
                    FROM groups g ORDER BY pinned DESC, last_seen DESC, created_at DESC, id""")]
        async with self._lock:
            rows = await asyncio.to_thread(read)
        for row in rows:
            for key in ("pinned", "enabled", "blocked"):
                row[key] = bool(row[key])
            row["douyin_enabled"] = self.feature(row["id"], "douyin_enabled", legacy.enabled and row["id"] in legacy.groups)
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

    async def update_group(self, group: str, changes: dict) -> bool:
        labels = {"name": "修改群名称", "note": "修改群备注", "pinned": "置顶群聊", "enabled": "群功能总开关", "blocked": "群聊黑名单", "douyin_enabled": "抖音解析", "auto_download": "自动保存媒体"}
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
                rows = db.execute("SELECT m.*, g.name AS group_name FROM members m JOIN groups g ON g.id=m.group_id" + where + " ORDER BY m.blocked DESC, m.last_seen DESC, m.id", parameters)
                return [{**dict(row), "blocked": bool(row["blocked"])} for row in rows]
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
