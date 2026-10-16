from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from wxhook_api.schemas.events import CallbackEvent


class MessageInbox:
    """Bounded, process-local view of callbacks for the message page."""

    def __init__(self, capacity: int = 100, content_limit: int = 20_000) -> None:
        self.capacity = capacity
        self.content_limit = content_limit
        self._messages: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._total = 0
        self._instance = uuid4().hex

    async def receive(self, event: CallbackEvent) -> None:
        self._total += 1
        self._messages.append({
            "id": self._total,
            "msgid": str(event.msgid),
            "event_type": event.event_type,
            "type": event.type,
            "timestamp": event.timestamp,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "wxid": event.wxid[:256],
            "sender": event.sender[:256],
            "roomid": event.roomid[:256],
            "content": event.content[:self.content_limit],
            "truncated": len(event.content) > self.content_limit,
        })

    def snapshot(self) -> dict[str, Any]:
        return {
            "instance": self._instance,
            "total": self._total,
            "capacity": self.capacity,
            "messages": list(reversed(self._messages)),
        }
