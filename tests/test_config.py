from pathlib import Path

import pytest

from wxhook_api.core.config import Settings, load_settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "version.yml"


def test_existing_config_separates_hook_and_service_ports():
    settings = load_settings(CONFIG_PATH)

    assert settings.hook_base_url == "http://127.0.0.1:30001"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.hook_config["image-auto-download"] is True


def test_environment_overrides_yaml_and_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "version.yml"
    config_path.write_text("start-port: 31001\ncallback-url: http://localhost:9000/callback\n", encoding="utf-8")
    monkeypatch.setenv("WXHOOK_CONFIG", str(config_path))
    monkeypatch.setenv("WXHOOK_PORT", "8100")
    monkeypatch.setenv("WXHOOK_HOOK_BASE_URL", "http://127.0.0.1:31002/")
    monkeypatch.setenv("WXHOOK_ALLOWED_CALLBACK_URLS", "http://localhost:8100/receiveChatBotMsg, http://127.0.0.1:8100/receiveChatBotMsg")

    settings = Settings.from_environment()

    assert settings.port == 8100
    assert settings.hook_base_url == "http://127.0.0.1:31002"
    assert settings.allowed_callback_urls == (
        "http://localhost:8100/receiveChatBotMsg",
        "http://127.0.0.1:8100/receiveChatBotMsg",
    )
    assert settings.hook_config["start-port"] == 31001


def test_config_path_does_not_depend_on_current_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    settings = load_settings(CONFIG_PATH)

    assert settings.hook_config["start-port"] == 30001


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("start-port: nope\n", "start-port"),
        ("start-port: 70000\n", "start-port"),
        ("- list\n", "mapping"),
    ],
)
def test_invalid_yaml_is_rejected(tmp_path, contents, message):
    config_path = tmp_path / "version.yml"
    config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_settings(config_path)


def test_service_cannot_proxy_to_itself(monkeypatch, tmp_path):
    config_path = tmp_path / "version.yml"
    config_path.write_text("start-port: 30001\n", encoding="utf-8")
    monkeypatch.setenv("WXHOOK_HOST", "127.0.0.1")
    monkeypatch.setenv("WXHOOK_PORT", "30001")
    monkeypatch.setenv("WXHOOK_HOOK_BASE_URL", "http://127.0.0.1:30001")

    with pytest.raises(ValueError, match="same address"):
        Settings.from_environment(config_path)
