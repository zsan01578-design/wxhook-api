from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.types import Message, Receive, Scope, Send


class BodyLimitMiddleware:
    def __init__(self, app: Callable[..., Awaitable[Any]], max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for key, value in scope.get("headers", []) if key.lower() == b"content-length"),
            None,
        )
        try:
            declared_length = int(content_length) if content_length is not None else None
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > self.max_bytes:
            await self._reject(send)
            return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    await self._reject(send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        position = 0

        async def replay() -> Message:
            nonlocal position
            if position < len(messages):
                message = messages[position]
                position += 1
                return message
            # After replaying the buffered body, preserve disconnect semantics.
            # Repeating an empty request here can spin forever in file responses.
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send: Send) -> None:
        body = b'{"error":{"code":"body_too_large","message":"request body too large"}}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
