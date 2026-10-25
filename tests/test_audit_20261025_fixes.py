"""Regression coverage for backup validation and model/search response limits."""
import asyncio
import gzip
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zipfile import BadZipFile

import httpx
import pytest

from tests.test_followup_scan_fixes import encode
from tests.test_parse_video import CountedStream
from tests.test_storage_and_backups import backup_service
from wxhook_api.core import bounded_http
from wxhook_api.core.ai_safety import MAX_RESPONSE_BYTES
from wxhook_api.services.backups import BackupService
from wxhook_api.services.chat_model import ChatCompletionsProvider, ModelSettings, ModelError, UpstreamModelError, upstream_failure
from wxhook_api.services.web_search import DeepSeekSearchProvider, SearchError


async def provider_result(kind, wire, encoding="", *, length=None):
    headers = {"Content-Encoding": "deflate" if encoding == "raw-deflate" else encoding}
    if length is not None:
        headers["Content-Length"] = str(length)
    stream = CountedStream([wire])
    def respond(request):
        assert request.headers["accept-encoding"] == "gzip, deflate"
        return httpx.Response(200, stream=stream, headers=headers)
    transport = httpx.MockTransport(respond)
    try:
        if kind == "chat":
            provider = ChatCompletionsProvider(ModelSettings(base_url="https://model.invalid/v1", model="test"),
                transport, budget=SimpleNamespace(slots=asyncio.Semaphore(1), reserve=AsyncMock()))
            return await provider.answer("synthetic question")
        return await DeepSeekSearchProvider("https://search.invalid/v1", "synthetic-key", transport=transport).search("test")
    finally:
        assert stream.closed


def response_payload(kind, size=None):
    payload = ({"choices": [{"message": {"content": "safe answer"}, "finish_reason": "stop"}]}
        if kind == "chat" else {"results": [{"title": "safe title", "summary": "safe answer"}]})
    raw = json.dumps(payload).encode()
    return raw.ljust(size, b" ") if size else raw


@pytest.mark.parametrize("kind", ["chat", "search"])
@pytest.mark.parametrize("encoding", ["", "gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("size", [1024, MAX_RESPONSE_BYTES, MAX_RESPONSE_BYTES + 1])
async def test_model_and_search_response_limits(kind, encoding, size):
    wire = encode(response_payload(kind, size), encoding)
    if size > MAX_RESPONSE_BYTES:
        with pytest.raises((ModelError, SearchError), match="过大"):
            await provider_result(kind, wire, encoding)
    else:
        result = await provider_result(kind, wire, encoding)
        assert result == "safe answer" if kind == "chat" else result[0].snippet == "safe answer"


@pytest.mark.parametrize("kind", ["chat", "search"])
@pytest.mark.parametrize("damage", ["trailer", "crc", "deflate", "encoding"])
async def test_incomplete_or_invalid_model_and_search_responses_are_rejected(kind, damage):
    raw = response_payload(kind)
    wire, encoding = gzip.compress(raw), "gzip"
    if damage == "trailer":
        wire = wire[:-8]
    elif damage == "crc":
        wire = wire[:-8] + bytes([wire[-8] ^ 1]) + wire[-7:]
    elif damage == "deflate":
        wire, encoding = encode(raw, "deflate")[:-2], "deflate"
    else:
        wire, encoding = raw, "br"
    with pytest.raises((ModelError, SearchError), match="传输不完整或压缩格式无效"):
        await provider_result(kind, wire, encoding)


@pytest.mark.parametrize("kind", ["chat", "search"])
async def test_decoder_output_is_bounded_before_response_limit(kind, monkeypatch):
    wire = gzip.compress(response_payload(kind, 4 * 1024 * 1024))
    original = bounded_http.zlib.decompressobj
    sizes = []
    class Decoder:
        def __init__(self, *args, **kwargs):
            self.inner = original(*args, **kwargs)
        def __getattr__(self, name):
            return getattr(self.inner, name)
        def decompress(self, data, max_length=0):
            assert 0 < max_length <= 65536
            result = self.inner.decompress(data, max_length)
            sizes.append(len(result))
            return result
    monkeypatch.setattr(bounded_http.zlib, "decompressobj", Decoder)
    with pytest.raises((ModelError, SearchError), match="过大"):
        await provider_result(kind, wire, "gzip")
    assert sizes and max(sizes) <= 65536
    assert sum(sizes) <= MAX_RESPONSE_BYTES + 1


@pytest.mark.parametrize("kind", ["chat", "search"])
async def test_wire_limit_and_concatenated_gzip_share_response_budget(kind):
    with pytest.raises((ModelError, SearchError), match="过大"):
        await provider_result(kind, response_payload(kind), length=MAX_RESPONSE_BYTES + 1)
    payload = response_payload(kind, MAX_RESPONSE_BYTES + 1)
    wire = gzip.compress(payload[:100000]) + gzip.compress(payload[100000:])
    with pytest.raises((ModelError, SearchError), match="过大"):
        await provider_result(kind, wire, "gzip")


@pytest.mark.parametrize("encoding", ["", "gzip", "deflate"])
@pytest.mark.parametrize("damage", [None, "oversize", "incomplete"])
async def test_only_complete_bounded_quota_errors_allow_model_failover(encoding, damage):
    payload = json.dumps({"error": {"code": "insufficient_quota", "message": "private upstream data"}}).encode()
    if damage == "oversize":
        payload = payload.ljust(16385, b" ")
    wire = encode(payload, encoding)
    if damage == "incomplete":
        wire = wire[:-8] if encoding == "gzip" else wire[:-2]
    stream = CountedStream([wire])
    transport = httpx.MockTransport(lambda request: httpx.Response(400, stream=stream, headers={"Content-Encoding": encoding}))
    async with httpx.AsyncClient(transport=transport) as client:
        async with client.stream("POST", "https://model.invalid") as response:
            with pytest.raises(ModelError) as exc:
                await upstream_failure(response, "safe error")
    assert stream.closed
    assert str(exc.value) == "safe error"
    assert isinstance(exc.value, UpstreamModelError) == (damage is None)


async def test_corrupt_backups_do_not_count_toward_retention(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    service.keep = 4
    files = [await service.run_once() for _ in range(4)]
    files[-1].write_bytes(b"damaged archive")
    service.keep = 2
    await service.cleanup()
    assert not files[0].exists()
    assert all(path.exists() for path in files[1:])
    assert all(service._existing(path) for path in files[1:3])
    # Even if fewer valid copies than requested remain, never delete them.
    files[-2].write_bytes(b"another damaged archive")
    await service.cleanup()
    assert files[1].exists() and service._existing(files[1])


async def test_corrupt_latest_backup_is_replaced_immediately(tmp_path, monkeypatch):
    groups, original = await backup_service(tmp_path, monkeypatch)
    original.keep = 2
    healthy = await original.run_once()
    damaged = await original.run_once()
    damaged.write_bytes(b"damaged archive")
    service = BackupService(groups, original.configs)
    service.keep = 1
    create = service._create
    def checked_create():
        assert healthy.exists() and service._existing(healthy)
        return create()
    monkeypatch.setattr(service, "_create", checked_create)
    async def stop(delay):
        assert delay == service.interval
        raise asyncio.CancelledError
    monkeypatch.setattr("wxhook_api.services.backups.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await service._run()
    files = service._files()
    assert len(files) == 1 and files[0] not in {healthy, damaged}
    assert service._existing(files[0]) and not service.status["last_error"]
    assert service.status["last_file"] == files[0].name


async def test_failed_replacement_preserves_healthy_backup_and_retries(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    service.keep = 2
    healthy = await service.run_once()
    damaged = await service.run_once()
    damaged.write_bytes(b"damaged archive")
    service.keep = 1
    create = service._create
    calls = 0
    def transient_failure():
        nonlocal calls
        calls += 1
        assert healthy.exists()
        if calls == 1:
            raise OSError("private disk error")
        return create()
    monkeypatch.setattr(service, "_create", transient_failure)
    delays = []
    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 1:
            assert delay == 60 and healthy.exists()
            assert "private" not in service.status["last_error"]
        else:
            raise asyncio.CancelledError
    monkeypatch.setattr("wxhook_api.services.backups.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await service._run()
    assert calls == 2 and delays == [60, service.interval]
    assert len(service._files()) == 1 and service._existing(service._files()[0])
    assert not service.status["last_error"]


async def test_backup_validation_cache_is_invalidated_on_file_change(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    latest = await service.run_once()
    validate = Mock(wraps=service._existing)
    monkeypatch.setattr(service, "_existing", validate)
    assert await service._discover(latest)
    await service.cleanup()
    # The file was verified during run_once's cleanup; unchanged cycles reuse it.
    validate.assert_not_called()
    latest.write_bytes(b"damaged archive")
    assert not await service._discover(latest)
    validate.assert_called_once()
    assert not service.status["last_file"] and not service.status["last_success_at"]
    with pytest.raises(BadZipFile):
        service._existing(latest)


@pytest.mark.parametrize("failure", ["unreadable", "changed"])
async def test_unverified_backup_never_replaces_last_healthy_copy(tmp_path, monkeypatch, failure):
    _, service = await backup_service(tmp_path, monkeypatch)
    service.keep = 2
    healthy = await service.run_once()
    candidate = await service.run_once()
    # Simulate a fresh process, without a cached validation for either file.
    service = BackupService(service.groups, service.configs)
    service.keep = 1
    original = service._existing
    def validate(path):
        if path == candidate:
            if failure == "unreadable":
                raise PermissionError("synthetic locked archive")
            result = original(path)
            path.write_bytes(b"changed while being checked")
            return result
        return original(path)
    monkeypatch.setattr(service, "_existing", validate)
    await service.cleanup()
    assert healthy.exists() and original(healthy)
    assert candidate.exists()


async def test_no_valid_backup_means_no_retention_deletion(tmp_path, monkeypatch):
    _, service = await backup_service(tmp_path, monkeypatch)
    service.keep = 3
    files = [await service.run_once() for _ in range(3)]
    for path in files:
        path.write_bytes(b"damaged")
    service.keep = 1
    await service.cleanup()
    assert service._files() == files
