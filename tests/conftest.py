import pytest


@pytest.fixture(autouse=True)
def isolated_management_database(tmp_path, monkeypatch):
    # Tests must never discover groups or change policies in the user's database.
    monkeypatch.setenv("WXHOOK_MANAGEMENT_DB", str(tmp_path / "management.sqlite3"))
    # Legacy bridge tests assert exact Hook calls; synchronization has dedicated integration tests.
    monkeypatch.setenv("WXHOOK_SYNC_ENABLED", "false")
    # Real outbound messages are tested only with an explicitly mocked Hook.
    monkeypatch.setenv("WXHOOK_DOUYIN_FEEDBACK_ENABLED", "false")
    # Legacy tests must never clean the user's real download directory.
    monkeypatch.setenv("WXHOOK_MEDIA_CLEANUP_ENABLED", "false")
    # Mention reply tests explicitly inject a service with a mocked Hook.
    monkeypatch.setenv("WXHOOK_CHATBOT_ENABLED", "false")
    monkeypatch.setenv("WXHOOK_CHAT_MODEL_CONFIG", str(tmp_path / "chat-model.local.json"))
    monkeypatch.setenv("WXHOOK_CHATBOT_CONFIG", str(tmp_path / "chatbot.local.yml"))
    monkeypatch.setenv("WXHOOK_SEARCH_CONFIG", str(tmp_path / "search.local.json"))
    monkeypatch.setenv("WXHOOK_AI_USAGE_DB", str(tmp_path / "ai-usage.sqlite3"))
    monkeypatch.setenv("WXHOOK_ALLOWED_HOSTS", "test,testserver")
    monkeypatch.delenv("WXHOOK_ADMIN_TOKEN", raising=False)
