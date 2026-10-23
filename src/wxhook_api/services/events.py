from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.core.callback_diagnostics import utc_now
from wxhook_api.core.errors import AccountMismatchError, HookTransportError
from wxhook_api.services.callback_store import CallbackStore, Delivery
from wxhook_api.services.groups import event_group
from wxhook_api.services.wechat_sync import SyncError


EventHandler = Callable[[CallbackEvent], Awaitable[None]]
logger = logging.getLogger(__name__)


def conversation(event: CallbackEvent) -> str:
    if isinstance(event, Delivery):
        event = event.event
    return event_group(event) or event.wxid or event.sender


class ConversationQueue(asyncio.Queue):
    """Dispatch the oldest available conversation without blocking other groups."""

    def __init__(self, maxsize: int):
        super().__init__(maxsize)
        self._busy: set[str] = set()
        self._changed = asyncio.Event()

    def put_nowait(self, item):
        super().put_nowait(item)
        self._changed.set()

    async def get(self):
        while True:
            self._changed.clear()
            for index, event in enumerate(self._queue):
                key = conversation(event) if event is not None else None
                if key is None or key not in self._busy:
                    del self._queue[index]
                    if key is not None:
                        self._busy.add(key)
                    self._wakeup_next(self._putters)
                    return event
            await self._changed.wait()

    def finish(self, key: str | None):
        if key is not None:
            self._busy.discard(key)
        self.task_done()
        self._changed.set()


class EventDispatcher:
    def __init__(self, maxsize: int = 1000, *, workers: int = 4, store: CallbackStore | None = None) -> None:
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("callback workers must be between 1 and 4")
        self.workers = workers
        self._queue = ConversationQueue(maxsize=maxsize)
        self._handlers: list[EventHandler] = []
        self._critical_handlers: set[EventHandler] = set()
        self._worker: asyncio.Task[None] | None = None
        self._accepting = False
        self.store = store
        self.retry_delays = (1.0, 3.0)
        self.storage_retry_delays = (1.0, 3.0, 10.0, 30.0)
        self._storage_waiting = 0
        self._enqueue_lock = asyncio.Lock()
        self._consumers: list[asyncio.Task] = []
        self._recovery_task: asyncio.Task | None = None
        self._recovering = False
        self._inflight = 0
        self._progress_at = time.monotonic()
        self.rejection_code = "callback_unavailable"
        self._stats = {"processed": 0, "failed": 0, "handler_failures": 0, "archive_retries": 0, "storage_retries": 0,
                       "recovered": 0, "interrupted": 0, "ingress_storage_available": True,
                       "last_success_at": "", "last_error": None}

    def snapshot(self) -> dict:
        running = self._worker is not None and not self._worker.done()
        stalled = bool(self._queue.qsize() and time.monotonic() - self._progress_at > 120)
        return {**self._stats, "running": running, "accepting": self._accepting and running,
                "ready": running and self._accepting and not self._recovering and not stalled and not self._storage_waiting,
                "recovering": self._recovering, "stalled": stalled,
                "storage_waiting": self._storage_waiting,
                "workers": sum(not task.done() for task in self._consumers), "configured_workers": self.workers,
                "pending": self._queue.qsize(), "capacity": self._queue.maxsize, "inflight": self._inflight,
                "durable": self.store is not None}

    def _error(self, stage: str, exc: BaseException):
        # Store categories only; upstream bodies, message text and paths are private.
        code = ("account_blocked" if isinstance(exc, AccountMismatchError) else
                "storage_unavailable" if isinstance(exc, (sqlite3.Error, OSError)) else
                "hook_unavailable" if isinstance(exc, (HookTransportError, SyncError)) else "handler_failed")
        self._stats["last_error"] = {"at": utc_now(), "stage": stage, "code": code}
        logger.error("callback %s failed: %s", stage, code)

    def register(self, handler: EventHandler, *, critical: bool = False) -> None:
        self._handlers.append(handler)
        if critical:
            self._critical_handlers.add(handler)

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            recovered, interrupted = await self.store.start() if self.store else ([], 0)
            self._stats["recovered"] += len(recovered)
            self._stats["interrupted"] += interrupted
            self._recovering = bool(recovered)
            self._progress_at = time.monotonic()
            self._accepting = True
            self._worker = asyncio.create_task(self._run(recovered))

    async def enqueue(self, event: CallbackEvent | dict[str, Any]) -> bool:
        if not isinstance(event, CallbackEvent):
            event = CallbackEvent.model_validate(event)

        async def accept():
            if not self.snapshot()["ready"]:
                self.rejection_code = "callback_unavailable"
                return False
            if self._queue.full():
                self.rejection_code = "callback_queue_full"
                return False
            try:
                delivery = await self.store.add(event) if self.store else event
            except (sqlite3.Error, OSError) as exc:
                self._stats["ingress_storage_available"] = False
                self._error("ingress", exc)
                self.rejection_code = "callback_storage_unavailable"
                return False
            if delivery is None:
                self.rejection_code = "callback_queue_full"
                return False
            self._stats["ingress_storage_available"] = True
            if not self._accepting or self._worker is None or self._worker.done():
                self.rejection_code = "callback_unavailable"
                return False
            if not self._queue.qsize() and not self._inflight:
                self._progress_at = time.monotonic()
            self._queue.put_nowait(delivery)
            return True

        async with self._enqueue_lock:
            # Finish the durable write and memory enqueue together, even when
            # the HTTP client disconnects or cancels more than once.
            return await finish_before_cancel(accept())

    async def _run(self, recovered=()) -> None:
        workers = [asyncio.create_task(self._consume()) for _ in range(self.workers)]
        self._consumers = workers
        async def feed():
            for delivery in recovered:
                await self._queue.put(delivery)
            self._recovering = False
        self._recovery_task = asyncio.create_task(feed())
        try:
            await asyncio.gather(*workers, self._recovery_task)
        finally:
            self._accepting = False
            # Own every consumer so cancelling the supervisor cannot leak
            # callbacks or release their conversation before cleanup finishes.
            for worker in [*workers, self._recovery_task]:
                worker.cancel()
            await asyncio.gather(*workers, self._recovery_task, return_exceptions=True)
            self._consumers = []

    async def _receipt_write(self, stage: str, operation):
        # Keep ownership of the receipt and its conversation while storage is
        # unavailable. Retrying a status write must never replay automation.
        failures = 0
        waiting = False
        try:
            while True:
                try:
                    return await operation()
                except (sqlite3.Error, OSError) as exc:
                    if not waiting:
                        waiting = True
                        self._storage_waiting += 1
                    self._stats["storage_retries"] += 1
                    self._error(stage, exc)
                    delay = self.storage_retry_delays[min(failures, len(self.storage_retry_delays) - 1)]
                    failures += 1
                    await asyncio.sleep(delay)
        finally:
            if waiting:
                self._storage_waiting -= 1

    async def _archive(self, delivery: Delivery) -> bool:
        for _ in range(self.store.max_attempts - delivery.attempts):
            try:
                await self._receipt_write("attempt", lambda: self.store.attempt(delivery))
                for handler in tuple(self._handlers):
                    if handler in self._critical_handlers:
                        await handler(delivery.event)
                # Persist the no-replay boundary BEFORE any automatic side effects.
                await self._receipt_write("dispatch", lambda: self.store.finish(delivery, "dispatched"))
                return True
            except Exception as exc:
                self._stats["handler_failures"] += 1
                self._error("archive", exc)
                retryable = isinstance(exc, (sqlite3.Error, OSError, HookTransportError, SyncError)) and not isinstance(exc, AccountMismatchError)
                if not retryable or delivery.attempts >= self.store.max_attempts:
                    state = "blocked" if isinstance(exc, AccountMismatchError) else "failed"
                    await self._receipt_write("finalize", lambda: self.store.finish(delivery, state))
                    return False
                self._stats["archive_retries"] += 1
                await asyncio.sleep(self.retry_delays[min(delivery.attempts - 1, len(self.retry_delays) - 1)])
        return False

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            key = conversation(item) if item is not None else None
            event = item.event if isinstance(item, Delivery) else item
            self._inflight += event is not None
            success = True
            try:
                if event is None:
                    return
                if isinstance(item, Delivery) and not await self._archive(item):
                    success = False
                    continue
                for handler in tuple(self._handlers):
                    if isinstance(item, Delivery) and handler in self._critical_handlers:
                        continue
                    try:
                        await handler(event)
                    except Exception as exc:
                        success = False
                        self._stats["handler_failures"] += 1
                        self._error("handler", exc)
                        if handler in self._critical_handlers:
                            break
                if isinstance(item, Delivery):
                    state = "done" if success else "failed"
                    await self._receipt_write("finalize", lambda: self.store.finish(item, state))
            except asyncio.CancelledError:
                success = False
                if event is not None:
                    self._stats["interrupted"] += 1
                raise
            finally:
                if event is not None:
                    self._inflight -= 1
                    self._stats["processed" if success else "failed"] += 1
                    if success:
                        self._stats["last_success_at"] = utc_now()
                    self._progress_at = time.monotonic()
                self._queue.finish(key)

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
            async with self._enqueue_lock:
                if self._recovery_task is not None:
                    remaining = max(0.0, deadline - loop.time())
                    await asyncio.wait_for(asyncio.gather(self._recovery_task, return_exceptions=True), timeout=remaining)
                for _ in range(self.workers):
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
