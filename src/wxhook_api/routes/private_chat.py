from typing import Annotated
import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from wxhook_api.services.private_chat import MAX_PERSONALITY_CHARS

router = APIRouter(prefix="/api/private", tags=["私聊问答"])
ContactId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.@-]+$")]


class PrivateConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    personality: Annotated[str, StringConstraints(max_length=MAX_PERSONALITY_CHARS)] | None = None


class PrivateContactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] | None = None

    @field_validator("name")
    @classmethod
    def no_control_characters(cls, value):
        if value is not None and any(ord(char) < 32 for char in value):
            raise ValueError("备注不能包含控制字符")
        return value


@router.get("/state")
async def state(request: Request):
    return JSONResponse(request.app.state.private_chat.snapshot(), headers={"Cache-Control": "no-store"})


@router.post("/config")
async def set_config(body: PrivateConfigPatch, request: Request):
    try:
        state = await request.app.state.private_chat.configure(**body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(500, "无法保存私聊配置，请检查管理数据库") from exc
    return {"saved": True, "state": state}


@router.patch("/contacts/{contact}")
async def update_contact(contact: ContactId, body: PrivateContactPatch, request: Request):
    if contact.endswith("@chatroom"):
        raise HTTPException(400, "请填写私聊联系人 wxid，不能填写群 ID")
    try:
        saved = await request.app.state.private_chat.update_contact(contact, body.model_dump(exclude_unset=True))
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(500, "无法保存联系人配置，请检查管理数据库") from exc
    if not saved:
        raise HTTPException(404, "联系人不存在，请先让对方发一条私聊消息")
    return {"saved": True, "state": request.app.state.private_chat.snapshot()}
