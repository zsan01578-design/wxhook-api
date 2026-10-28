from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml

from wxhook_api.core.config import PROJECT_ROOT, load_yaml


class GroupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    groups: list[str] = Field(default_factory=list, max_length=100)
    auto_download: bool = True
    notify_group: bool = True
    send_results: bool = True
    parser_provider: Literal["parse_video", "api", "builtin", "media_parser"] = "parse_video"
    parser_api_url: str = Field(default="", max_length=4096)
    media_parser_url: str = Field(default="http://127.0.0.1:8051", max_length=2048)

    @field_validator("parser_api_url")
    @classmethod
    def validate_parser_api_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme == "https" and parsed.hostname == "jk.y-l.cc" and parsed.port in {None, 443}
            valid = valid and parsed.path.startswith("/api/dsp/") and not parsed.username and not parsed.password and not parsed.fragment and not parsed.query
            if not valid or any(ord(char) < 33 for char in value):
                raise ValueError
        except ValueError as exc:
            raise ValueError("请填写完整的 https://jk.y-l.cc/api/dsp/.../.../ 接口地址，不要附加 ?url= 参数") from exc
        return value

    @field_validator("media_parser_url")
    @classmethod
    def validate_media_parser_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.port is not None
                and 1 <= parsed.port <= 65535
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and parsed.path in {"", "/"}
                and not any(ord(char) < 33 for char in value)
            )
            if not valid:
                raise ValueError
        except ValueError as exc:
            raise ValueError("media-parser 地址必须是本机地址，例如 http://127.0.0.1:8051") from exc
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_media_parser_selection(self):
        if self.parser_provider == "media_parser" and not self.media_parser_url:
            raise ValueError("选择 media-parser 时必须填写本机服务地址")
        return self

    @field_validator("groups")
    @classmethod
    def validate_groups(cls, groups: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(group.strip() for group in groups))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}@chatroom", group) for group in cleaned):
            raise ValueError("请填写真实群 ID，例如 123456789@chatroom，每行一个；不能填群昵称")
        return cleaned


@dataclass(frozen=True)
class MediaRetention:
    enabled: bool = True
    hour: int = 3
    sent_hours: int = 24
    other_hours: int = 72
    record_days: int = 30


def load_media_retention(values: dict | None = None) -> MediaRetention:
    values = values or {}
    enabled = values.get("cleanup-enabled", True)
    override = os.getenv("WXHOOK_MEDIA_CLEANUP_ENABLED")
    if override is not None:
        if override.lower() not in {"true", "false", "1", "0", "on", "off"}:
            raise ValueError("WXHOOK_MEDIA_CLEANUP_ENABLED 必须为 true 或 false")
        enabled = override.lower() in {"true", "1", "on"}
    hour = values.get("cleanup-hour", 3)
    if not isinstance(enabled, bool) or isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ValueError("cleanup-enabled 必须为布尔值，cleanup-hour 必须为 0 到 23 的整数")
    return MediaRetention(enabled=enabled, hour=hour)


@dataclass(frozen=True)
class DouyinOptions:
    settings: GroupSettings
    config_path: Path
    output_dir: Path
    max_file_bytes: int = 100 * 1024 * 1024
    retention: MediaRetention = field(default_factory=load_media_retention)


def load_douyin_options() -> DouyinOptions:
    explicit = os.getenv("WXHOOK_DOUYIN_CONFIG")
    target = Path(explicit).expanduser().resolve() if explicit else PROJECT_ROOT / "douyin.local.yml"
    source = target if target.is_file() or explicit else PROJECT_ROOT / "douyin.yml"
    values = load_yaml(source) if source.is_file() else {}
    version = values.get("parser-config-version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("parser-config-version 必须是整数")
    settings = GroupSettings(
        enabled=values.get("enabled", False),
        groups=values.get("groups", []),
        auto_download=values.get("auto-download", True),
        notify_group=values.get("notify-group", True),
        send_results=values.get("send-results", True),
        # Switch pre-library configurations once. Saving from the new UI writes
        # version 2, so a later explicit provider choice remains authoritative.
        parser_provider=values.get("parser-provider", "parse_video") if version >= 2 else "parse_video",
        parser_api_url=os.getenv("WXHOOK_DOUYIN_API_URL", values.get("parser-api-url", "")),
        media_parser_url=os.getenv("WXHOOK_MEDIA_PARSER_URL", values.get("media-parser-url", "http://127.0.0.1:8051")),
    )
    output = Path(values.get("output-dir", "downloads/douyin")).expanduser()
    if not output.is_absolute():
        output = target.parent / output
    size = values.get("max-file-mb", 100)
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 500:
        raise ValueError("douyin 配置的 max-file-mb 必须是 1 到 500 的整数")
    return DouyinOptions(settings, target, output.resolve(), size * 1024 * 1024, load_media_retention(values))


def save_group_settings(options: DouyinOptions, settings: GroupSettings) -> DouyinOptions:
    values = {
        "enabled": settings.enabled,
        "groups": settings.groups,
        "auto-download": settings.auto_download,
        "notify-group": settings.notify_group,
        "send-results": settings.send_results,
        "parser-provider": settings.parser_provider,
        "parser-config-version": 2,
        "parser-api-url": settings.parser_api_url,
        "media-parser-url": settings.media_parser_url,
        "output-dir": str(options.output_dir),
        "max-file-mb": options.max_file_bytes // (1024 * 1024),
        "cleanup-enabled": options.retention.enabled,
        "cleanup-hour": options.retention.hour,
    }
    options.config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = options.config_path.with_suffix(".yml.tmp")
    try:
        temporary.write_text(yaml.safe_dump(values, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temporary.replace(options.config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return replace(options, settings=settings)
