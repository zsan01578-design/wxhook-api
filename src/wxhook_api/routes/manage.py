from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from wxhook_api.schemas.requests import ContactSearchRequest
from wxhook_api.core.chatbot_config import MentionSettings
from wxhook_api.services.groups import GROUP_PATTERN
from wxhook_api.services.chat_model import ModelDefinition, ModelError, ModelSettings
from wxhook_api.services.image_model import ImageModelDefinition
from wxhook_api.services.web_search import SearchError, SearchSettings
from wxhook_api.services.wechat_sync import SyncError, records
from wxhook_api.services.chat_summary import local_date


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
    chat_enabled: bool = False
    summary_enabled: bool = False


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
    tool_groups = await request.app.state.group_tools.enabled_groups()
    for group in groups:
        group["tools_enabled"] = group["id"] in tool_groups
    inbox = request.app.state.message_inbox.snapshot()
    callbacks = {**request.app.state.callback_status, "processing": request.app.state.event_dispatcher.snapshot(), "total": inbox["total"], "last_received_at": inbox["messages"][0]["received_at"] if inbox["messages"] else ""}
    media = {"parser_provider": settings.parser_provider, "notify_group": settings.notify_group,
             "send_results": settings.send_results, "feedback_available": service.messenger is not None}
    runtime = {"backup": request.app.state.backups.snapshot(), "media_storage": service.storage.snapshot(),
               "revision": request.app.state.runtime_revision}
    return JSONResponse({"groups": groups, "blocked_members": await manager.members(blocked_only=True), "startup": request.app.state.launcher_status, "sync": request.app.state.wechat_sync.status, "media": media, "callbacks": callbacks, "runtime": runtime, "chatbot": request.app.state.chatbot.snapshot()}, headers={"Cache-Control": "no-store"})


@router.post("/sync", status_code=202)
async def sync_now(request: Request):
    if not request.app.state.wechat_sync.request():
        raise HTTPException(409, "自动同步已关闭，请启用 WXHOOK_SYNC_ENABLED 后重启")
    return {"scheduled": True, "sync": request.app.state.wechat_sync.status}


@router.get("/chat-model")
async def get_chat_model(request: Request):
    return JSONResponse(request.app.state.chat_model.snapshot(), headers={"Cache-Control": "no-store"})


@router.get("/chat-search")
async def get_chat_search(request: Request):
    return JSONResponse(request.app.state.chat_search.snapshot(), headers={"Cache-Control": "no-store"})


@router.get("/image-model")
async def get_image_model(request: Request):
    return JSONResponse(request.app.state.image_model.snapshot(), headers={"Cache-Control": "no-store"})


@router.post("/image-models")
async def add_image_model(body: ImageModelDefinition, request: Request):
    return await model_mutation(request.app.state.image_model.save_profile(body))


@router.put("/image-models/{model_id}")
async def update_image_model(model_id: str, body: ImageModelDefinition, request: Request):
    return await model_mutation(request.app.state.image_model.save_profile(body, model_id))


@router.post("/image-models/{model_id}/activate")
async def activate_image_model(model_id: str, request: Request):
    return await model_mutation(request.app.state.image_model.activate(model_id))


@router.delete("/image-models/{model_id}")
async def delete_image_model(model_id: str, request: Request):
    return await model_mutation(request.app.state.image_model.delete(model_id))


@router.post("/chat-search")
async def configure_chat_search(body: SearchSettings, request: Request):
    try:
        result = await request.app.state.chat_search.save(body)
    except SearchError as exc:
        result = {
            "saved": False,
            "message": str(exc),
            "settings": request.app.state.chat_search.snapshot(),
        }
    except OSError:
        raise HTTPException(500, "联网搜索配置保存失败，请检查 data 目录写入权限和磁盘空间。") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/chatbot/mentions")
async def configure_mentions(body: MentionSettings, request: Request):
    try:
        await request.app.state.chatbot.configure_mentions(body.mention_names)
    except OSError:
        raise HTTPException(500, "无法保存机器人显示名，请检查本机配置文件的写入权限。") from None
    return JSONResponse({"saved": True, "chatbot": request.app.state.chatbot.snapshot()}, headers={"Cache-Control": "no-store"})


@router.post("/chat-model")
async def configure_chat_model(body: ModelSettings, request: Request):
    return await model_mutation(request.app.state.chat_model.configure(body))


async def model_mutation(operation):
    try:
        result = await operation
    except ModelError as exc:
        result = {"saved": False, "test": {"ok": False, "message": str(exc)}}
    except OSError:
        raise HTTPException(500, "模型配置保存失败，请检查 data 目录写入权限和磁盘空间。") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/chat-models")
async def add_chat_model(body: ModelDefinition, request: Request):
    return await model_mutation(request.app.state.chat_model.save_profile(body))


@router.put("/chat-models/{model_id}")
async def update_chat_model(model_id: str, body: ModelDefinition, request: Request):
    return await model_mutation(request.app.state.chat_model.save_profile(body, model_id))


@router.post("/chat-models/{model_id}/activate")
async def activate_chat_model(model_id: str, request: Request):
    return await model_mutation(request.app.state.chat_model.activate(model_id))


@router.delete("/chat-models/{model_id}")
async def delete_chat_model(model_id: str, request: Request):
    return await model_mutation(request.app.state.chat_model.delete(model_id))


@router.get("/groups/{group}/messages")
async def messages(group: GroupId, request: Request, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200), q: str = Query("", max_length=200)):
    return JSONResponse(await request.app.state.groups.messages(group, page=page, size=size, query=q), headers={"Cache-Control": "no-store"})


@router.get("/groups/{group}/word-cloud")
async def word_cloud(group: GroupId, request: Request):
    return JSONResponse(await request.app.state.groups.word_cloud(group, bot_wxid=request.app.state.chatbot.identity), headers={"Cache-Control": "no-store"})


@router.get("/groups/{group}/radar")
async def radar(group: GroupId, request: Request, days: int = Query(14, ge=1, le=30)):
    return JSONResponse(await request.app.state.groups.radar(group, bot_wxid=request.app.state.chatbot.identity, days=days), headers={"Cache-Control": "no-store"})


@router.get("/groups/{group}/summary")
async def get_summary(group: GroupId, request: Request):
    return JSONResponse({"summary": await request.app.state.chat_summary.groups.get_summary(group, local_date())}, headers={"Cache-Control": "no-store"})


@router.post("/groups/{group}/summary")
async def refresh_summary(group: GroupId, request: Request):
    try:
        result = await request.app.state.chat_summary.get(group, force=True)
    except ModelError as exc:
        raise HTTPException(409, str(exc)) from None
    return JSONResponse({"summary": result}, headers={"Cache-Control": "no-store"})


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
