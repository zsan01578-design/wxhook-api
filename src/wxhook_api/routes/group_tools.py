from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator, model_validator

from wxhook_api.routes.manage import GroupId
from wxhook_api.services.chat_model import ModelError

router = APIRouter(prefix="/api/manage/groups/{group}/tools", tags=["群工具"])
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ToolBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Preferences(ToolBody):
    auto_reports: StrictBool | None = None
    task_reminders: StrictBool | None = None


class Rule(ToolBody):
    name: Text
    keyword: Text
    match_mode: Literal["contains", "exact"] = "contains"
    action: Literal["record", "reply"] = "record"
    reply: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""
    enabled: StrictBool = False
    cooldown: int = Field(60, ge=10, le=86400)

    @model_validator(mode="after")
    def valid_reply(self):
        if self.action == "reply" and not self.reply:
            raise ValueError("自动回复规则必须填写回复内容")
        return self


class Task(ToolBody):
    title: Text
    owner: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""
    due_date: str = Field("", max_length=10)
    source_id: int | None = Field(None, ge=1)

    @field_validator("due_date")
    @classmethod
    def valid_date(cls, value):
        if value:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("日期格式必须为 YYYY-MM-DD")
        return value


class TaskStatus(ToolBody):
    status: Literal["open", "done", "cancelled"]


async def service(request, group):
    tools = request.app.state.group_tools
    if not await tools.db(lambda db: db.execute("SELECT 1 FROM groups WHERE id=?", (group,)).fetchone()):
        raise HTTPException(404, "群聊不存在，请先添加")
    return tools


@router.get("")
async def snapshot(group: GroupId, request: Request):
    return JSONResponse(await (await service(request, group)).snapshot(group), headers={"Cache-Control": "no-store"})


@router.patch("/preferences")
async def preferences(group: GroupId, body: Preferences, request: Request):
    await (await service(request, group)).preferences(group, body.auto_reports, body.task_reminders)
    return {"saved": True}


@router.post("/reports/{kind}")
async def report(group: GroupId, kind: Literal["week", "month"], request: Request):
    try:
        return {"report": await (await service(request, group)).report(group, kind, force=True)}
    except ModelError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/rules")
async def add_rule(group: GroupId, body: Rule, request: Request):
    try:
        return {"id": await (await service(request, group)).save_rule(group, body.model_dump()), "saved": True}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.put("/rules/{rule_id}")
async def update_rule(group: GroupId, rule_id: str, body: Rule, request: Request):
    tools = await service(request, group)
    if not any(rule["id"] == rule_id for rule in (await tools.snapshot(group))["rules"]):
        raise HTTPException(404, "规则不存在")
    await tools.save_rule(group, body.model_dump(), rule_id)
    return {"saved": True}


@router.delete("/rules/{rule_id}")
async def delete_rule(group: GroupId, rule_id: str, request: Request):
    await (await service(request, group)).delete_rule(group, rule_id)
    return {"saved": True}


@router.post("/tasks/extract")
async def extract_tasks(group: GroupId, request: Request):
    try:
        return {"candidates": await (await service(request, group)).extract_tasks(group)}
    except ModelError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/tasks")
async def add_task(group: GroupId, body: Task, request: Request):
    try:
        return {"id": await (await service(request, group)).save_task(group, body.model_dump()), "saved": True}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.patch("/tasks/{task_id}")
async def task_status(group: GroupId, task_id: str, body: TaskStatus, request: Request):
    if not await (await service(request, group)).task_status(group, task_id, body.status):
        raise HTTPException(404, "待办不存在")
    return {"saved": True}
