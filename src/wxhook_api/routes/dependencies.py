from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from wxhook_api.clients.hook_client import HookClient, HookResponse
from wxhook_api.core.config import Settings
from wxhook_api.core.errors import InvalidJsonError
from wxhook_api.services.events import EventDispatcher


def get_hook_client(request: Request) -> HookClient:
    return request.app.state.hook_client


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_dispatcher(request: Request) -> EventDispatcher:
    return request.app.state.event_dispatcher


def hook_response(response: HookResponse) -> JSONResponse:
    return JSONResponse(status_code=response.status_code, content=response.data)


async def optional_body(request: Request) -> dict[str, Any]:
    body = await request.body()
    if not body:
        return {}
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidJsonError from exc
    if not isinstance(value, dict):
        raise RequestValidationError([{"type": "dict_type", "loc": ("body",), "msg": "request body must be an object"}])
    return value
