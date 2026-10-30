"""Run ucmao/media-parser as a loopback-only service for wxhook."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import types


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    configured = os.getenv("WXHOOK_MEDIA_PARSER_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (PROJECT_ROOT.parent / "media-parser").resolve()


def parse_video_config_path() -> Path:
    return Path(os.getenv("WXHOOK_PARSE_VIDEO_CONFIG") or PROJECT_ROOT / "parse-video.local.yml").expanduser()


def bridge_xhs_cookie(project_root: Path) -> None:
    if os.getenv("XHS_COOKIE", "").strip():
        return
    configured = os.getenv("PARSE_VIDEO_XHS_COOKIE", "").strip()
    if configured and all(32 <= ord(char) <= 126 for char in configured):
        os.environ["XHS_COOKIE"] = configured
        return
    path = parse_video_config_path()
    try:
        if not path.is_file() or path.stat().st_size > 32 * 1024:
            return
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        value = data.get("xiaohongshu-cookie", "")
    except Exception:
        return
    if isinstance(value, str):
        value = value.strip()
    else:
        return
    if value and all(32 <= ord(char) <= 126 for char in value):
        os.environ["XHS_COOKIE"] = value


def bridge_douyin_session(project_root: Path) -> None:
    """Pass the owner's optional Douyin session to this child process only."""
    path = parse_video_config_path()
    try:
        if path.is_file() and path.stat().st_size <= 32 * 1024:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
    except Exception:
        data = {}
    for target, alias, source, key, limit in (
        ("DOUYIN_COOKIE", "DY_COOKIE", "PARSE_VIDEO_DOUYIN_COOKIE", "douyin-cookie", 16384),
        ("DOUYIN_UIFID", "DY_UIFID", "PARSE_VIDEO_DOUYIN_UIFID", "douyin-uifid", 4096),
    ):
        if os.getenv(target, "").strip() or os.getenv(alias, "").strip():
            continue
        value = os.getenv(source)
        if value is None:
            value = data.get(key, "")
        if isinstance(value, str) and len(value) <= limit and all(32 <= ord(char) <= 126 for char in value):
            value = value.strip()
            if value:
                os.environ[target] = value


def install_fcntl_stub() -> None:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        module = types.ModuleType("fcntl")
        module.LOCK_EX = 2

        def flock(descriptor, operation):
            return None

        module.flock = flock
        sys.modules["fcntl"] = module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root")
    parser.add_argument("--port", type=int, default=int(os.getenv("WXHOOK_MEDIA_PARSER_PORT", "8051") or "8051"))
    args = parser.parse_args(argv)
    root = resolve_root(args.root)
    if not (root / "app.py").is_file() or not (root / "src" / "parsers").is_dir():
        print(f"media-parser source not found at {root}", file=sys.stderr)
        return 2
    bridge_xhs_cookie(PROJECT_ROOT)
    bridge_douyin_session(PROJECT_ROOT)
    os.environ["API_ONLY"] = "true"
    os.environ.setdefault("TZ", "Asia/Shanghai")
    install_fcntl_stub()
    sys.path.insert(0, str(root))
    os.chdir(root)
    from app import app as application  # noqa: E402
    from waitress import serve  # noqa: E402

    serve(application, host="127.0.0.1", port=args.port, threads=4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
