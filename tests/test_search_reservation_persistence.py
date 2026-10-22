import asyncio
from datetime import date, timedelta
import json
import threading

import pytest

from tests.test_web_search import FakeSearchProvider
from wxhook_api.services.web_search import SearchSettings, WebSearchConfiguration


async def configured(tmp_path, daily_limit):
    current = [date(2026, 10, 21)]
    provider = FakeSearchProvider()
    config = WebSearchConfiguration(tmp_path / "search.json", provider=provider, now=lambda: current[0])
    await config.save(SearchSettings(enabled=True, api_key="synthetic-key", daily_limit=daily_limit))
    assert await config.context("existing")
    return config, provider, current


@pytest.mark.parametrize("rollover", [False, True])
async def test_failed_reservations_preserve_usage_and_recover_without_restart(tmp_path, monkeypatch, rollover):
    config, provider, current = await configured(tmp_path, daily_limit=2)
    previous = (config.usage_date, config.used_today)
    disk = config.path.read_bytes()
    if rollover:
        current[0] += timedelta(days=1)
    original_write = config._write

    def fail(payload):
        raise OSError("private storage location")

    monkeypatch.setattr(config, "_write", fail)
    for _ in range(3):
        assert await config.context("must not send") is None
        assert (config.usage_date, config.used_today) == previous
        assert config.path.read_bytes() == disk
        assert config.last_error and "private storage location" not in config.last_error
    assert provider.queries == ["existing"]
    monkeypatch.setattr(config, "_write", original_write)
    assert await config.context("recovered")
    assert provider.queries == ["existing", "recovered"]
    expected = 1 if rollover else 2
    restored = WebSearchConfiguration(config.path, now=lambda: current[0])
    assert config.snapshot()["used_today"] == restored.snapshot()["used_today"] == expected


@pytest.mark.parametrize("rollover", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_usage_changes_only_after_reservation_commit(tmp_path, monkeypatch, rollover, write_fails, cancelled):
    config, provider, current = await configured(tmp_path, daily_limit=3)
    previous = (config.usage_date, config.used_today)
    disk = config.path.read_bytes()
    if rollover:
        current[0] += timedelta(days=1)
    entered, release = threading.Event(), threading.Event()
    original_write = config._write

    def delayed_write(payload):
        entered.set()
        assert release.wait(5), "The test must release the reservation write"
        if write_fails:
            raise OSError("synthetic storage failure")
        original_write(payload)

    monkeypatch.setattr(config, "_write", delayed_write)
    pending = asyncio.create_task(config.context("pending"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancelled:
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        assert not pending.done() and config._lock.locked()
        assert (config.usage_date, config.used_today) == previous
        assert config.path.read_bytes() == disk
        assert config.snapshot()["used_today"] == (0 if rollover else 1)
        release.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
        else:
            result = await asyncio.wait_for(pending, 2)
            assert bool(result) is not write_fails
        assert not config._lock.locked()
        expected = previous if write_fails else (current[0].isoformat(), 1 if rollover else 2)
        assert (config.usage_date, config.used_today) == expected
        saved = json.loads(config.path.read_text(encoding="utf-8"))
        assert (saved["usage_date"], saved["used_today"]) == expected
        assert provider.queries == (["existing"] if cancelled or write_fails else ["existing", "pending"])
        monkeypatch.setattr(config, "_write", original_write)
        assert await config.context("following")
        restored = WebSearchConfiguration(config.path, now=lambda: current[0])
        assert restored.snapshot()["used_today"] == config.snapshot()["used_today"]
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
