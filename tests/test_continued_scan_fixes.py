import asyncio
from dataclasses import replace
from datetime import datetime
import gzip
from io import BytesIO
import json
from pathlib import Path
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zipfile import ZipFile

import httpx
from PIL import Image
import pytest
import yaml

from tests.test_chatbot import context
from tests.test_douyin import GROUP, SHORT, options
from tests.test_douyin_api import ENDPOINT, video_response
from tests.test_followup_scan_fixes import encode
from tests.test_parse_video import CountedStream
from tests.test_storage_and_backups import backup_service
from tests.test_wechat_sync import FakeWeChat, ROOM, build
from wxhook_api import launcher
from wxhook_api.core import bounded_http, chatbot_config
from wxhook_api.core.config import load_settings
from wxhook_api.services.backups import BackupService
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.douyin_api import DouyinAPIParser
from wxhook_api.services.douyin_delivery import DeliveryError, prepare_image
from wxhook_api.services.douyin_parser import DouyinError, DouyinParser


@pytest.mark.parametrize("metadata", [None, [], {}, True, False, 1.5, "invalid", 0, -1, "2"])
async def test_bad_history_page_is_isolated_without_import_or_checkpoint(tmp_path, metadata):
    fake = FakeWeChat(groups=2, history=1)
    original = fake.respond
    requested = []

    def respond(request):
        if request.url.path == "/Message/history":
            body = json.loads(request.content)
            requested.append(body["wxid"])
            if body["wxid"] == ROOM:
                return httpx.Response(200, json={"status": 0, "page": metadata, "data": fake.histories[ROOM]})
        return original(request)

    fake.respond = respond
    groups, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        assert requested == [ROOM, "1001@chatroom"]
        assert (await groups.messages(ROOM))["total"] == 0
        assert (await groups.messages("1001@chatroom"))["total"] == 1
        with groups._connect() as db:
            row = db.execute("SELECT history_next_page,history_complete,history_error FROM groups WHERE id=?", (ROOM,)).fetchone()
            assert row[0] == 1 and not row[1] and row[2]
            assert "invalid" not in row[2]
    finally:
        await client.aclose()
        fake.db.close()


@pytest.mark.parametrize("page", [1, "1", "missing"])
async def test_compatible_history_page_metadata_still_imports(tmp_path, page):
    fake = FakeWeChat(history=1)
    original = fake.respond

    def respond(request):
        response = original(request)
        if request.url.path == "/Message/history":
            payload = response.json()
            if page == "missing":
                payload.pop("page")
            else:
                payload["page"] = page
            return httpx.Response(200, json=payload)
        return response

    fake.respond = respond
    groups, client, sync = await build(tmp_path, fake)
    try:
        await sync.run_once()
        assert (await groups.messages(ROOM))["total"] == 1
    finally:
        await client.aclose()
        fake.db.close()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("override", ["false", "true"])
async def test_mention_save_preserves_persistent_switch_across_environment_override(tmp_path, monkeypatch, existing, explicit, override):
    monkeypatch.setattr(chatbot_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("WXHOOK_CHATBOT_ENABLED", override)
    target = tmp_path / ("custom.yml" if explicit else "chatbot.local.yml")
    if explicit:
        monkeypatch.setenv("WXHOOK_CHATBOT_CONFIG", str(target))
    else:
        monkeypatch.delenv("WXHOOK_CHATBOT_CONFIG", raising=False)
    configured_enabled = override == "false"
    base = {"enabled": configured_enabled, "mention-names": ["old"], "bot-wxid": "wxid_robot", "max-reply-chars": 1234}
    (tmp_path / "chatbot.yml").write_text(yaml.safe_dump(base), encoding="utf-8")
    if existing:
        target.write_text(yaml.safe_dump(base), encoding="utf-8")
    expected = True if explicit and not existing else configured_enabled
    configured = chatbot_config.load_chatbot_options()
    async with context(tmp_path, bot_options=configured) as state:
        result = await state.client.post("/api/manage/chatbot/mentions", json={"mention_names": ["new"]})
        assert result.status_code == 200 and result.json()["saved"]
        assert state.bot.options.enabled == (override == "true")
    monkeypatch.delenv("WXHOOK_CHATBOT_ENABLED")
    restored = chatbot_config.load_chatbot_options()
    assert restored.enabled is expected and restored.mention_names == ("new",)
    assert restored.bot_wxid == configured.bot_wxid and restored.max_reply_chars == configured.max_reply_chars


@pytest.mark.parametrize("host", ["::1", "::", "127.0.0.1", "0.0.0.0"])
async def test_default_callback_matches_launcher_for_ipv4_and_ipv6(monkeypatch, host):
    monkeypatch.setenv("WXHOOK_HOST", host)
    monkeypatch.delenv("WXHOOK_ALLOWED_CALLBACK_URLS", raising=False)
    settings = load_settings()
    callback = launcher.service_url(settings) + "/receiveChatBotMsg"
    assert callback in settings.allowed_callback_urls
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"IsLogin": 1} if request.method == "GET" else {"ret": 0})

    client = httpx.AsyncClient(base_url=launcher.service_url(settings), transport=httpx.MockTransport(respond))
    monkeypatch.setattr(launcher.httpx, "AsyncClient", lambda **kwargs: client)
    launch = Mock(side_effect=AssertionError("No real process"))
    monkeypatch.setattr(launcher, "start_wechat", launch)

    async def stop(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(launcher.asyncio, "sleep", stop)
    app = SimpleNamespace(state=SimpleNamespace(launcher_status=None))
    with pytest.raises(asyncio.CancelledError):
        await launcher.manage_wechat(app, settings, launcher.LauncherSettings(Path("unused.exe")))
    assert app.state.launcher_status["phase"] == "ready"
    assert json.loads(requests[1].content)["url"] == callback
    launch.assert_not_called()


async def test_explicit_callback_whitelist_is_not_expanded(monkeypatch):
    monkeypatch.setenv("WXHOOK_HOST", "::1")
    monkeypatch.setenv("WXHOOK_ALLOWED_CALLBACK_URLS", "http://localhost:8000/receiveChatBotMsg")
    assert load_settings().allowed_callback_urls == ("http://localhost:8000/receiveChatBotMsg",)


async def test_restart_restores_valid_backup_diagnostics_without_rotation(tmp_path, monkeypatch):
    groups, original = await backup_service(tmp_path, monkeypatch)
    latest = await original.run_once()
    with ZipFile(latest) as bundle:
        created = json.loads(bundle.read("manifest.json"))["created_at"]
    restarted = BackupService(groups, original.configs)
    restarted.run_once = AsyncMock(side_effect=AssertionError("Must reuse recent backup"))

    async def stop(delay):
        assert delay > restarted.interval - 60
        raise asyncio.CancelledError

    monkeypatch.setattr("wxhook_api.services.backups.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await restarted._run()
    assert restarted.snapshot()["last_success_at"] == datetime.fromisoformat(created).isoformat()
    assert restarted.snapshot()["last_file"] == latest.name
    assert not restarted.snapshot()["last_error"]
    assert restarted._files() == [latest]
    restarted.run_once.assert_not_awaited()


@pytest.mark.parametrize("damage", ["not-zip", "missing-database", "invalid-manifest"])
async def test_backup_discovery_does_not_claim_damaged_archive_is_valid(tmp_path, monkeypatch, damage):
    groups, original = await backup_service(tmp_path, monkeypatch)
    latest = await original.run_once()
    if damage == "not-zip":
        latest.write_bytes(b"not a ZIP")
    else:
        with ZipFile(latest, "w") as bundle:
            if damage != "missing-database":
                bundle.writestr("management.sqlite3", b"synthetic")
            bundle.writestr("manifest.json", "not-json" if damage == "invalid-manifest" else "{}")
    restarted = BackupService(groups, original.configs)
    await restarted._discover(latest)
    assert not restarted.snapshot()["last_success_at"] and not restarted.snapshot()["last_file"]
    assert "完整性检查" in restarted.snapshot()["last_error"]
    assert latest.is_file()


def png_data():
    content = BytesIO()
    Image.frombytes("RGB", (128, 128), random.Random(0).randbytes(128 * 128 * 3)).save(content, "PNG")
    return content.getvalue()


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("truncated", [False, True])
async def test_media_compression_validates_eof_before_publishing(tmp_path, monkeypatch, encoding, truncated):
    content = png_data()
    wire = encode(content, encoding)
    if truncated:
        wire = wire[:len(wire) // 2]
    stream = CountedStream([wire])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream,
        headers={"content-encoding": "deflate" if encoding == "raw-deflate" else encoding}))
    service = DouyinService(options(tmp_path))
    monkeypatch.setenv("WXHOOK_MEDIA_MIN_FREE_MB", "0")
    folder = service.options.output_dir / ("a" * 32)
    folder.mkdir(parents=True)
    async with httpx.AsyncClient(transport=transport) as client:
        service._client = client
        if truncated:
            with pytest.raises(DouyinError, match="不完整"):
                await service._download("https://p3.douyinpic.com/1.png", "image", folder, 1, GROUP, 1024 * 1024)
            assert list(folder.iterdir()) == []
        else:
            result = await service._download("https://p3.douyinpic.com/1.png", "image", folder, 1, GROUP, 1024 * 1024)
            saved = folder / result["name"]
            assert saved.read_bytes() == content
            assert prepare_image(saved) == (saved, "image")
    assert stream.closed


@pytest.mark.parametrize("suffix", ["png", "jpg"])
def test_image_preparation_rejects_truncated_pixel_data(tmp_path, suffix):
    content = BytesIO()
    Image.frombytes("RGB", (128, 128), random.Random(0).randbytes(128 * 128 * 3)).save(content, "PNG" if suffix == "png" else "JPEG")
    source = tmp_path / ("001." + suffix)
    source.write_bytes(content.getvalue()[:len(content.getvalue()) // 2])
    with pytest.raises(DeliveryError, match="未发送"):
        prepare_image(source)


@pytest.mark.parametrize("route", ["media", "builtin", "api"])
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
async def test_all_media_readers_bound_decoder_output_before_size_error(tmp_path, monkeypatch, route, encoding):
    content = b"a" * (4 * 1024 * 1024)
    wire = encode(content, encoding)
    stream = CountedStream([wire])
    produced = []
    original = bounded_http.zlib.decompressobj

    class Decoder:
        def __init__(self, *args):
            self.actual = original(*args)

        def __getattr__(self, name):
            return getattr(self.actual, name)

        def decompress(self, data, maximum):
            assert 0 < maximum <= 65536
            result = self.actual.decompress(data, maximum)
            produced.append(len(result))
            return result

    monkeypatch.setattr(bounded_http.zlib, "decompressobj", Decoder)
    from httpx._decoders import GZipDecoder, DeflateDecoder
    monkeypatch.setattr(GZipDecoder, "decode", lambda *args: pytest.fail("HTTPX must not decode before our limit"))
    monkeypatch.setattr(DeflateDecoder, "decode", lambda *args: pytest.fail("HTTPX must not decode before our limit"))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream,
        headers={"content-encoding": "deflate" if encoding == "raw-deflate" else encoding}))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DouyinError, match="过大|大小限制"):
            if route == "media":
                service = DouyinService(replace(options(tmp_path), max_file_bytes=1024 * 1024))
                service._client = client
                folder = tmp_path / "media"
                folder.mkdir()
                await service._download("https://p3.douyinpic.com/1.png", "image", folder, 1, GROUP, 1024 * 1024)
            elif route == "builtin":
                await DouyinParser(client).document("https://www.douyin.com/note/123456")
            else:
                await DouyinAPIParser(client, ENDPOINT).parse(SHORT)
    assert produced and max(produced) <= 65536 and stream.closed


@pytest.mark.parametrize("route", ["builtin", "api"])
async def test_valid_compressed_legacy_pages_and_api_remain_compatible(route):
    content = b"<html>valid</html>" if route == "builtin" else json.dumps(video_response()).encode()
    stream = CountedStream([gzip.compress(content)])
    def respond(request):
        assert request.headers["accept-encoding"] == "gzip, deflate"
        return httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if route == "builtin":
            assert (await DouyinParser(client).document("https://www.douyin.com/note/123456"))[1] == content.decode()
        else:
            assert (await DouyinAPIParser(client, ENDPOINT).parse(SHORT)).kind == "video"
    assert stream.closed
