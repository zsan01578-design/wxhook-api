from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "version.yml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "version.example.yml"


def _parse_port(value: Any, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer port") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def _parse_float(value: Any, name: str, minimum: float = 0.01) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return number


def _clean_url(value: str, name: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{name} cannot contain credentials or a fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), parsed.query, ""))


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    config_path: Path
    host: str
    port: int
    hook_base_url: str
    hook_config: dict[str, Any]
    connect_timeout: float = 2.0
    read_timeout: float = 10.0
    max_body_bytes: int = 1_048_576
    callback_queue_size: int = 1000
    shutdown_timeout: float = 5.0
    log_level: str = "INFO"
    allowed_callback_urls: tuple[str, ...] = ()
    callback_workers: int = 4

    @classmethod
    def from_environment(cls, config_path: str | Path | None = None) -> "Settings":
        selected_path = Path(config_path or os.getenv("WXHOOK_CONFIG") or DEFAULT_CONFIG_PATH).expanduser().resolve()
        if not selected_path.is_file() and config_path is None and not os.getenv("WXHOOK_CONFIG"):
            # A fresh clone has only the example. Keep the service importable while
            # making it obvious that this is not a completed local configuration.
            if EXAMPLE_CONFIG_PATH.is_file():
                selected_path = EXAMPLE_CONFIG_PATH.resolve()
        if not selected_path.is_file() and selected_path.name == "version.yml":
            raise ValueError(
                "configuration file does not exist: "
                f"{selected_path}. Copy version.example.yml to version.yml and fill in this computer's Hook settings."
            )
        loaded = load_yaml(selected_path)
        hook_port = _parse_port(loaded.get("start-port", 30001), "start-port")
        host = os.getenv("WXHOOK_HOST", "127.0.0.1").strip()
        port = _parse_port(os.getenv("WXHOOK_PORT", "8000"), "WXHOOK_PORT")
        hook_url = os.getenv("WXHOOK_HOOK_BASE_URL", f"http://127.0.0.1:{hook_port}")
        hook_url = _clean_url(hook_url, "WXHOOK_HOOK_BASE_URL")
        hook_parts = urlsplit(hook_url)
        service_host = host.lower().strip("[]")
        hook_host = (hook_parts.hostname or "").lower().strip("[]")
        local_hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}
        if service_host in local_hosts and hook_host in local_hosts and hook_parts.port == port:
            raise ValueError("service and hook cannot use the same address")

        allowed = _csv(os.getenv("WXHOOK_ALLOWED_CALLBACK_URLS"))
        if not allowed:
            allowed = (
                f"http://127.0.0.1:{port}/receiveChatBotMsg",
                f"http://localhost:{port}/receiveChatBotMsg",
                f"http://[::1]:{port}/receiveChatBotMsg",
            )
        allowed = tuple(_clean_url(url, "allowed callback URL") for url in allowed)

        max_body_bytes = int(os.getenv("WXHOOK_MAX_BODY_BYTES", "1048576"))
        callback_queue_size = int(os.getenv("WXHOOK_CALLBACK_QUEUE_SIZE", "1000"))
        callback_workers = int(os.getenv("WXHOOK_CALLBACK_WORKERS", "4"))
        if not 1 <= callback_workers <= 4:
            raise ValueError("WXHOOK_CALLBACK_WORKERS must be between 1 and 4")
        if max_body_bytes < 1 or callback_queue_size < 1:
            raise ValueError("body and queue limits must be positive")
        return cls(
            config_path=selected_path,
            host=host,
            port=port,
            hook_base_url=hook_url,
            hook_config=loaded,
            connect_timeout=_parse_float(os.getenv("WXHOOK_CONNECT_TIMEOUT", "2"), "connect timeout"),
            read_timeout=_parse_float(os.getenv("WXHOOK_READ_TIMEOUT", "10"), "read timeout"),
            max_body_bytes=max_body_bytes,
            callback_queue_size=callback_queue_size,
            callback_workers=callback_workers,
            shutdown_timeout=_parse_float(os.getenv("WXHOOK_SHUTDOWN_TIMEOUT", "5"), "shutdown timeout"),
            log_level=os.getenv("WXHOOK_LOG_LEVEL", "INFO").upper(),
            allowed_callback_urls=allowed,
        )


def load_yaml(path: str | Path) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"configuration file does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as file:
            loaded = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in configuration: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("configuration must be a mapping")
    return loaded


def load_settings(path: str | Path | None = None) -> Settings:
    return Settings.from_environment(path)
