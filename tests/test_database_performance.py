from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from wxhook_api.services.groups import GroupManager


@pytest.mark.asyncio
async def test_unchanged_roster_does_not_rewrite_members(tmp_path, monkeypatch):
    manager = GroupManager(tmp_path / "rosters.sqlite3")
    await manager.start([])
    groups = [{"id": "g@chatroom", "name": "group"}]
    rosters = {"g@chatroom": [{"id": f"m{i}", "name": f"member {i}"} for i in range(500)]}
    await manager.sync_contacts(groups, rosters)
    original = manager._connect
    changes = []

    @contextmanager
    def observe():
        with original() as db:
            # A trigger makes unnecessary writes observable independently of
            # the SQL chosen by the synchronization implementation.
            db.executescript("""CREATE TABLE IF NOT EXISTS member_updates (id TEXT);
                CREATE TRIGGER IF NOT EXISTS observe_member_updates AFTER UPDATE ON members
                BEGIN INSERT INTO member_updates VALUES (new.id); END;""")
            yield db
            changes.append(db.execute("SELECT count(*) FROM member_updates").fetchone()[0])

    monkeypatch.setattr(manager, "_connect", observe)
    await manager.sync_contacts(groups, rosters)
    assert changes == [0], "Unchanged member rows should not be updated"


@pytest.mark.asyncio
async def test_roster_delta_handles_rename_departure_rejoin_and_local_policies(tmp_path):
    manager = GroupManager(tmp_path / "rosters.sqlite3")
    await manager.start([])
    group = "g@chatroom"
    groups = [{"id": group, "name": "group"}]
    await manager.sync_contacts(groups, {group: [{"id": "a", "name": "old"}, {"id": "b", "name": "stay"}]})
    await manager.save_member(group, "a", {"name": "local", "blocked": True})
    await manager.sync_contacts(groups, {group: [{"id": "a", "name": "new"}, {"id": "c", "name": "join"}]})
    rows = {row["id"]: row for row in await manager.members(group)}
    assert (rows["a"]["wechat_name"], rows["a"]["name"], rows["a"]["blocked"]) == ("new", "local", True)
    assert rows["b"]["membership"] == "left" and rows["c"]["membership"] == "current"
    await manager.sync_contacts(groups, {group: [{"id": "b", "name": "back"}, {"id": "b", "name": "last name"}]})
    rows = {row["id"]: row for row in await manager.members(group)}
    assert rows["b"]["membership"] == "current" and rows["b"]["wechat_name"] == "last name"
    assert rows["a"]["membership"] == rows["c"]["membership"] == "left"
    # Missing rosters mean unknown/unsupported, rather than an empty roster.
    await manager.sync_contacts(groups, {})
    assert next(row for row in await manager.members(group) if row["id"] == "b")["membership"] == "current"
    await manager.sync_contacts(groups, {group: []})
    assert all(row["membership"] == "left" for row in await manager.members(group))


@pytest.mark.asyncio
async def test_radar_calendar_boundaries_ties_and_latest_member_time(tmp_path, monkeypatch):
    china = timezone(timedelta(hours=8))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 3, 1, 12, tzinfo=china).astimezone(tz)

    monkeypatch.setattr("wxhook_api.services.groups.datetime", Clock)
    manager = GroupManager(tmp_path / "radar.sqlite3")
    await manager.start([])
    group = "g@chatroom"
    start = datetime(2024, 2, 28, tzinfo=china)
    moments = [start - timedelta(seconds=1), start, start + timedelta(hours=23, minutes=59, seconds=59),
               start + timedelta(days=1), start + timedelta(days=1, hours=23, minutes=59, seconds=59),
               start + timedelta(days=2), start + timedelta(days=3)]
    await manager.import_history(group, [
        {"msgid": i + 1, "sender": "a" if i % 2 else "b", "timestamp": int(moment.timestamp()), "type": 1, "content": "苹果"}
        for i, moment in enumerate(moments)
    ])
    result = await manager.radar(group, days=3)
    assert [row["date"] for row in result["daily"]] == ["2024-02-28", "2024-02-29", "2024-03-01"]
    assert [row["count"] for row in result["daily"]] == [2, 2, 1]
    assert result["hourly"][0]["count"] == 3 and result["hourly"][23]["count"] == 2
    assert result["peak_day"]["date"] == "2024-02-28"
    assert result["messages"] == result["text_messages"] == 5
    assert result["top_members"][0]["id"] == "a"
    assert result["top_members"][0]["last_seen"] == moments[5].astimezone(timezone.utc).isoformat()
    assert result["words"] == [{"text": "苹果", "count": 5}]


@pytest.mark.asyncio
async def test_radar_latest_thousand_preserves_timestamp_and_id_order(tmp_path):
    manager = GroupManager(tmp_path / "radar.sqlite3")
    await manager.start([])
    instant = int(datetime.now(timezone.utc).timestamp())
    await manager.import_history("g@chatroom", [
        {"msgid": i, "sender": "member", "timestamp": instant, "type": 1, "content": "旧词" if i < 3 else "新词"}
        for i in range(1003)
    ])
    result = await manager.radar("g@chatroom")
    assert result["messages"] == 1003
    assert result["words"] == [{"text": "新词", "count": 1000}]
