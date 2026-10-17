import pytest


@pytest.fixture(autouse=True)
def isolated_management_database(tmp_path, monkeypatch):
    # Tests must never discover groups or change policies in the user's database.
    monkeypatch.setenv("WXHOOK_MANAGEMENT_DB", str(tmp_path / "management.sqlite3"))
