import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_chatbot import context
from tests.test_douyin import GROUP
from wxhook_api.core.douyin_config import GroupSettings
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.retention import RetentionService


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
