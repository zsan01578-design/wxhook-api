from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from wxhook_api.core.config import Settings
from wxhook_api.routes.dependencies import get_settings

router = APIRouter()


@router.get("/health")
async def health(request: Request, settings: Settings = Depends(get_settings)):
    return {"status": "ok", "hook_base_url": settings.hook_base_url, "hook_port": urlsplit(settings.hook_base_url).port, "runtime_revision": request.app.state.runtime_revision}


@router.get("/ready")
async def ready(request: Request):
    state = request.app.state
    processing = state.event_dispatcher.snapshot()
    account_blocked = bool(state.groups.account_error)
    tasks = {"media": [state.douyin._worker, state.douyin._notice_worker],
             "group_tools": [state.group_tools._worker, state.group_tools._scheduler, state.group_tools.reminders._worker],
             "summary": [state.chat_summary._task]}
    if state.chatbot.options.enabled:
        tasks["chatbot"] = state.chatbot._workers or [None]
    if state.wechat_sync.options.enabled:
        tasks["sync"] = [state.wechat_sync._task]
    if state.douyin.options.retention.enabled:
        tasks["media_cleanup"] = [state.douyin._cleanup_worker]
    if state.backups.enabled:
        tasks["backup"] = [state.backups._task]
    components = {name: all(task is not None and not task.done() for task in workers) for name, workers in tasks.items()}
    is_ready = processing["ready"] and processing["ingress_storage_available"] and not account_blocked and all(components.values())
    return JSONResponse({"status": "ready" if is_ready else "unavailable", "processing": processing,
                         "account_blocked": account_blocked, "components": components}, status_code=200 if is_ready else 503,
                        headers={"Cache-Control": "no-store"})
