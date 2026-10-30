import importlib.util
import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "scripts" / "media-parser-server.py"


def load_launcher():
    spec = importlib.util.spec_from_file_location("media_parser_server", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_precedence(tmp_path, monkeypatch):
    module = load_launcher()
    explicit = tmp_path / "explicit"
    configured = tmp_path / "configured"
    monkeypatch.setenv("WXHOOK_MEDIA_PARSER_ROOT", str(configured))
    assert module.resolve_root(str(explicit)) == explicit.resolve()
    assert module.resolve_root(None) == configured.resolve()
    monkeypatch.delenv("WXHOOK_MEDIA_PARSER_ROOT")
    assert module.resolve_root(None) == (PROJECT_ROOT.parent / "media-parser").resolve()


def test_xhs_cookie_is_bridged_without_overwriting_env(tmp_path, monkeypatch):
    module = load_launcher()
    config = tmp_path / "parse-video.local.yml"
    config.write_text("xiaohongshu-cookie: 'a1=private; web_session=test'\n", encoding="utf-8")
    monkeypatch.setenv("WXHOOK_PARSE_VIDEO_CONFIG", str(config))
    monkeypatch.delenv("XHS_COOKIE", raising=False)
    try:
        module.bridge_xhs_cookie(PROJECT_ROOT)
        assert os.environ["XHS_COOKIE"] == "a1=private; web_session=test"

        os.environ["XHS_COOKIE"] = "existing"
        module.bridge_xhs_cookie(PROJECT_ROOT)
        assert os.environ["XHS_COOKIE"] == "existing"
    finally:
        # The bridge writes straight to os.environ, so undo it here.
        os.environ.pop("XHS_COOKIE", None)


def test_parse_video_env_cookie_takes_precedence_and_needs_no_file(tmp_path, monkeypatch):
    module = load_launcher()
    monkeypatch.delenv("XHS_COOKIE", raising=False)
    monkeypatch.setenv("PARSE_VIDEO_XHS_COOKIE", "a1=env; web_session=env")
    try:
        monkeypatch.setenv("WXHOOK_PARSE_VIDEO_CONFIG", str(tmp_path / "missing.yml"))
        module.bridge_xhs_cookie(PROJECT_ROOT)
        assert os.environ["XHS_COOKIE"] == "a1=env; web_session=env"

        config = tmp_path / "parse-video.local.yml"
        config.write_text("xiaohongshu-cookie: 'a1=file; web_session=file'\n", encoding="utf-8")
        monkeypatch.setenv("WXHOOK_PARSE_VIDEO_CONFIG", str(config))
        os.environ.pop("XHS_COOKIE", None)
        module.bridge_xhs_cookie(PROJECT_ROOT)
        assert os.environ["XHS_COOKIE"] == "a1=env; web_session=env"
    finally:
        os.environ.pop("XHS_COOKIE", None)


def test_missing_root_exits_with_message(tmp_path, capsys):
    module = load_launcher()
    assert module.main(["--root", str(tmp_path / "missing")]) == 2
    assert "not found" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="fcntl is unavailable on Windows only")
def test_fcntl_stub_enables_app_import():
    module = load_launcher()
    sys.modules.pop("fcntl", None)
    module.install_fcntl_stub()
    try:
        import fcntl
        assert callable(fcntl.flock)
    finally:
        # Never leak the injected stub into other tests.
        sys.modules.pop("fcntl", None)


@pytest.fixture
def douyin_session(tmp_path, monkeypatch):
    for name in ("DOUYIN_COOKIE", "DY_COOKIE", "DOUYIN_UIFID", "DY_UIFID",
                 "PARSE_VIDEO_DOUYIN_COOKIE", "PARSE_VIDEO_DOUYIN_UIFID"):
        monkeypatch.setenv(name, "")
    # Optional source variables distinguish unset from explicitly empty.
    monkeypatch.delenv("PARSE_VIDEO_DOUYIN_COOKIE")
    monkeypatch.delenv("PARSE_VIDEO_DOUYIN_UIFID")
    path = tmp_path / "parse-video.local.yml"
    monkeypatch.setenv("WXHOOK_PARSE_VIDEO_CONFIG", str(path))
    return load_launcher(), path


def test_douyin_owned_cookie_and_uifid_are_bridged_locally(douyin_session, capsys):
    module, path = douyin_session
    path.write_text("douyin-cookie: 'sessionid=private-test'\ndouyin-uifid: 'owned-test-uifid'\n", encoding="utf-8")
    module.bridge_douyin_session(PROJECT_ROOT)
    assert os.environ["DOUYIN_COOKIE"] == "sessionid=private-test"
    assert os.environ["DOUYIN_UIFID"] == "owned-test-uifid"
    assert not capsys.readouterr().out


def test_douyin_existing_aliases_take_precedence(douyin_session, monkeypatch):
    module, path = douyin_session
    path.write_text("douyin-cookie: 'sessionid=file'\ndouyin-uifid: 'file-uifid'\n", encoding="utf-8")
    monkeypatch.setenv("DY_COOKIE", "sessionid=existing")
    monkeypatch.setenv("DOUYIN_UIFID", "existing-uifid")
    module.bridge_douyin_session(PROJECT_ROOT)
    assert not os.environ["DOUYIN_COOKIE"]
    assert os.environ["DY_COOKIE"] == "sessionid=existing"
    assert os.environ["DOUYIN_UIFID"] == "existing-uifid"


def test_douyin_environment_sources_work_without_a_file(douyin_session, monkeypatch):
    module, _ = douyin_session
    monkeypatch.setenv("PARSE_VIDEO_DOUYIN_COOKIE", "sessionid=env")
    monkeypatch.setenv("PARSE_VIDEO_DOUYIN_UIFID", "env-uifid")
    module.bridge_douyin_session(PROJECT_ROOT)
    assert os.environ["DOUYIN_COOKIE"] == "sessionid=env"
    assert os.environ["DOUYIN_UIFID"] == "env-uifid"


@pytest.mark.parametrize("value", ["invalid\nheader", "非ASCII", "x" * 20000])
def test_douyin_invalid_session_values_are_not_forwarded(douyin_session, value):
    import yaml

    module, path = douyin_session
    path.write_text(yaml.safe_dump({"douyin-cookie": value, "douyin-uifid": value}), encoding="utf-8")
    module.bridge_douyin_session(PROJECT_ROOT)
    assert not os.environ["DOUYIN_COOKIE"] and not os.environ["DOUYIN_UIFID"]
