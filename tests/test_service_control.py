import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from wxhook_api.core.service_control import registered_service, watch_shutdown_request


async def test_matching_local_request_stops_server_and_registration_is_cleaned(tmp_path):
    state = tmp_path / "data" / "runtime" / "service.json"
    server = SimpleNamespace(should_exit=False)
    with registered_service(tmp_path) as (stop, token):
        assert json.loads(state.read_text()) == {"pid": os.getpid(), "project": str(tmp_path.resolve()), "token": token}
        stop.write_text("wrong token")
        watcher = asyncio.create_task(watch_shutdown_request(server, stop, token))
        await asyncio.sleep(0)
        assert not server.should_exit and not watcher.done()
        stop.write_text(token)
        await asyncio.wait_for(watcher, 1)
        assert server.should_exit
    assert not state.exists() and not stop.exists()


def test_old_registration_cleanup_does_not_remove_a_new_instance(tmp_path):
    state = tmp_path / "data" / "runtime" / "service.json"
    with registered_service(tmp_path):
        state.write_text(json.dumps({"token": "replacement"}))
    assert json.loads(state.read_text())["token"] == "replacement"


async def test_cancelled_shutdown_watcher_does_not_stop_server(tmp_path):
    server = SimpleNamespace(should_exit=False)
    with registered_service(tmp_path) as (stop, token):
        watcher = asyncio.create_task(watch_shutdown_request(server, stop, token))
        await asyncio.sleep(0)
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher
        assert not server.should_exit
