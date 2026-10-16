from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml

from wxhook_api.core.config import PROJECT_ROOT, load_yaml


class GroupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    groups: list[str] = Field(default_factory=list, max_length=100)
    auto_download: bool = True

    @field_validator("groups")
    @classmethod
    def validate_groups(cls, groups: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(group.strip() for group in groups))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}@chatroom", group) for group in cleaned):
            raise ValueError("请填写真实群 ID，例如 123456789@chatroom，每行一个；不能填群昵称")
        return cleaned


@dataclass(frozen=True)
class DouyinOptions:
    settings: GroupSettings
    config_path: Path
    output_dir: Path
    max_file_bytes: int = 100 * 1024 * 1024


def load_douyin_options() -> DouyinOptions:
    explicit = os.getenv("WXHOOK_DOUYIN_CONFIG")
    target = Path(explicit).expanduser().resolve() if explicit else PROJECT_ROOT / "douyin.local.yml"
    source = target if target.is_file() or explicit else PROJECT_ROOT / "douyin.yml"
    values = load_yaml(source) if source.is_file() else {}
    settings = GroupSettings(
        enabled=values.get("enabled", False),
        groups=values.get("groups", []),
        auto_download=values.get("auto-download", True),
    )
    output = Path(values.get("output-dir", "downloads/douyin")).expanduser()
    if not output.is_absolute():
        output = target.parent / output
    size = values.get("max-file-mb", 100)
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 500:
        raise ValueError("douyin 配置的 max-file-mb 必须是 1 到 500 的整数")
    return DouyinOptions(settings, target, output.resolve(), size * 1024 * 1024)


def save_group_settings(options: DouyinOptions, settings: GroupSettings) -> DouyinOptions:
    values = {
        "enabled": settings.enabled,
        "groups": settings.groups,
        "auto-download": settings.auto_download,
        "output-dir": str(options.output_dir),
        "max-file-mb": options.max_file_bytes // (1024 * 1024),
    }
    options.config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = options.config_path.with_suffix(".yml.tmp")
    try:
        temporary.write_text(yaml.safe_dump(values, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temporary.replace(options.config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return replace(options, settings=settings)
