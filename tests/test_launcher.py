import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from wxhook_api import launcher
from wxhook_api.core.config import load_settings


def test_launcher_path_is_relative_to_its_config_and_env_can_override(tmp_path, monkeypatch):
    config = tmp_path / "launcher.yml"
    config.write_text("wechat-path: client/Weixin.exe\nopen-browser: false\n", encoding="utf-8")
    monkeypatch.delenv("WXHOOK_WECHAT_PATH", raising=False)
    options = launcher.load_launcher_settings(config)
    assert options.wechat_path == tmp_path / "client" / "Weixin.exe"
    assert options.open_browser is False
    monkeypatch.setenv("WXHOOK_WECHAT_PATH", str(tmp_path / "override.exe"))
    assert launcher.load_launcher_settings(config).wechat_path == tmp_path / "override.exe"


def test_existing_wechat_is_reused_without_spawning_a_process(monkeypatch):
    monkeypatch.setattr(launcher, "wechat_is_running", lambda executable: True)
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert "已运行" in launcher.start_wechat(launcher.LauncherSettings(Path("not-installed.exe")))
    spawn.assert_not_called()


def test_wechat_starts_from_its_own_directory_without_shell(tmp_path, monkeypatch):
    executable = tmp_path / "Weixin.exe"
    executable.touch()
    monkeypatch.setattr(launcher, "wechat_is_running", lambda path: False)
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    launcher.start_wechat(launcher.LauncherSettings(executable))
    spawn.assert_called_once_with([str(executable)], cwd=str(tmp_path))


def test_missing_wechat_gives_actionable_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "wechat_is_running", lambda path: False)
    with pytest.raises(FileNotFoundError, match="launcher.yml"):
        launcher.start_wechat(launcher.LauncherSettings(tmp_path / "missing.exe"))


async def test_manager_launches_waits_for_login_and_reconfigures_after_reconnect(monkeypatch):
    # No real client process, HTTP listener, browser or Hook is started.
    calls = []
    statuses = iter([None, 0, 1, 1, None, 1])

    def respond(request):
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/QueryDB/status":
            status = next(statuses)
            if status is None:
                return httpx.Response(502, json={})
            return httpx.Response(200, json={"IsLogin": status})
        return httpx.Response(200, json={"ret": 0})

    client = httpx.AsyncClient(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(launcher.httpx, "AsyncClient", lambda **kwargs: client)
    start = Mock(return_value="started")
    monkeypatch.setattr(launcher, "start_wechat", start)
    cycles = 0

    async def advance(seconds):
        nonlocal cycles
        cycles += 1
        if cycles == 6:
            raise asyncio.CancelledError

    monkeypatch.setattr(launcher.asyncio, "sleep", advance)
    app = SimpleNamespace(state=SimpleNamespace(launcher_status=None))
    with pytest.raises(asyncio.CancelledError):
        await launcher.manage_wechat(app, load_settings(), launcher.LauncherSettings(Path("Weixin.exe")))
    start.assert_called_once()
    callback_calls = [item for item in calls if item[1] == "/set_callback"]
    assert len(callback_calls) == 2
    assert b'http://127.0.0.1:8000/receiveChatBotMsg' in callback_calls[0][2]
    assert app.state.launcher_status["phase"] == "ready"
    assert {item[1] for item in calls} == {"/QueryDB/status", "/set_callback"}


async def test_manager_retries_failed_callback_instead_of_claiming_ready(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        payload = {"IsLogin": 1} if request.method == "GET" else {"ret": -1}
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(launcher.httpx, "AsyncClient", lambda **kwargs: client)
    start = Mock()
    monkeypatch.setattr(launcher, "start_wechat", start)

    async def stop(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(launcher.asyncio, "sleep", stop)
    app = SimpleNamespace(state=SimpleNamespace(launcher_status=None))
    with pytest.raises(asyncio.CancelledError):
        await launcher.manage_wechat(app, load_settings(), launcher.LauncherSettings(Path("Weixin.exe")))
    assert app.state.launcher_status["phase"] == "error"
    start.assert_not_called()


async def test_manager_respects_callback_allowlist_without_making_requests(monkeypatch):
    client = Mock()
    monkeypatch.setattr(launcher.httpx, "AsyncClient", client)
    app = SimpleNamespace(state=SimpleNamespace(launcher_status=None))
    settings = replace(load_settings(), allowed_callback_urls=("http://localhost:8000/receiveChatBotMsg",))
    await launcher.manage_wechat(app, settings, launcher.LauncherSettings(Path("Weixin.exe")))
    assert app.state.launcher_status["phase"] == "error"
    client.assert_not_called()


async def test_service_shutdown_cancels_startup_monitor(monkeypatch):
    server = SimpleNamespace(started=True)

    async def serve():
        # Let the startup task reach the mocked manager.
        await asyncio.sleep(0)

    server.serve = serve
    monkeypatch.setattr(launcher.uvicorn, "Server", lambda config: server)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def manage(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(launcher, "manage_wechat", manage)
    monkeypatch.setattr(launcher, "present_wechat", AsyncMock())
    await launcher.run_service(load_settings(), launcher.LauncherSettings(Path("Weixin.exe"), open_browser=False))
    assert entered.is_set()
    assert cancelled.is_set()


async def test_bind_failure_does_not_launch_wechat_or_open_browser(monkeypatch):
    async def fail_to_bind():
        await asyncio.sleep(0)
        raise OSError(10048, "address already in use")

    server = SimpleNamespace(started=False, serve=fail_to_bind)
    monkeypatch.setattr(launcher.uvicorn, "Server", lambda config: server)
    browser = Mock()
    manager = Mock()
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    monkeypatch.setattr(launcher, "manage_wechat", manager)
    with pytest.raises(OSError, match="10048"):
        await launcher.run_service(load_settings(), launcher.LauncherSettings(Path("Weixin.exe")))
    browser.assert_not_called()
    manager.assert_not_called()


@pytest.mark.parametrize("code, detail", [(10013, "permission denied"), (10048, "address already in use")])
def test_main_preserves_system_error_instead_of_assuming_port_conflict(monkeypatch, capsys, code, detail):
    monkeypatch.setattr(launcher, "load_launcher_settings", lambda: launcher.LauncherSettings(Path("Weixin.exe")))
    monkeypatch.setattr(launcher, "existing_service", lambda settings: False)

    def fail(coroutine):
        coroutine.close()
        raise OSError(code, detail)

    monkeypatch.setattr(launcher.asyncio, "run", fail)
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert str(code) in output
    assert detail in output
    assert "请先停止占用" not in output


async def test_present_wechat_waits_for_new_window_without_relaunching(monkeypatch):
    show = Mock(side_effect=["missing", "focused"])
    monkeypatch.setattr(launcher, "show_wechat_window", show)
    monkeypatch.setattr(launcher.asyncio, "sleep", AsyncMock())
    spawn = Mock()
    monkeypatch.setattr(launcher, "start_wechat", spawn)
    options = launcher.LauncherSettings(Path("Weixin.exe"))
    await launcher.present_wechat(options)
    assert show.call_count == 2
    spawn.assert_not_called()


async def test_native_window_is_presented_after_browser_even_when_hook_is_running(monkeypatch):
    events = []
    native_presented = asyncio.Event()

    async def serve():
        await asyncio.wait_for(native_presented.wait(), timeout=1)

    server = SimpleNamespace(started=True, serve=serve)
    monkeypatch.setattr(launcher.uvicorn, "Server", lambda config: server)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: events.append("browser"))

    async def present(options):
        events.append("native")
        native_presented.set()

    async def manage(*args):
        # A running Hook need not launch a new client process to show its window.
        await asyncio.Event().wait()

    monkeypatch.setattr(launcher, "present_wechat", present)
    monkeypatch.setattr(launcher, "manage_wechat", manage)
    await launcher.run_service(load_settings(), launcher.LauncherSettings(Path("Weixin.exe"), open_browser=True))
    assert events == ["browser", "native"]


def test_reusing_service_also_presents_native_wechat(monkeypatch):
    options = launcher.LauncherSettings(Path("Weixin.exe"), open_browser=False)
    monkeypatch.setattr(launcher, "load_launcher_settings", lambda: options)
    monkeypatch.setattr(launcher, "existing_service", lambda settings: True)
    start = Mock()
    present = AsyncMock()
    serve = Mock()
    monkeypatch.setattr(launcher, "start_wechat", start)
    monkeypatch.setattr(launcher, "present_wechat", present)
    monkeypatch.setattr(launcher, "run_service", serve)
    launcher.main()
    start.assert_called_once_with(options)
    present.assert_awaited_once_with(options)
    serve.assert_not_called()


@pytest.mark.parametrize("revision", [None, "old-source-revision", "current-source-revision"])
def test_reuse_requires_matching_backend_revision(monkeypatch, revision):
    settings = load_settings()

    def respond(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "hook_base_url": settings.hook_base_url, "runtime_revision": revision})
        return httpx.Response(200, json={"instance": "test", "messages": [], "capacity": 100, "total": 0, "startup": {"phase": "ready"}})

    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(launcher.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(launcher, "source_revision", lambda: "current-source-revision")
    if revision == "current-source-revision":
        assert launcher.existing_service(settings)
    else:
        with pytest.raises(ValueError, match="旧版消息服务仍在运行"):
            launcher.existing_service(settings)
