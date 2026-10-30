import asyncio
from collections import deque

from fastapi import APIRouter, HTTPException, Request, Response

from wxhook_api.schemas.requests import ParseLinkRequest
from wxhook_api.services.douyin_parser import DouyinError
from wxhook_api.services.media_parser import MediaParserClient, enrich_live_photos
from wxhook_api.services.parse_video import ParseVideoParser


router = APIRouter()

_RATE_LIMIT = 60
_RATE_WINDOW = 60.0
_QUEUE_TIMEOUT = 5.0


def attach(app) -> None:
    app.state.parse_video_parser = ParseVideoParser()
    app.state.parse_slots = asyncio.Semaphore(2)
    app.state.parse_rate = deque()


def _check_rate(app) -> None:
    now = asyncio.get_running_loop().time()
    bucket = app.state.parse_rate
    while bucket and now - bucket[0] > _RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="解析请求过于频繁，请稍后重试")
    bucket.append(now)


async def _acquire_slot(app) -> asyncio.Semaphore:
    slots = app.state.parse_slots
    try:
        await asyncio.wait_for(slots.acquire(), _QUEUE_TIMEOUT)
    except TimeoutError:
        raise HTTPException(status_code=429, detail="解析任务排队过多，请稍后重试") from None
    return slots


@router.post("/api/parse", tags=["分享解析"])
async def parse_link(body: ParseLinkRequest, request: Request, response: Response):
    _check_rate(request.app)
    slots = await _acquire_slot(request.app)
    try:
        parser = request.app.state.parse_video_parser
        settings = request.app.state.douyin.options.settings
        used_fallback = False
        try:
            media = await parser.parse(body.url)
        except DouyinError as primary_error:
            if not settings.media_parser_url:
                raise
            try:
                media = await MediaParserClient(
                    settings.media_parser_url, transport=getattr(parser, "transport", None)).parse(body.url)
                used_fallback = True
            except DouyinError as fallback_error:
                raise DouyinError(
                    f"{str(primary_error)[:300]}；media-parser 兜底也失败：{str(fallback_error)[:200]}") from fallback_error
        media, notice, _ = await enrich_live_photos(
            media, body.url, settings.media_parser_url if not used_fallback else "",
            transport=getattr(parser, "transport", None))
    except DouyinError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        slots.release()
    response.headers["Cache-Control"] = "no-store"
    result = {"platform": media.platform, "kind": media.kind, "title": media.title, "author": media.author, "assets": media.assets}
    if notice:
        result["media_notice"] = notice
    return result
