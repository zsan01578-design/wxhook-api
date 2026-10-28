import pytest
from pydantic import ValidationError

from wxhook_api.core.douyin_config import GroupSettings, load_douyin_options, save_group_settings


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8051",
    "http://localhost:9000",
    "http://[::1]:8051",
    "",
])
def test_media_parser_url_accepts_loopback_and_empty(url):
    assert GroupSettings(media_parser_url=url).media_parser_url == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "http://192.168.1.10:8051",
    "http://127.0.0.1",
    "http://127.0.0.1:8051/api",
    "http://user:pass@127.0.0.1:8051",
    "ftp://127.0.0.1:8051",
    "http://127.0.0.1:8051?x=1",
])
def test_media_parser_url_rejects_non_loopback_or_paths(url):
    with pytest.raises(ValidationError):
        GroupSettings(media_parser_url=url)


def test_media_parser_provider_requires_an_address():
    with pytest.raises(ValidationError):
        GroupSettings(parser_provider="media_parser", media_parser_url="")
    settings = GroupSettings(parser_provider="media_parser", media_parser_url="http://127.0.0.1:8051")
    assert settings.parser_provider == "media_parser"


def test_environment_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "douyin.local.yml"
    path.write_text("media-parser-url: http://127.0.0.1:8051\n", encoding="utf-8")
    monkeypatch.setenv("WXHOOK_DOUYIN_CONFIG", str(path))
    monkeypatch.setenv("WXHOOK_MEDIA_PARSER_URL", "http://localhost:9001")
    assert load_douyin_options().settings.media_parser_url == "http://localhost:9001"


def test_media_parser_url_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "douyin.local.yml"
    monkeypatch.setenv("WXHOOK_DOUYIN_CONFIG", str(path))
    monkeypatch.delenv("WXHOOK_MEDIA_PARSER_URL", raising=False)
    options = load_douyin_options()
    assert options.settings.media_parser_url == "http://127.0.0.1:8051"
    updated = save_group_settings(
        options, options.settings.model_copy(update={"media_parser_url": "http://localhost:9001"}))
    assert updated.settings.media_parser_url == "http://localhost:9001"
    assert load_douyin_options().settings.media_parser_url == "http://localhost:9001"
