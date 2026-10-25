"""Durable ingress; only the idempotent archive stage is safe to replay."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.groups import GroupManager, now


@dataclass
class Delivery:
    event: CallbackEvent
    key: str
    attempts: int = 0


class CallbackStore:
    max_attempts = 3

    def __init__(self, groups: GroupManager, capacity: int):
        self.groups = groups
        self.capacity = capacity

    async def _write(self, action):
        def write():
            with self.groups._connect() as db:
                return action(db)
        async with self.groups._lock:
            return await finish_before_cancel(asyncio.to_thread(write))

    async def start(self) -> tuple[list[Delivery], int]:
        def initialize(db):
            # callback_receipts is part of the central management schema.
            interrupted = db.execute("UPDATE callback_receipts SET state='unknown',event_json='',updated_at=? WHERE state='dispatched'", (now(),)).rowcount
            db.execute("UPDATE callback_receipts SET state='failed',event_json='',updated_at=? WHERE state='pending' AND attempts>=?", (now(), self.max_attempts))
            deliveries = []
            # Capacity changes must not discard previously acknowledged records.
            for row in db.execute("SELECT receipt_key,event_json,attempts FROM callback_receipts WHERE state='pending' ORDER BY sequence"):
                try:
                    event = CallbackEvent.model_validate_json(row["event_json"])
                except ValueError:
                    db.execute("UPDATE callback_receipts SET state='failed',event_json='',updated_at=? WHERE receipt_key=?", (now(), row["receipt_key"]))
                    continue
                event._receipt_key = row["receipt_key"]
                deliveries.append(Delivery(event, row["receipt_key"], row["attempts"]))
            self._prune(db)
            return deliveries, interrupted
        return await self._write(initialize)

    @staticmethod
    def _prune(db):
        db.execute("""DELETE FROM callback_receipts WHERE state NOT IN ('pending','dispatched')
            AND sequence NOT IN (SELECT sequence FROM callback_receipts
                WHERE state NOT IN ('pending','dispatched') ORDER BY sequence DESC LIMIT 1000)""")

    async def add(self, event: CallbackEvent) -> Delivery | None:
        event = event.model_copy(deep=True)
        key = uuid4().hex
        payload = event.model_dump_json()
        def insert(db):
            if db.execute("SELECT count(*) FROM callback_receipts WHERE state IN ('pending','dispatched')").fetchone()[0] >= self.capacity:
                return None
            db.execute("INSERT INTO callback_receipts(receipt_key,event_json,state,created_at,updated_at) VALUES (?,?,'pending',?,?)", (key, payload, now(), now()))
            self._prune(db)
            event._receipt_key = key
            return Delivery(event, key)
        return await self._write(insert)

    async def attempt(self, delivery: Delivery):
        await self._write(lambda db: db.execute("UPDATE callback_receipts SET attempts=attempts+1,updated_at=? WHERE receipt_key=? AND state='pending'", (now(), delivery.key)).rowcount)
        delivery.attempts += 1

    async def finish(self, delivery: Delivery, state: str):
        if state not in {"dispatched", "done", "failed", "blocked"}:
            raise ValueError("Invalid callback delivery state")
        # No message body is needed after leaving the replayable archive stage.
        await self._write(lambda db: db.execute("UPDATE callback_receipts SET state=?,event_json='',updated_at=? WHERE receipt_key=?", (state, now(), delivery.key)).rowcount)
