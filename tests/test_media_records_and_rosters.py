from datetime import timedelta
import json
import os

import pytest

from tests.test_media_cleanup import NOW, POLICY, seed, service_for
from tests.test_wechat_sync import FakeWeChat, ROOM, build
from wxhook_api.services.media_cleanup import cleanup_media


@pytest.mark.parametrize("legacy_members", ["", "wxid_member", "wxid_old"])
async def test_legacy_roster_does_not_replace_verified_relational_members(tmp_path, legacy_members):
    fake = FakeWeChat(history=0)
    fake.db.execute("ALTER TABLE chat_room ADD COLUMN MemberList TEXT")
    fake.db.execute("UPDATE chat_room SET MemberList=?", (legacy_members,))
    manager, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        members = await manager.members(ROOM)
        current = [member for member in members if member["membership"] == "current"]
        assert [(member["id"], member["wechat_name"]) for member in current] == [("wxid_member", "微信成员")]
    finally:
        await client.aclose()
        fake.db.close()


BROKEN_RECORDS = [
    {"status": []}, {"group_id": []}, {"source_url": {}},
    {"files": [None]}, {"files": "broken"},
    {"notifications": {"start": None}}, {"assets": [None]},
]


@pytest.mark.parametrize("patch", BROKEN_RECORDS)
def test_loading_one_malformed_record_keeps_other_media_available(tmp_path, patch):
    folder, broken = seed(tmp_path, age=timedelta(days=4), delivery="failed")
    _, healthy = seed(tmp_path, age=timedelta(days=4), delivery="failed")
    broken.update(patch)
    (folder / "result.json").write_text(json.dumps(broken), encoding="utf-8")
    service = service_for(tmp_path, enabled=False)
    service._load_saved()
    assert [job["id"] for job in service.snapshot()["jobs"]] == [healthy["id"]]
    assert service.file_path(healthy["id"], "001.webp") is not None


@pytest.mark.parametrize("patch", BROKEN_RECORDS)
def test_cleanup_preserves_malformed_record_and_its_files_but_cleans_healthy_jobs(tmp_path, patch):
    folder, broken = seed(tmp_path, age=timedelta(days=4), delivery="failed")
    healthy_folder, _ = seed(tmp_path, age=timedelta(days=4), delivery="failed")
    broken.update(patch)
    record = folder / "result.json"
    record.write_text(json.dumps(broken), encoding="utf-8")
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert report.skipped_unsafe == 1
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == before
    assert report.deleted_files == 4
    assert not (healthy_folder / "001.webp").exists()


@pytest.mark.parametrize("value", ["9999-12-31T23:59:59-08:00", "0001-01-01T00:00:00+08:00"])
def test_overflowing_media_dates_use_conservative_filesystem_fallback(tmp_path, value):
    folder, job = seed(tmp_path, age=timedelta(days=4))
    healthy_folder, healthy = seed(tmp_path, age=timedelta(days=4))
    job.update(created_at=value, finished_at=value)
    record = folder / "result.json"
    record.write_text(json.dumps(job), encoding="utf-8")
    os.utime(record, (NOW.timestamp(), NOW.timestamp()))
    service = service_for(tmp_path, enabled=False)
    service._load_saved()
    assert {item["id"] for item in service.snapshot()["jobs"]} == {job["id"], healthy["id"]}
    report = cleanup_media(tmp_path, POLICY, set(), NOW)
    assert (folder / "001.webp").exists(), "A recent fallback timestamp must preserve this job"
    assert report.deleted_files == 4 and not (healthy_folder / "001.webp").exists()
