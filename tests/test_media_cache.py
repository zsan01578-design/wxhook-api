import asyncio
import json
import os
import time

import httpx

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, SHORT, options
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services import douyin as douyin_module
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.douyin_parser import ParsedMedia
from wxhook_api.services.media_cache import MediaCache


MP4 = b"\0\0\0\x18ftypisom" + b"\0" * 40
URL = "https://v3.douyinvod.com/cache.mp4"


def test_cache_store_load_and_materialize(tmp_path):
    cache = MediaCache(tmp_path / "cache")
    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "001.mp4").write_bytes(MP4)
    job = {
        "id": "a" * 32, "source_url": SHORT, "platform": "douyin", "item_id": "id", "title": "标题",
        "author": "作者", "kind": "video", "assets": [{"kind": "video", "urls": [URL]}],
        "files": [{"name": "001.mp4", "kind": "video", "size": len(MP4)}],
        "parser_provider": "dudunas", "parser_fallback": True,
    }
    key = cache.key("douyin", SHORT)
    cache.store(key, folder, job)
    loaded = cache.load(key)
    assert loaded["key"] == key and loaded["title"] == "标题"
    assert loaded["files"] == [{"name": "001.mp4", "kind": "video", "size": len(MP4)}]

    target = tmp_path / "target"
    files = cache.materialize(loaded, target)
    assert files == [{"name": "001.mp4", "kind": "video", "size": len(MP4)}]
    assert (target / "001.mp4").read_bytes() == MP4


def test_cache_rejects_unsafe_or_corrupt_entries(tmp_path):
    cache = MediaCache(tmp_path / "cache")
    assert cache.load("../escape") is None
    entry = tmp_path / "cache" / cache.key("douyin", SHORT)
    entry.mkdir(parents=True)
    (entry / "meta.json").write_text("{bad json", encoding="utf-8")
    assert cache.load(entry.name) is None


def test_cache_load_rejects_expired_entry_before_cleanup(tmp_path):
    cache = MediaCache(tmp_path / "cache")
    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "001.mp4").write_bytes(MP4)
    key = cache.key("douyin", SHORT)
    cache.store(key, folder, {
        "source_url": SHORT, "platform": "douyin", "item_id": "", "title": "", "author": "", "kind": "video",
        "assets": [], "files": [{"name": "001.mp4", "kind": "video", "size": len(MP4)}],
    })
    meta_path = tmp_path / "cache" / key / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["created_at"] = "2000-01-01T00:00:00+00:00"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert cache.load(key) is None


def test_cache_cleanup_removes_expired_entries(tmp_path):
    cache = MediaCache(tmp_path / "cache")
    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "001.mp4").write_bytes(MP4)
    key = cache.key("douyin", SHORT)
    cache.store(key, folder, {
        "source_url": SHORT, "platform": "douyin", "item_id": "", "title": "", "author": "", "kind": "video",
        "assets": [], "files": [{"name": "001.mp4", "kind": "video", "size": len(MP4)}],
    })
    old = time.time() - 8 * 86400
    os.utime(tmp_path / "cache" / key / "meta.json", (old, old))
    cache.cleanup()
    assert cache.load(key) is None


class FakeParser:
    def __init__(self):
        self.calls = 0

    async def parse(self, source):
        self.calls += 1
        return ParsedMedia("id", "标题", "作者", "video", [{"kind": "video", "urls": [URL]}], "douyin")


async def test_second_identical_link_uses_cached_media(tmp_path, monkeypatch):
    parser = FakeParser()
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: parser)
    downloads = []

    def respond(request):
        if request.url.host == "v3.douyinvod.com":
            downloads.append(str(request.url))
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    service = DouyinService(options(tmp_path, parser_provider="parse_video", send_results=False),
                            transport=httpx.MockTransport(respond))
    await service.start()
    try:
        for msgid in (1, 2):
            await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "msgid": msgid, "content": SHORT}))
            await asyncio.wait_for(service._queue.join(), 5)
        jobs = service.snapshot()["jobs"]
        assert len(jobs) == 2
        assert parser.calls == 1
        assert len(downloads) == 1
        assert sum(job.get("cache_hit") is True for job in jobs) == 1
        assert all(job["status"] == "completed" and job["files"] for job in jobs)
        assert all(service.file_path(job["id"], "001.mp4") is not None for job in jobs)
    finally:
        await service.stop()
