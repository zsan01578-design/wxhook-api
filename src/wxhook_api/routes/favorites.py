import mimetypes
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from wxhook_api.routes.douyin import LeasedFileResponse
from wxhook_api.routes.manage import GroupId


router = APIRouter(prefix="/api/manage/groups/{group}/favorites", tags=["群收藏"])
ItemId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=24)]


class FavoriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field("", max_length=220)
    tags: list[Tag] = Field(default_factory=list, max_length=12)
    notes: str = Field("", max_length=1000)


class AddFavorite(FavoriteBody):
    text: str = Field(min_length=1, max_length=8192)


class SearchFavorite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=300)
    page: int = Field(1, ge=1, le=1000)


async def service(request, group):
    result = request.app.state.favorites
    exists = await result.tools.read(lambda db: db.execute("SELECT 1 FROM groups WHERE id=?", (group,)).fetchone())
    if not exists:
        raise HTTPException(404, "群聊不存在，请先添加。")
    try:
        result.check(group)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return result


@router.get("")
async def list_favorites(group: GroupId, request: Request, q: str = Query("", max_length=300),
                         page: int = Query(1, ge=1, le=1000)):
    return JSONResponse(await (await service(request, group)).search(group, q, page=page), headers={"Cache-Control":"no-store"})


@router.post("/search")
async def search_favorites(group: GroupId, body: SearchFavorite, request: Request):
    return JSONResponse(await (await service(request, group)).search(group, body.query, page=body.page, semantic=True),
                        headers={"Cache-Control":"no-store"})


@router.post("")
async def add_favorite(group: GroupId, body: AddFavorite, request: Request):
    try:
        return await (await service(request, group)).collect(group, body.text, title=body.title, tags=body.tags, notes=body.notes)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{item_id}/retry")
async def retry_favorite(group: GroupId, item_id: ItemId, request: Request):
    worker = await service(request, group)
    item = await worker.get(group, item_id)
    if not item:
        raise HTTPException(404, "收藏不存在。")
    try:
        return await worker.collect(group, item["source_url"], sender=item["created_by"], retry_id=item_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.put("/{item_id}")
async def edit_favorite(group: GroupId, item_id: ItemId, body: FavoriteBody, request: Request):
    if not body.title.strip():
        raise HTTPException(422, "请填写收藏标题。")
    try:
        saved = await (await service(request, group)).update(group, item_id, body.title.strip(), body.tags, body.notes)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not saved:
        raise HTTPException(404, "收藏不存在。")
    return {"saved":True}


@router.delete("/{item_id}")
async def delete_favorite(group: GroupId, item_id: ItemId, request: Request):
    try:
        removed = await (await service(request, group)).delete(group, item_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not removed:
        raise HTTPException(404, "收藏不存在。")
    return {"deleted":True}


@router.get("/{item_id}/files/{filename}")
async def favorite_file(group: GroupId, item_id: ItemId, filename: str, request: Request, download: bool = False):
    worker = await service(request, group)
    path = await worker.acquire_file(group, item_id, filename)
    if path is None:
        raise HTTPException(404, "收藏文件不存在。")
    try:
        return LeasedFileResponse(path, release=lambda: worker.release_file(item_id), filename=filename if download else None,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            headers={"Cache-Control":"no-store", "X-Content-Type-Options":"nosniff"})
    except Exception:
        worker.release_file(item_id)
        raise
