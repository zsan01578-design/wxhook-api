from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.config import Settings
from wxhook_api.core.callback_diagnostics import utc_now
from wxhook_api.routes.dependencies import get_dispatcher, get_hook_client, get_settings, hook_response
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.schemas.requests import CallbackRequest
from wxhook_api.schemas.responses import RetResponse
from wxhook_api.services.events import EventDispatcher

router = APIRouter()


def _allowed_callback(url: str, settings: Settings) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.fragment:
            return False
        parsed.port
    except ValueError:
        return False
    return url.rstrip("/") in {allowed.rstrip("/") for allowed in settings.allowed_callback_urls}


@router.post("/set_callback", responses={200: {"model": RetResponse}})
async def set_callback(body: CallbackRequest, settings: Settings = Depends(get_settings), client: HookClient = Depends(get_hook_client)):
    if not _allowed_callback(body.url, settings):
        return JSONResponse(status_code=422, content={"error": {"code": "callback_url_not_allowed", "message": "callback URL is not allowed"}})
    return hook_response(await client.set_callback(body))


@router.post("/receiveChatBotMsg", responses={200: {"model": RetResponse}})
async def receive_callback(event: CallbackEvent, request: Request, dispatcher: EventDispatcher = Depends(get_dispatcher)):
    if not await dispatcher.enqueue(event):
        return JSONResponse(status_code=503, content={"error": {"code": "callback_queue_full", "message": "callback queue is full"}})
    request.app.state.callback_status["accepted"] += 1
    request.app.state.callback_status["last_accepted_at"] = utc_now()
    return {"ret": 0, "retmsg": "accepted"}


@router.get("/api/callback/status", tags=["消息回调"])
async def callback_status(request: Request):
    return JSONResponse(request.app.state.callback_status, headers={"Cache-Control": "no-store"})
