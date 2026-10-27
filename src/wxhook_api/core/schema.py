"""Single management-database schema entry point.

All services share one SQLite file. Base tables live here; column additions
that depend on an older table are still applied by the owning service after
this script, so legacy databases keep upgrading in place.
"""

SCHEMA_VERSION = 1

MANAGEMENT_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS chat_summaries (
    group_id TEXT NOT NULL, summary_date TEXT NOT NULL, summary TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (group_id, summary_date)
);
CREATE TABLE IF NOT EXISTS callback_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, receipt_key TEXT NOT NULL UNIQUE,
    event_json TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS callback_receipts_state ON callback_receipts(state,sequence);
CREATE TABLE IF NOT EXISTS group_reports (
    group_id TEXT NOT NULL, kind TEXT NOT NULL, start_date TEXT NOT NULL,
    end_date TEXT NOT NULL, content TEXT NOT NULL, message_count INTEGER NOT NULL,
    sampled_count INTEGER NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(group_id,kind,start_date)
);
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
CREATE TABLE IF NOT EXISTS member_reminders (
    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, sender TEXT NOT NULL,
    message_key TEXT NOT NULL, title TEXT NOT NULL, due_at REAL NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(group_id,message_key));
CREATE INDEX IF NOT EXISTS member_reminders_due ON member_reminders(status,due_at);
CREATE TABLE IF NOT EXISTS group_favorites (
    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, source_key TEXT NOT NULL,
    source_url TEXT NOT NULL, source_text TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT 'link',
    kind TEXT NOT NULL DEFAULT 'link', tags TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
    assets TEXT NOT NULL DEFAULT '[]', files TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'saving', error TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(group_id,source_key));
CREATE INDEX IF NOT EXISTS group_favorites_recent ON group_favorites(group_id,created_at DESC,id);
CREATE TABLE IF NOT EXISTS private_contacts (
    id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS private_chat_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    personality TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


def apply_management_schema(db) -> None:
    db.executescript(MANAGEMENT_SCHEMA)
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
