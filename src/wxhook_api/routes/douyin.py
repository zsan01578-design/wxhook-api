import mimetypes
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from wxhook_api.core.douyin_config import GroupSettings
from wxhook_api.routes.ui import WEB_ROOT


router = APIRouter()


class LeasedFileResponse(FileResponse):
    def __init__(self, *args, release, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.release()


@router.get("/douyin", include_in_schema=False)
async def douyin_page():
    return FileResponse(WEB_ROOT / "douyin.html", headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/api/douyin/config", tags=["群聊抖音解析"])
async def get_config(request: Request):
    options = request.app.state.douyin.options
    return {"settings": options.settings.model_dump(), "output_dir": str(options.output_dir), "max_file_mb": options.max_file_bytes // (1024 * 1024), "feedback_available": request.app.state.douyin.messenger is not None, "supported_parsers": ["parse_video", "api", "builtin", "media_parser"], "cleanup_policy": asdict(options.retention)}


@router.post("/api/douyin/config", tags=["群聊抖音解析"])
async def set_config(body: GroupSettings, request: Request):
    try:
        await request.app.state.douyin.configure(body)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法保存群配置，请检查项目目录写入权限") from exc
    return {"saved": True, "settings": body.model_dump()}


@router.get("/api/douyin/jobs", tags=["群聊抖音解析"])
async def list_jobs(request: Request):
    from fastapi.responses import JSONResponse

    return JSONResponse(request.app.state.douyin.snapshot(), headers={"Cache-Control": "no-store"})


@router.get("/api/douyin/files/{job_id}/{filename}", tags=["群聊抖音解析"])
async def download_file(job_id: str, filename: str, request: Request, download: bool = False):
    service = request.app.state.douyin
    path = await service.acquire_file(job_id, filename)
    if path is None:
        if service.is_file_cleaned(job_id, filename):
            raise HTTPException(status_code=410, detail="文件已按保留规则清理")
        raise HTTPException(status_code=404, detail="文件不存在或不属于当前解析记录")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        return LeasedFileResponse(path, release=lambda: service.release_file(job_id), media_type=media_type, filename=filename if download else None, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except Exception:
        service.release_file(job_id)
        raise
