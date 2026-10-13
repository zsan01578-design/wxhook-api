from urllib.parse import urlsplit

from fastapi import APIRouter, Depends

from wxhook_api.core.config import Settings
from wxhook_api.routes.dependencies import get_settings

router = APIRouter()


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)):
    return {"status": "ok", "hook_base_url": settings.hook_base_url, "hook_port": urlsplit(settings.hook_base_url).port}
