from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from wxhook_api.schemas.requests import ContactSearchRequest
from wxhook_api.services.groups import GROUP_PATTERN
from wxhook_api.services.wechat_sync import SyncError, records


router = APIRouter(prefix="/api/manage", tags=["群聊管理"])
GroupId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=f"^{GROUP_PATTERN}$")]
MemberId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.@-]+$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]


class GroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: GroupId
    name: Name = ""


class GroupPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name = ""
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""
    pinned: bool = False
    enabled: bool = True
    blocked: bool = False
    douyin_enabled: bool = False
    auto_download: bool = True


class MemberPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name = ""
    blocked: bool = False


class MemberCreate(MemberPatch):
    id: MemberId

    @field_validator("id")
    @classmethod
    def not_a_group(cls, value):
        if value.endswith("@chatroom"):
            raise ValueError("请填写成员 wxid，不能填写群 ID")
        return value


@router.get("/state")
async def state(request: Request):
    manager = request.app.state.groups
    service = request.app.state.douyin
    settings = service.options.settings
    groups = await manager.groups(settings)
    inbox = request.app.state.message_inbox.snapshot()
    callbacks = {**request.app.state.callback_status, "total": inbox["total"], "last_received_at": inbox["messages"][0]["received_at"] if inbox["messages"] else ""}
    media = {"parser_provider": settings.parser_provider, "notify_group": settings.notify_group,
             "send_results": settings.send_results, "feedback_available": service.messenger is not None}
    return JSONResponse({"groups": groups, "blocked_members": await manager.members(blocked_only=True), "startup": request.app.state.launcher_status, "sync": request.app.state.wechat_sync.status, "media": media, "callbacks": callbacks}, headers={"Cache-Control": "no-store"})


@router.post("/sync", status_code=202)
async def sync_now(request: Request):
    if not request.app.state.wechat_sync.request():
        raise HTTPException(409, "自动同步已关闭，请启用 WXHOOK_SYNC_ENABLED 后重启")
    return {"scheduled": True, "sync": request.app.state.wechat_sync.status}


@router.get("/groups/{group}/messages")
async def messages(group: GroupId, request: Request, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200), q: str = Query("", max_length=200)):
    return JSONResponse(await request.app.state.groups.messages(group, page=page, size=size, query=q), headers={"Cache-Control": "no-store"})


@router.post("/groups")
async def add_group(body: GroupCreate, request: Request):
    added = await request.app.state.groups.add_group(body.id, body.name)
    return {"saved": True, "created": added, "id": body.id}


@router.patch("/groups/{group}")
async def update_group(group: GroupId, body: GroupPatch, request: Request):
    if not await request.app.state.groups.update_group(group, body.model_dump(exclude_unset=True)):
        raise HTTPException(404, "群聊不存在，请先添加")
    return {"saved": True}


@router.get("/groups/{group}/members")
async def members(group: GroupId, request: Request):
    return JSONResponse({"members": await request.app.state.groups.members(group)}, headers={"Cache-Control": "no-store"})


@router.post("/groups/{group}/members")
async def add_member(group: GroupId, body: MemberCreate, request: Request):
    if not await request.app.state.groups.save_member(group, body.id, body.model_dump(exclude={"id"}), create=True):
        raise HTTPException(404, "群聊不存在，请先添加")
    return {"saved": True}


@router.patch("/groups/{group}/members/{member}")
async def update_member(group: GroupId, member: MemberId, body: MemberPatch, request: Request):
    if not await request.app.state.groups.save_member(group, member, body.model_dump(exclude_unset=True)):
        raise HTTPException(404, "群成员不存在，请刷新后重试")
    return {"saved": True}


@router.get("/audit")
async def audit(request: Request, group: GroupId | None = None):
    return JSONResponse({"events": await request.app.state.groups.audit(group)}, headers={"Cache-Control": "no-store"})


@router.get("/contacts")
async def contacts(request: Request, keyword: str = Query(min_length=1, max_length=80)):
    result = await request.app.state.hook_client.search_contacts(ContactSearchRequest(keywords=[keyword]))
    try:
        rows = records(result, context="查找微信群聊")
    except SyncError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"groups": [{"id": row["username"], "name": row.get("remark") or row.get("nick_name") or ""} for row in rows if isinstance(row, dict) and isinstance(row.get("username"), str) and row["username"].endswith("@chatroom")]}


async def storage_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "无法读取或保存管理数据，请检查 data 目录的写入权限和磁盘空间"})
