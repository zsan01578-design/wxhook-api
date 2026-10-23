from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any
from urllib.parse import urlsplit
import webbrowser

import httpx
import uvicorn

from wxhook_api.core.config import PROJECT_ROOT, Settings, load_settings, load_yaml
from wxhook_api.core.runtime import source_revision
from wxhook_api.main import create_app
from wxhook_api.services.desktop import show_wechat_window, wechat_process_ids


logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class LauncherSettings:
    wechat_path: Path
    open_browser: bool = True
    show_wechat_window: bool = False


def load_launcher_settings(path: Path | None = None) -> LauncherSettings:
    path = path or Path(os.getenv("WXHOOK_LAUNCHER_CONFIG", PROJECT_ROOT / "launcher.yml"))
    path = path.expanduser().resolve()
    values = load_yaml(path) if path.is_file() else {}
    executable = Path(os.getenv("WXHOOK_WECHAT_PATH") or values.get("wechat-path", "F:/app/Weixin/Weixin.exe")).expanduser()
    if not executable.is_absolute():
        executable = path.parent / executable
    open_browser = values.get("open-browser", True)
    if not isinstance(open_browser, bool):
        raise ValueError("launcher.yml 中的 open-browser 必须为 true 或 false")
    show_window = values.get("show-wechat-window", False)
    if not isinstance(show_window, bool):
        raise ValueError("launcher.yml 中的 show-wechat-window 必须为 true 或 false")
    return LauncherSettings(wechat_path=executable, open_browser=open_browser, show_wechat_window=show_window)


def service_url(settings: Settings) -> str:
    host = settings.host.strip("[]")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.port}"


def wechat_is_running(executable: Path) -> bool:
    return bool(wechat_process_ids(executable))


def start_wechat(options: LauncherSettings) -> str:
    if wechat_is_running(options.wechat_path):
        return "微信已运行，正在等待 Hook。"
    if not options.wechat_path.is_file():
        raise FileNotFoundError(f"找不到微信程序：{options.wechat_path}。请修改项目根目录 launcher.yml 中的 wechat-path。")
    # Start the installed client; its adjacent Hook DLL is loaded by WeChat itself.
    # Do not invoke the installer, inject a DLL, or close existing user processes.
    subprocess.Popen([str(options.wechat_path)], cwd=str(options.wechat_path.parent))
    return "已启动微信，请在微信窗口中完成登录。"


def update_status(app: Any, phase: str, message: str) -> None:
    status = {"phase": phase, "message": message}
    if app.state.launcher_status != status:
        app.state.launcher_status = status
        logger.info("%s", message)


async def present_wechat(options: LauncherSettings, timeout: float = 30) -> None:
    """Wait for an existing or newly launched client window, then activate once."""
    deadline = asyncio.get_running_loop().time() + timeout
    restored = False
    while True:
        try:
            result = await asyncio.to_thread(show_wechat_window, options.wechat_path)
        except (OSError, subprocess.SubprocessError):
            logger.warning("无法唤起微信窗口，请点击任务栏中的微信图标。")
            return
        if result == "focused":
            logger.info("已将原版微信窗口切换到前台。")
            return
        if result == "shown":
            if not restored:
                # ShowWindowAsync posts a message to WeChat's UI thread. Give
                # that thread one opportunity to show the window before retrying.
                restored = True
                await asyncio.sleep(0.2)
                continue
            logger.info("已显示微信窗口；系统暂未允许切换焦点，请点击闪烁的微信任务栏图标。")
            return
        if result == "unsupported":
            return
        if asyncio.get_running_loop().time() >= deadline:
            logger.warning("暂未找到微信主窗口，请确认微信已经启动。")
            return
        await asyncio.sleep(1)


async def manage_wechat(app: Any, settings: Settings, options: LauncherSettings) -> None:
    """Keep waiting for login, then configure callbacks after each reconnect."""
    url = service_url(settings)
    callback = f"{url}/receiveChatBotMsg"
    configured = False
    launch_attempted = False
    if callback.rstrip("/") not in {item.rstrip("/") for item in settings.allowed_callback_urls}:
        update_status(app, "error", f"自动回调地址 {callback} 不在 WXHOOK_ALLOWED_CALLBACK_URLS 白名单中。")
        return

    headers = {"Authorization": "Bearer " + os.environ["WXHOOK_ADMIN_TOKEN"]} if os.getenv("WXHOOK_ADMIN_TOKEN") else {}
    async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=15, headers=headers) as client:
        while True:
            hook_available = False
            try:
                response = await client.get("/QueryDB/status")
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("unexpected Hook response")
                login = data.get("IsLogin")
                if login is None and isinstance(data.get("data"), dict):
                    login = data["data"].get("IsLogin")
                hook_available = login in (0, 1, "0", "1", False, True)
                if not hook_available:
                    raise ValueError("missing login state")
            except (httpx.HTTPError, ValueError):
                configured = False
                if not launch_attempted:
                    launch_attempted = True
                    try:
                        message = await asyncio.to_thread(start_wechat, options)
                        update_status(app, "waiting_hook", message)
                    except (OSError, subprocess.SubprocessError) as exc:
                        update_status(app, "error", f"微信启动失败：{exc}")
                        return
                else:
                    update_status(app, "waiting_hook", "正在等待 Hook 服务，请确认微信已启动且 Hook 文件位于 Weixin.exe 同目录。")

            if hook_available:
                if login not in (1, "1", True):
                    configured = False
                    update_status(app, "waiting_login", "Hook 已连接，请在微信窗口中扫码或确认登录；登录后会自动启用消息接收。")
                elif not configured:
                    try:
                        response = await client.post("/set_callback", json={"url": callback})
                        response.raise_for_status()
                        result = response.json()
                        if not isinstance(result, dict) or result.get("ret") not in (0, "0"):
                            raise ValueError("callback was not accepted")
                    except (httpx.HTTPError, ValueError):
                        update_status(app, "error", "微信已登录，但回调配置未成功，正在自动重试。")
                    else:
                        configured = True
                        update_status(app, "ready", "微信已登录，消息接收已自动启用，可以收发消息了。")
            await asyncio.sleep(3)


async def run_service(settings: Settings, options: LauncherSettings) -> None:
    app = create_app(settings)
    app.state.launcher_status = {"phase": "starting", "message": "正在启动微信与消息服务…"}
    config = uvicorn.Config(app, host=settings.host, port=settings.port, workers=1,
                            log_level=settings.log_level.lower(), log_config=service_log_config(settings))
    server = uvicorn.Server(config)

    async def after_listening() -> None:
        while not server.started:
            await asyncio.sleep(0.1)
        if options.open_browser:
            try:
                await asyncio.to_thread(webbrowser.open, f"{service_url(settings)}/")
            except Exception:
                logger.warning("无法自动打开浏览器，请手动访问 %s/", service_url(settings))
        window_task = asyncio.create_task(present_wechat(options)) if options.show_wechat_window else None
        try:
            await manage_wechat(app, settings, options)
        finally:
            if window_task is not None:
                window_task.cancel()
                await asyncio.gather(window_task, return_exceptions=True)

    task = asyncio.create_task(after_listening())
    try:
        await server.serve()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def service_log_config(settings: Settings) -> dict:
    config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    database = Path(os.getenv("WXHOOK_MANAGEMENT_DB") or PROJECT_ROOT / "data" / "management.sqlite3")
    path = Path(os.getenv("WXHOOK_LOG_FILE") or database.parent / "wxhook.log").expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    config["formatters"]["persistent"] = {"format": "%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"}
    config["handlers"]["persistent"] = {"class": "logging.handlers.RotatingFileHandler", "formatter": "persistent",
        "filename": str(path), "maxBytes": 5 * 1024 * 1024, "backupCount": 3, "encoding": "utf-8"}
    config["loggers"]["uvicorn"]["handlers"].append("persistent")
    # Keep verbose third-party HTTP request URLs out of persistent logs.
    config["root"] = {"handlers": ["persistent"], "level": "WARNING"}
    config["loggers"]["wxhook_api"] = {"level": settings.log_level, "propagate": True}
    return config


def existing_service(settings: Settings) -> bool:
    """Reuse only an identifiable instance of this project's message service."""
    try:
        headers = {"Authorization": "Bearer " + os.environ["WXHOOK_ADMIN_TOKEN"]} if os.getenv("WXHOOK_ADMIN_TOKEN") else {}
        with httpx.Client(base_url=service_url(settings), trust_env=False, timeout=2, headers=headers) as client:
            health = client.get("/health")
            inbox = client.get("/ui/messages")
            data = inbox.json()
            status = health.json()
            identified = (
                health.status_code == inbox.status_code == 200
                and status.get("hook_base_url") == settings.hook_base_url
                and isinstance(data, dict)
                and {"instance", "messages", "capacity", "total"}.issubset(data)
                and isinstance(data.get("startup"), dict)
            )
    except (httpx.HTTPError, ValueError, AttributeError):
        return False
    if identified and status.get("runtime_revision") != source_revision():
        raise ValueError("检测到旧版消息服务仍在运行，更新的代码尚未生效。请停止原 PyCharm 运行/调试任务或在原终端按 Ctrl+C，再运行根目录 main.py。无需退出微信。")
    return identified


def main() -> None:
    try:
        settings = load_settings()
        options = load_launcher_settings()
        if urlsplit(settings.hook_base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("一键启动用于微信和 Python 在同一台电脑的场景，请将 WXHOOK_HOOK_BASE_URL 设置为本机地址。")
        if existing_service(settings):
            print(f"消息服务已运行：{service_url(settings)}/。无需重复启动。")
            if options.open_browser:
                webbrowser.open(f"{service_url(settings)}/")
            start_wechat(options)
            if options.show_wechat_window:
                asyncio.run(present_wechat(options))
            return
        # Uvicorn owns the listening socket and reports the original bind error.
        # A separate bind probe can disagree with its socket reuse behaviour.
        # Startup actions already wait for server.started, so a bind failure
        # cannot launch WeChat or open the browser.
        asyncio.run(run_service(settings, options))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError) as exc:
        print(f"启动失败：{exc}")
        raise SystemExit(1) from exc
