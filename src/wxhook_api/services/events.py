from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.core.persistence import finish_before_cancel


EventHandler = Callable[[CallbackEvent], Awaitable[None]]
logger = logging.getLogger(__name__)


class EventDispatcher:
    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[CallbackEvent | None] = asyncio.Queue(maxsize=maxsize)
        self._handlers: list[EventHandler] = []
        self._critical_handlers: set[EventHandler] = set()
        self._worker: asyncio.Task[None] | None = None
        self._accepting = False

    def register(self, handler: EventHandler, *, critical: bool = False) -> None:
        self._handlers.append(handler)
        if critical:
            self._critical_handlers.add(handler)

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._accepting = True
            self._worker = asyncio.create_task(self._run())

    async def enqueue(self, event: CallbackEvent | dict[str, Any]) -> bool:
        if not self._accepting:
            return False
        if not isinstance(event, CallbackEvent):
            event = CallbackEvent.model_validate(event)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            return False
        return True

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if event is None:
                    return
                for handler in tuple(self._handlers):
                    try:
                        await handler(event)
                    except Exception as exc:
                        logger.error("callback handler failed: %s", type(exc).__name__)
                        if handler in self._critical_handlers:
                            break
            finally:
                self._queue.task_done()

    async def stop(self, timeout: float = 5.0) -> None:
        self._accepting = False
        # Do not orphan the worker if lifespan shutdown itself is cancelled.
        await finish_before_cancel(self._stop(timeout))

    async def _stop(self, timeout: float) -> None:
        worker = self._worker
        if worker is None:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            remaining = max(0.0, deadline - loop.time())
            await asyncio.wait_for(self._queue.put(None), timeout=remaining)
            remaining = max(0.0, deadline - loop.time())
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=remaining)
        except asyncio.TimeoutError:
            pass
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            # A timeout may leave both callbacks and the stop sentinel queued.
            # They must not keep join() pending or replay on a later start().
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            self._worker = None
