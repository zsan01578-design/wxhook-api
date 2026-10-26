import asyncio
from dataclasses import replace
import gzip
import json
import os
from pathlib import Path
import threading
from unittest.mock import AsyncMock
from zipfile import ZipFile
import zlib

import httpx
import pytest

from tests.test_douyin import options
from tests.test_parse_video import CountedStream
from tests.test_storage_and_backups import backup_service
from wxhook_api.core.config import load_settings
from wxhook_api.main import create_app
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services import parse_video
from wxhook_api.services.douyin_parser import DouyinError


async def test_locked_old_backup_does_not_block_other_pruning_or_hide_new_snapshot(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    service.keep = 1
    first = await service.run_once()
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == first:
            raise PermissionError("private locked backup path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    for _ in range(3):
        latest = await service.run_once()
        assert service.status["last_file"] == latest.name and service.status["last_success_at"]
        assert service.status["last_error"] == "" and service.status["cleanup_error"]
        assert service._files() == [first, latest]
        with ZipFile(latest) as bundle:
            assert bundle.testzip() is None
    assert "private locked backup path" not in json.dumps(service.snapshot())
    monkeypatch.setattr(Path, "unlink", original)
    before = service.status["last_success_at"]
    await service.cleanup()
    assert service._files() == [latest] and service.status["cleanup_error"] == ""
    assert service.status["last_success_at"] == before


async def test_scheduler_retries_retention_without_creating_another_snapshot(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    service.keep = 1
    first = await service.run_once()
    os.utime(first, (0, 0))
    original = Path.unlink
    locked = True

    def unlink(path, *args, **kwargs):
        if path == first and locked:
            raise PermissionError("locked")
        return original(path, *args, **kwargs)

    delays = []

    async def sleep(delay):
        nonlocal locked
        delays.append(delay)
        if len(delays) == 1:
            assert len(service._files()) == 2 and service.status["cleanup_error"]
            locked = False
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr("wxhook_api.services.backups.asyncio.sleep", sleep)
    service.run_once = AsyncMock(wraps=service.run_once)
    with pytest.raises(asyncio.CancelledError):
        await service._run()
    assert service.run_once.await_count == 1
    assert delays[0] == 60 and delays[1] > service.interval - 60
    assert len(service._files()) == 1 and not first.exists()
    assert not service.status["last_error"] and not service.status["cleanup_error"]


async def test_restart_cleans_surplus_backups_even_with_a_recent_snapshot(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    service.keep = 3
    for _ in range(3):
        await service.run_once()
    service.keep = 1
    service.run_once = AsyncMock(side_effect=AssertionError("must reuse recent snapshot"))

    async def sleep(delay):
        assert len(service._files()) == 1
        raise asyncio.CancelledError

    monkeypatch.setattr("wxhook_api.services.backups.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await service._run()
    service.run_once.assert_not_awaited()


async def test_due_snapshot_failure_does_not_prevent_retention_recovery(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    service.keep = 3
    for _ in range(3):
        latest = await service.run_once()
    service.keep = 1
    os.utime(latest, (0, 0))
    service.run_once = AsyncMock(side_effect=OSError("no backup headroom"))

    async def sleep(delay):
        assert delay == 60
        raise asyncio.CancelledError

    monkeypatch.setattr("wxhook_api.services.backups.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await service._run()
    service.run_once.assert_awaited_once()
    assert service._files() == [latest]
    assert service.status["last_error"] and not service.status["cleanup_error"]


async def test_cancelled_cleanup_keeps_backup_lock_until_pruning_finishes(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def prune():
        entered.set()
        assert release.wait(5)
        finished.set()
        return 0

    monkeypatch.setattr(service, "_prune", prune)
    task = asyncio.create_task(service.cleanup())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert service._lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert finished.is_set() and not service._lock.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("explicit", [False, True])
async def test_backup_excludes_effective_parser_file_and_cookie(tmp_path, monkeypatch, explicit):
    monkeypatch.setattr("wxhook_api.main.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(parse_video, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("WXHOOK_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("WXHOOK_LAUNCHER_CONFIG", str(tmp_path / "launcher.yml"))
    monkeypatch.delenv("PARSE_VIDEO_XHS_COOKIE", raising=False)
    parser_config = tmp_path / ("custom-parser.yml" if explicit else "parse-video.local.yml")
    parser_config.write_text("xiaohongshu-cookie: synthetic-cookie-value\n", encoding="utf-8")
    if explicit:
        monkeypatch.setenv("WXHOOK_PARSE_VIDEO_CONFIG", str(parser_config))
    else:
        monkeypatch.delenv("WXHOOK_PARSE_VIDEO_CONFIG", raising=False)
    assert parse_video._local_xhs_cookie() == "synthetic-cookie-value"
    config = tmp_path / "version.yml"
    config.write_text("start-port: 30001\n", encoding="utf-8")
    app = create_app(settings=replace(load_settings(), config_path=config),
                     douyin_service=DouyinService(options(tmp_path)))
    await app.state.groups.start([])
    archive = await app.state.backups.run_once()
    with ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert "config/parse-video.local.yml" not in bundle.namelist()
        assert str(parser_config.resolve()) not in manifest["sources"].values()
        assert all(b"synthetic-cookie-value" not in bundle.read(name) for name in bundle.namelist())
        assert "config/version.yml" in bundle.namelist()
    assert parser_config.is_file()
    assert parse_video._local_xhs_cookie() == "synthetic-cookie-value"


def encode(data, encoding):
    if encoding == "gzip":
        return gzip.compress(data)
    if encoding == "deflate":
        return zlib.compress(data)
    if encoding == "raw-deflate":
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        return compressor.compress(data) + compressor.flush()
    return data


async def request_page(payload, encoding, monkeypatch, *, chunks=None, content_type="text/html"):
    monkeypatch.delenv("PARSE_VIDEO_PROXY", raising=False)
    parse_video._load_library()
    from parse_video_py.utils import create_async_client
    stream = CountedStream(chunks if chunks is not None else [payload])

    def respond(request):
        assert request.headers["accept-encoding"] == "gzip, deflate"
        headers = {"Content-Type": content_type, "Content-Length": str(len(payload))}
        if encoding:
            headers["Content-Encoding"] = "deflate" if encoding == "raw-deflate" else encoding
        return httpx.Response(200, headers=headers, stream=stream)

    token = parse_video._request_context.set(parse_video._RequestContext(httpx.MockTransport(respond)))
    try:
        async with create_async_client() as client:
            try:
                return await client.get("https://www.xiaohongshu.com/explore/test"), stream
            finally:
                assert stream.closed
    finally:
        parse_video._request_context.reset(token)


@pytest.mark.parametrize("encoding", ["", "gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("size", [1024, parse_video._MAX_PAGE_BYTES, parse_video._MAX_PAGE_BYTES + 1])
async def test_decoded_page_limit_with_compression_boundaries(monkeypatch, encoding, size):
    content = b"a" * size
    payload = encode(content, encoding)
    if size > parse_video._MAX_PAGE_BYTES:
        with pytest.raises(DouyinError, match="页面过大"):
            await request_page(payload, encoding, monkeypatch)
    else:
        response, _ = await request_page(payload, encoding, monkeypatch)
        assert response.content == content


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
async def test_fragmented_compression_headers_and_body_are_decoded(monkeypatch, encoding):
    content = "中文测试".encode() * 100
    payload = encode(content, encoding)
    response, _ = await request_page(payload, encoding, monkeypatch, chunks=[bytes([byte]) for byte in payload])
    assert response.content == content


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
async def test_truncated_or_invalid_compressed_pages_are_rejected(monkeypatch, encoding):
    payload = encode(b"page", encoding)[:-2]
    with pytest.raises(DouyinError, match="压缩页面"):
        await request_page(payload, encoding, monkeypatch)
    with pytest.raises(DouyinError, match="压缩页面"):
        await request_page(b"invalid compressed body", encoding, monkeypatch)


async def test_gzip_members_share_one_decoded_limit(monkeypatch):
    small = gzip.compress(b"first") + gzip.compress(b"second")
    response, _ = await request_page(small, "gzip", monkeypatch)
    assert response.content == b"firstsecond"
    large = gzip.compress(b"a" * (2 * 1024 * 1024)) + gzip.compress(b"b" * (2 * 1024 * 1024))
    with pytest.raises(DouyinError, match="页面过大"):
        await request_page(large, "gzip", monkeypatch)


async def test_decoded_stream_output_chunks_stay_bounded():
    stream = CountedStream([gzip.compress(b"a" * parse_video._MAX_PAGE_BYTES)])
    wrapper = parse_video._LimitedStream(stream, encoding="gzip")
    sizes = [len(chunk) async for chunk in wrapper]
    assert sum(sizes) == parse_video._MAX_PAGE_BYTES and max(sizes) <= 65536
    await wrapper.aclose()
    assert stream.closed


async def test_unknown_encoding_is_rejected_and_binary_probe_still_skips_body(monkeypatch):
    with pytest.raises(DouyinError, match="压缩格式不受支持"):
        await request_page(b"untrusted codec", "br", monkeypatch)
    response, stream = await request_page(b"not even valid gzip", "gzip", monkeypatch, content_type="video/mp4")
    assert response.content == b"" and stream.reads == 0


async def test_empty_encoded_redirect_body_remains_compatible(monkeypatch):
    response, _ = await request_page(b"", "gzip", monkeypatch)
    assert response.content == b""
