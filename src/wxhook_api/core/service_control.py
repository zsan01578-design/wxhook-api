"""Local restart handshake; no shutdown endpoint is exposed over HTTP."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
from uuid import uuid4

from wxhook_api.core.config import PROJECT_ROOT


@contextmanager
def registered_service(root: Path = PROJECT_ROOT):
    directory = root / "data" / "runtime"
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / "service.json"
    token = uuid4().hex
    stop = directory / f"stop-{token}.request"
    temporary = directory / f"service-{token}.tmp"
    record = {"pid": os.getpid(), "project": str(root.resolve()), "token": token}
    try:
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, state)
        yield stop, token
    finally:
        temporary.unlink(missing_ok=True)
        stop.unlink(missing_ok=True)
        try:
            if json.loads(state.read_text(encoding="utf-8")).get("token") == token:
                state.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


async def watch_shutdown_request(server, stop: Path, token: str):
    while True:
        try:
            if stop.read_text(encoding="utf-8").strip() == token:
                server.should_exit = True
                return
        except (OSError, UnicodeError):
            pass
        await asyncio.sleep(0.25)
