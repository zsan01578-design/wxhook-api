from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse


router = APIRouter()
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


@router.get("/", include_in_schema=False)
async def message_page():
    return FileResponse(WEB_ROOT / "index.html", headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/ui/messages", tags=["消息页面"])
async def inbox_messages(request: Request):
    data = request.app.state.message_inbox.snapshot()
    data["startup"] = request.app.state.launcher_status
    return JSONResponse(data, headers={"Cache-Control": "no-store"})
