import sqlite3
from contextlib import closing

from wxhook_api.core.douyin_config import GroupSettings
from wxhook_api.services.groups import GroupManager


async def test_index_upgrade_preserves_rows_counts_order_and_policy_results(tmp_path):
    manager = GroupManager(tmp_path / "upgrade.sqlite3")
    await manager.start([])
    with manager._connect() as db:
        db.executescript("DROP INDEX members_blocked; DROP INDEX members_current;")
        db.executemany("INSERT INTO groups(id,name,created_at,pinned,last_seen,blocked,enabled) VALUES (?,?,?,?,?,?,?)", [
            ("a@chatroom", "local group", "2020-01-01", 0, "2024-01-01", 0, 1),
            ("b@chatroom", "", "2020-01-01", 1, "2023-01-01", 0, 1),
            ("c@chatroom", "blocked", "2020-01-01", 0, "2023-01-01", 1, 1),
            ("empty@chatroom", "", "2020-01-01", 0, "", 0, 0),
        ])
        db.executemany("INSERT INTO members(group_id,id,name,blocked,membership,last_seen) VALUES (?,?,?,?,?,?)", [
            ("a@chatroom", "a", "local member", 1, "current", "2024-01-01"),
            ("a@chatroom", "b", "", 0, "current", "2024-01-02"),
            ("a@chatroom", "c", "", 1, "left", ""),
            ("a@chatroom", "d", "", 0, "unknown", ""),
            ("b@chatroom", "a", "", 0, "current", ""),
            ("c@chatroom", "a", "", 0, "current", ""),
            # Legacy noncanonical flags retain the original blocked=1 predicate.
            ("c@chatroom", "legacy", "", 2, "unknown", ""),
        ])

    async def snapshot():
        await manager._write_policy(lambda db: None)
        with manager._connect() as db:
            raw = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
                   for table in ("groups", "members", "sync_metadata")}
        return {
            "groups": await manager.groups(GroupSettings()),
            "members": await manager.members(),
            "blocked": await manager.members(blocked_only=True),
            "policy": dict(manager._policies),
            "permissions": {(group, member): manager.allowed(group, member)
                            for group in ["a@chatroom", "b@chatroom", "c@chatroom", "empty@chatroom"]
                            for member in ["a", "b", "legacy", "unknown"]},
            "raw": raw,
        }

    before = await snapshot()
    await manager.start([])
    assert await snapshot() == before
    by_group = {row["id"]: row for row in before["groups"]}
    assert (by_group["a@chatroom"]["member_count"], by_group["a@chatroom"]["blocked_member_count"], by_group["a@chatroom"]["current_member_count"]) == (4, 2, 2)
    assert by_group["empty@chatroom"]["member_count"] == 0
    assert before["groups"][0]["id"] == "b@chatroom"
    # A second restart also preserves the data and behavior.
    await manager.start([])
    assert await snapshot() == before


async def test_indexed_queries_immediately_reflect_membership_and_blacklist_changes(tmp_path):
    manager = GroupManager(tmp_path / "updates.sqlite3")
    await manager.start([])
    group = "g@chatroom"
    groups = [{"id": group, "name": "group"}]
    await manager.sync_contacts(groups, {group: [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]})
    await manager.save_member(group, "a", {"blocked": True})
    assert [row["id"] for row in await manager.members(blocked_only=True)] == ["a"]
    assert not manager.allowed(group, "a")
    await manager.sync_contacts(groups, {group: [{"id": "b", "name": "B"}, {"id": "c", "name": "C"}]})
    row = (await manager.groups(GroupSettings()))[0]
    assert (row["member_count"], row["current_member_count"], row["blocked_member_count"]) == (3, 2, 1)
    await manager.save_member(group, "a", {"blocked": False})
    assert await manager.members(blocked_only=True) == [] and manager.allowed(group, "a")
    await manager.sync_contacts(groups, {group: []})
    assert (await manager.groups(GroupSettings()))[0]["current_member_count"] == 0


async def test_indexes_are_added_after_legacy_member_column_migrations(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""CREATE TABLE members (
            group_id TEXT NOT NULL,id TEXT NOT NULL,name TEXT NOT NULL DEFAULT '',
            blocked INTEGER NOT NULL DEFAULT 0,last_seen TEXT NOT NULL DEFAULT '',
            message_count INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(group_id,id));
            INSERT INTO members(group_id,id,name,blocked) VALUES ('g@chatroom','a','local',1);""")
    manager = GroupManager(path)
    await manager.start(["g@chatroom"])
    member = (await manager.members(blocked_only=True))[0]
    assert member["name"] == "local" and member["membership"] == "unknown"
    assert not manager.allowed("g@chatroom", "a")
    await manager.sync_contacts([{"id": "g@chatroom", "name": "group"}], {"g@chatroom": [{"id": "a", "name": "remote"}]})
    assert (await manager.groups(GroupSettings()))[0]["current_member_count"] == 1
    assert (await manager.members())[0]["name"] == "local"
