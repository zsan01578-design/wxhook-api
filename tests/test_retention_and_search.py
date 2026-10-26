import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_chatbot import context
from tests.test_douyin import GROUP
from wxhook_api.core.douyin_config import GroupSettings
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.retention import RetentionService


@pytest.mark.parametrize("fallback", ["disabled", "missing_module"])
async def test_existing_fts_database_can_fall_back_and_rebuild(tmp_path, monkeypatch, fallback):
    monkeypatch.setenv("WXHOOK_FTS_ENABLED", "true")
    path = tmp_path / "portable.sqlite3"
    manager = GroupManager(path)
    await manager.start([])
    if not manager.fts_available:
        pytest.skip("SQLite FTS5 trigram is unavailable")
    await manager.add_group(GROUP, "兼容群")
    await manager.import_history(GROUP, [
        {"msgid": 1, "timestamp": 1000, "type": 1, "sender": "a", "content": "original message"},
        {"msgid": 2, "timestamp": 1001, "type": 1, "sender": "a", "content": "removed message"},
    ])
    connect = sqlite3.connect

    class WithoutFTS(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "USING fts5" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return super().execute(sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        if fallback == "disabled":
            patch.setenv("WXHOOK_FTS_ENABLED", "false")
        else:
            patch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=WithoutFTS))
        manager = GroupManager(path)
        await manager.start([])
        assert not manager.fts_available
        with manager._connect() as db:
            assert not db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'chat_messages_fts_%'").fetchall()
            assert not db.execute("SELECT 1 FROM sync_metadata WHERE key='fts_backfill_v1'").fetchone()
            db.execute("UPDATE chat_messages SET content='changed message' WHERE msgid='1'")
            db.execute("DELETE FROM chat_messages WHERE msgid='2'")
        await manager.import_history(GROUP, [
            {"msgid": 3, "timestamp": 1002, "type": 1, "sender": "a", "content": "inserted message"},
        ])
        assert (await manager.messages(GROUP, query="inserted"))["total"] == 1

    manager = GroupManager(path)
    await manager.start([])
    assert manager.fts_available
    for query, total in [("original", 0), ("removed", 0), ("changed", 1), ("inserted", 1)]:
        assert (await manager.messages(GROUP, query=query))["total"] == total
    with manager._connect() as db:
        db.execute("INSERT INTO chat_messages_fts(chat_messages_fts, rank) VALUES('integrity-check', 1)")


async def test_fts_search_handles_substrings_wildcards_and_short_queries(tmp_path, monkeypatch):
    monkeypatch.setenv("WXHOOK_FTS_ENABLED", "true")
    manager = GroupManager(tmp_path / "search.sqlite3")
    await manager.start([])
    await manager.add_group(GROUP, "搜索群")
    await manager.import_history(GROUP, [
        {"msgid": 1, "timestamp": 1000, "type": 1, "sender": "a", "content": "今天讨论生图模型和海报"},
        {"msgid": 2, "timestamp": 1001, "type": 1, "sender": "a", "content": "hello world 100% done"},
        {"msgid": 3, "timestamp": 1002, "type": 1, "sender": "a", "content": "ab"},
    ])
    if not manager.fts_available:
        pytest.skip("SQLite FTS5 trigram is unavailable")
    for query in ("生图模型", "生图", "hello", "100%", "%", "ab"):
        result = await manager.messages(GROUP, query=query)
        assert result["total"] >= 1, query
    assert (await manager.messages(GROUP, query="不存在的词语"))["total"] == 0


async def test_retention_prunes_old_terminal_rows_updates_fts_and_reports_stats(tmp_path):
    async with context(tmp_path) as state:
        groups, tools = state.app.state.groups, state.app.state.group_tools
        await groups.add_group(GROUP, "保留群")
        old = datetime.now(timezone.utc) - timedelta(days=10)
        await groups.import_history(GROUP, [{
            "msgid": 1, "timestamp": int((old - timedelta(days=1)).timestamp()),
            "type": 1, "sender": "a", "content": "需要清理的旧消息",
        }])

        def seed(db):
            db.execute("INSERT INTO chat_reply_claims VALUES (?,?,?,?,?)",
                       (GROUP, "old-claim", "done", old.isoformat(), old.isoformat()))
            db.execute("""INSERT INTO group_tasks
                (id,group_id,title,owner,due_date,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                ("old-task", GROUP, "旧任务", "", "", "done", old.isoformat(), old.isoformat()))
            db.execute("""INSERT INTO member_reminders
                (id,group_id,sender,message_key,title,due_at,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                ("old-reminder", GROUP, "a", "old-key", "旧提醒", 0, "sent", old.isoformat(), old.isoformat()))
        await tools.db(seed)

        retention = RetentionService(groups, tools, chat_days=7, claim_days=7, task_days=7, reminder_days=7)
        result = await retention.run_once()
        assert result["deleted"] == {
            "chat_messages": 1, "chat_reply_claims": 1, "group_tasks": 1, "member_reminders": 1,
        }
        database = retention.snapshot()["database"]
        assert database["chat_messages"] == 0
        assert database["chat_reply_claims"] == 0
        assert database["group_tasks"] == 0
        assert database["member_reminders"] == 0
        assert database["size_bytes"] > 0
        assert (await groups.messages(GROUP, query="需要清理的旧消息"))["total"] == 0


async def test_writer_lock_does_not_block_policy_reads(tmp_path):
    manager = GroupManager(tmp_path / "reads.sqlite3")
    await manager.start([])
    await manager.add_group(GROUP, "读写群")
    async with manager._lock:
        rows = await asyncio.wait_for(manager.groups(GroupSettings()), timeout=1)
    assert rows and rows[0]["id"] == GROUP
    with manager._connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


async def test_legacy_self_flags_are_normalized_on_upgrade(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "legacy-self.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, msg_key TEXT NOT NULL,
                msgid TEXT NOT NULL, sender TEXT NOT NULL, type INTEGER NOT NULL,
                timestamp INTEGER NOT NULL, content TEXT NOT NULL, received_at TEXT NOT NULL,
                source TEXT NOT NULL, raw_json TEXT NOT NULL, UNIQUE(group_id, msg_key));
        """)
        db.execute("""INSERT INTO chat_messages
            (group_id,msg_key,msgid,sender,type,timestamp,content,received_at,source,raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (GROUP, "server:1", "1", "a", 1, 1, "机器人消息", "", "history",
             json.dumps({"is_self": " TRUE "})))
    manager = GroupManager(path)
    await manager.start([])
    with manager._connect() as db:
        assert db.execute("SELECT is_self FROM chat_messages").fetchone()[0] == 1
    assert (await manager.word_cloud(GROUP))["sampled_messages"] == 0
