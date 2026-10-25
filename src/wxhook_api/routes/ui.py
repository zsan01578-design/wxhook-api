from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse


router = APIRouter()
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


@router.get("/", include_in_schema=False)
@router.get("/manage", include_in_schema=False)
async def management_page():
    return FileResponse(WEB_ROOT / "manage.html", headers=PAGE_HEADERS)


@router.get("/messages", include_in_schema=False)
async def message_page():
    return FileResponse(WEB_ROOT / "index.html", headers=PAGE_HEADERS)


@router.get("/ui/messages", tags=["消息页面"])
async def inbox_messages(request: Request):
    data = request.app.state.message_inbox.snapshot()
    data["startup"] = request.app.state.launcher_status
    return JSONResponse(data, headers={"Cache-Control": "no-store"})
