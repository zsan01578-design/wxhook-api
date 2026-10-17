import asyncio
import json
import sqlite3

from asgi_lifespan import LifespanManager
import httpx
import pytest

from tests.test_callbacks import EVENT
from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.douyin_config import GroupSettings
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.wechat_sync import SyncError, SyncOptions, WeChatSync


ROOM = "1000@chatroom"


class FakeWeChat:
    """Execute the generated read-only SQL against real SQLite schemas, rather than matching SQL strings."""

    def __init__(self, groups=1, history=9):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE contact (id INTEGER PRIMARY KEY,username TEXT,nick_name TEXT,remark TEXT);
            CREATE TABLE chat_room (id INTEGER PRIMARY KEY,username TEXT);
            CREATE TABLE chatroom_member (room_id INTEGER,member_id INTEGER);
        """)
        self.db.execute("INSERT INTO contact VALUES (1,'wxid_member','微信成员','')")
        self.histories = {}
        for index in range(groups):
            room = f"{1000+index}@chatroom"
            self.db.execute("INSERT INTO contact VALUES (?,?,?,'')", (index+2, room, f"微信群 {index}"))
            self.db.execute("INSERT INTO chat_room VALUES (?,?)", (index+1, room))
            self.db.execute("INSERT INTO chatroom_member VALUES (?,1)", (index+1,))
            self.histories[room] = [self.message(i) for i in range(history, 0, -1)]
        self.account = "wxid_owner"
        self.online = True
        self.fail_page = None
        self.repeat_pages = False
        self.change_account_on_history = False
        self.calls, self.sql = [], []

    @staticmethod
    def message(index):
        return {"db_name": "message_0.db", "table_name": "Msg_123", "local_id": str(index), "server_id": str(9007199254740993+index),
                "local_type": "1", "real_sender": "wxid_member", "create_time": str(1800000000+index), "message_content": f"聊天记录 {index}", "compress_content": ""}

    def respond(self, request):
        self.calls.append(request.url.path)
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/QueryDB/status":
            return httpx.Response(200, json={"IsLogin": int(self.online)})
        if request.url.path == "/GetSelfProfile":
            return httpx.Response(200, json={"wxid": self.account})
        if request.url.path == "/QueryDB/GetAllDBName":
            return httpx.Response(200, json=[{"dbName": "contact.db"}, {"dbName": "message_0.db"}])
        if request.url.path == "/QueryDB/execute":
            assert body["optDbName"] == "contact.db"
            sql = body["SQL"]
            assert sql.startswith(("SELECT ", "PRAGMA table_info(")), "sync may not mutate WeChat"
            self.sql.append(sql)
            return httpx.Response(200, json={"status": 0, "data": [dict(row) for row in self.db.execute(sql)]})
        if request.url.path == "/Message/history":
            page, size = body["page"], body["size"]
            if page == self.fail_page:
                return httpx.Response(200, json={"status": -1, "data": []})
            if self.change_account_on_history:
                self.account = "wxid_other_owner"
            offset = 0 if self.repeat_pages else (page-1)*size
            return httpx.Response(200, json={"status": 0, "wxid": body["wxid"], "page": page, "data": self.histories.get(body["wxid"], [])[offset:offset+size]})
        raise AssertionError("unexpected Hook request: " + request.url.path)


async def build(tmp_path, fake, **options):
    manager = GroupManager(tmp_path / "management.sqlite3")
    await manager.start([])
    client = HookClient("http://hook", transport=httpx.MockTransport(fake.respond))
    sync = WeChatSync(manager, SyncOptions(**{"page_size": 2, "pages_per_group": 2, **options}))
    sync.client = client
    return manager, client, sync


async def test_complete_group_enumeration_has_no_twenty_contact_cap(tmp_path):
    fake = FakeWeChat(groups=503, history=0)
    manager, client, sync = await build(tmp_path, fake)
    try:
        groups, rosters, warnings = await sync._contacts()
        assert len(groups) == 503 and len(rosters) == 503
        assert all(people[0]["id"] == "wxid_member" for people in rosters.values())
        assert warnings == []
        assert any("OFFSET 500" in sql for sql in fake.sql)
        assert "/Contact/search" not in fake.calls
        await manager.sync_contacts(groups, rosters)
        assert len(await manager.groups(GroupSettings())) == 503
    finally:
        await client.aclose()


async def test_history_backfill_resumes_after_restart_and_then_catches_up(tmp_path):
    fake = FakeWeChat(history=9)
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        assert (await manager.messages(ROOM))["total"] == 4
        group = (await manager.groups(GroupSettings()))[0]
        assert not group["history_complete"] and group["history_next_page"] == 3
        # A different manager must resume from the saved cursor, not discard the archive.
        reloaded = GroupManager(manager.path)
        await reloaded.start([])
        restarted = WeChatSync(reloaded, sync.options)
        restarted.client = client
        for _ in range(5):
            await restarted.run_once()
        result = await reloaded.messages(ROOM)
        assert result["total"] == 9
        assert [row["content"] for row in result["messages"]] == [f"聊天记录 {i}" for i in range(9, 0, -1)]
        assert (await reloaded.groups(GroupSettings()))[0]["history_complete"]
        fake.histories[ROOM].insert(0, fake.message(10))
        await restarted.run_once()
        assert (await reloaded.messages(ROOM))["total"] == 10
    finally:
        await client.aclose()


async def test_history_and_callbacks_deduplicate_and_never_replay_automation(tmp_path):
    fake = FakeWeChat(history=1)
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.receive(CallbackEvent.model_validate({**EVENT, "roomid": ROOM, "sender": "wxid_member", "msgid": fake.message(1)["server_id"], "timestamp": 1800000001, "content": "实时内容"}))
        await sync.run_once()
        await sync.run_once()
        result = await manager.messages(ROOM)
        assert result["total"] == 1
        assert result["messages"][0]["source"] == "history"
        assert result["messages"][0]["content"] == "聊天记录 1"
        assert result["messages"][0]["msgid"] == "9007199254740994"
        assert set(fake.calls) <= {"/QueryDB/status", "/GetSelfProfile", "/QueryDB/GetAllDBName", "/QueryDB/execute", "/Message/history"}
    finally:
        await client.aclose()


async def test_large_incremental_gap_survives_overlap_and_already_received_callbacks(tmp_path):
    fake = FakeWeChat(history=1)
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        fake.histories[ROOM] = [fake.message(i) for i in range(12, 0, -1)]
        # The newest two messages are already persisted through callbacks, while the middle gap is missing.
        for i in (11, 12):
            await sync.receive(CallbackEvent.model_validate({**EVENT, "roomid": ROOM, "sender": "wxid_member", "msgid": fake.message(i)["server_id"], "timestamp": 1800000000+i, "content": f"聊天记录 {i}"}))
        for _ in range(12):
            await sync.run_once()
        result = await manager.messages(ROOM)
        assert result["total"] == 12
        assert all(row["source"] == "history" for row in result["messages"])
        assert (await manager.groups(GroupSettings()))[0]["history_next_page"] == 1
    finally:
        await client.aclose()


async def test_sync_preserves_local_policies_and_updates_names_and_membership(tmp_path):
    fake = FakeWeChat(history=0)
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        await manager.update_group(ROOM, {"name": "本地群名", "note": "本地用途", "blocked": True, "douyin_enabled": True})
        await manager.save_member(ROOM, "wxid_member", {"name": "本地成员备注", "blocked": True})
        fake.db.execute("UPDATE contact SET nick_name='微信新群名' WHERE username=?", (ROOM,))
        await sync.run_once()
        group = (await manager.groups(GroupSettings()))[0]
        assert group["name"] == "本地群名" and group["wechat_name"] == "微信新群名"
        assert group["note"] == "本地用途" and group["blocked"] and group["douyin_enabled"]
        member = (await manager.members(ROOM))[0]
        assert member["name"] == "本地成员备注" and member["wechat_name"] == "微信成员" and member["blocked"]
        fake.db.execute("DELETE FROM chatroom_member")
        await sync.run_once()
        group = (await manager.groups(GroupSettings()))[0]
        assert group["members_sync"] == "complete" and group["current_member_count"] == 0
        assert (await manager.members(ROOM))[0]["membership"] == "left"
        assert not manager.allowed(ROOM, "wxid_member")
    finally:
        await client.aclose()


async def test_failed_history_page_does_not_erase_saved_progress(tmp_path):
    fake = FakeWeChat(history=9)
    fake.fail_page = 2
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        group = (await manager.groups(GroupSettings()))[0]
        assert group["history_next_page"] == 2 and not group["history_complete"]
        assert group["history_error"] and (await manager.messages(ROOM))["total"] == 2
        fake.fail_page = None
        for _ in range(5):
            await sync.run_once()
        assert (await manager.messages(ROOM))["total"] == 9
        assert not (await manager.groups(GroupSettings()))[0]["history_error"]
    finally:
        await client.aclose()


async def test_unknown_membership_schema_is_reported_as_partial(tmp_path):
    fake = FakeWeChat(history=1)
    fake.db.execute("DROP TABLE chatroom_member")
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        group = (await manager.groups(GroupSettings()))[0]
        assert group["members_sync"] == "unsupported"
        assert group["history_complete"] and (await manager.messages(ROOM))["total"] == 1
        assert sync.status["warnings"] and sync.status["phase"] == "partial"
    finally:
        await client.aclose()


async def test_unresolved_member_relationship_is_not_misrepresented_as_complete(tmp_path):
    fake = FakeWeChat(history=0)
    fake.db.execute("INSERT INTO chatroom_member VALUES (1,99999)")
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        assert (await manager.groups(GroupSettings()))[0]["members_sync"] == "unsupported"
        assert sync.status["warnings"]
    finally:
        await client.aclose()


async def test_legacy_delimited_roster_is_supported(tmp_path):
    fake = FakeWeChat(history=0)
    fake.db.executescript("DROP TABLE chatroom_member; DROP TABLE chat_room; CREATE TABLE ChatRoom (ChatRoomName TEXT, MemberList TEXT);")
    fake.db.execute("INSERT INTO ChatRoom VALUES (?,?)", (ROOM, "wxid_a\x07wxid_b\x07wxid_a"))
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        assert {member["id"] for member in await manager.members(ROOM)} == {"wxid_a", "wxid_b"}
        assert (await manager.groups(GroupSettings()))[0]["members_sync"] == "complete"
    finally:
        await client.aclose()


async def test_repeated_upstream_page_never_claims_history_is_complete(tmp_path):
    fake = FakeWeChat(history=9)
    fake.repeat_pages = True
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        group = (await manager.groups(GroupSettings()))[0]
        assert not group["history_complete"]
        assert "分页没有推进" in group["history_error"]
    finally:
        await client.aclose()


async def test_changed_account_cannot_pollute_existing_archive(tmp_path):
    fake = FakeWeChat(history=2)
    manager, client, sync = await build(tmp_path, fake)
    try:
        fake.change_account_on_history = True
        with pytest.raises(ValueError, match="账号"):
            await sync.run_once()
        assert (await manager.messages(ROOM))["total"] == 0
        with pytest.raises(ValueError, match="账号"):
            await sync.receive(CallbackEvent.model_validate({**EVENT, "roomid": ROOM}))
        assert (await manager.messages(ROOM))["total"] == 0
    finally:
        await client.aclose()


async def test_background_reconnect_manual_sync_and_offline_history_api(tmp_path):
    fake = FakeWeChat(history=3)
    fake.online = False
    app = create_app(hook_transport=httpx.MockTransport(fake.respond), sync_options=SyncOptions(interval=.04, retry_interval=.01, page_size=2, pages_per_group=2))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async def wait_phase(phase):
            while app.state.wechat_sync.status["phase"] != phase:
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait_phase("waiting"), 1)
        fake.online = True
        assert (await client.post("/api/manage/sync")).status_code == 202
        await asyncio.wait_for(wait_phase("idle"), 2)
        result = await client.get(f"/api/manage/groups/{ROOM}/messages", params={"size": 2})
        assert result.json()["total"] == 3 and result.json()["has_more"]
        assert result.headers["cache-control"] == "no-store"
        assert len((await client.get(f"/api/manage/groups/{ROOM}/messages?page=2&size=2")).json()["messages"]) == 1
        assert (await client.get(f"/api/manage/groups/{ROOM}/messages", params={"q": "记录 2"})).json()["total"] == 1
        assert (await client.get(f"/api/manage/groups/{ROOM}/messages?q=%25")).json()["total"] == 0
        assert (await client.get(f"/api/manage/groups/{ROOM}/messages?page=0")).status_code == 422
        assert (await client.get(f"/api/manage/groups/{ROOM}/messages?size=201")).status_code == 422
        fake.online = False
        await asyncio.wait_for(wait_phase("waiting"), 2)
        assert (await client.get(f"/api/manage/groups/{ROOM}/messages")).json()["total"] == 3
    assert app.state.wechat_sync._task is None
    restarted = create_app(group_manager=GroupManager(app.state.groups.path), sync_options=SyncOptions(enabled=False))
    async with LifespanManager(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://test") as client:
        assert (await client.get(f"/api/manage/groups/{ROOM}/messages")).json()["total"] == 3
        assert (await client.post("/api/manage/sync")).status_code == 409


async def test_old_management_database_is_migrated_without_losing_blacklists(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE groups (id TEXT PRIMARY KEY,name TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',
                pinned INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,blocked INTEGER NOT NULL DEFAULT 0,
                douyin_enabled INTEGER,auto_download INTEGER,created_at TEXT NOT NULL,last_seen TEXT NOT NULL DEFAULT '',
                last_message TEXT NOT NULL DEFAULT '',message_count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE members (group_id TEXT NOT NULL,id TEXT NOT NULL,name TEXT NOT NULL DEFAULT '',blocked INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT NOT NULL DEFAULT '',message_count INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(group_id,id));
        """)
        db.execute("INSERT INTO groups (id,name,blocked,created_at) VALUES (?, '保留群名',1,'2026-01-01')", (ROOM,))
        db.execute("INSERT INTO members (group_id,id,name,blocked) VALUES (?,'wxid_member','保留备注',1)", (ROOM,))
    manager = GroupManager(path)
    await manager.start([])
    assert (await manager.groups(GroupSettings()))[0]["name"] == "保留群名"
    assert (await manager.members(ROOM))[0]["blocked"]
    await manager.import_history(ROOM, [FakeWeChat.message(1)])
    await manager.start([])
    assert (await manager.messages(ROOM))["total"] == 1
    assert not manager.allowed(ROOM, "wxid_member")


async def test_history_enriches_unknown_callback_sender_without_duplicate_messages(tmp_path):
    manager = GroupManager(tmp_path / "management.sqlite3")
    await manager.start([])
    await manager.receive(CallbackEvent.model_validate({**EVENT, "roomid": ROOM, "sender": "", "msgid": FakeWeChat.message(1)["server_id"]}))
    await manager.import_history(ROOM, [FakeWeChat.message(1)])
    assert (await manager.messages(ROOM))["total"] == 1
    assert (await manager.members(ROOM))[0]["id"] == "wxid_member"
