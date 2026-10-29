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
    parser.add_argument("--port", type=int, default=int(os.getenv("WXHOOK_MEDIA_PARSER_PORT", "8051")))
    args = parser.parse_args(argv)
    root = resolve_root(args.root)
    if not (root / "app.py").is_file() or not (root / "src" / "parsers").is_dir():
        print(f"media-parser source not found at {root}", file=sys.stderr)
        return 2
    bridge_xhs_cookie(PROJECT_ROOT)
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
