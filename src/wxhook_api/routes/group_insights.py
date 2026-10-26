import asyncio
import base64
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from wxhook_api.core.ai_safety import BudgetError
from wxhook_api.routes.manage import GroupId
from wxhook_api.services.chat_model import ModelError
from wxhook_api.services.group_insights import InsightRequest, window_bounds
from wxhook_api.services.image_model import MAX_IMAGE_BYTES


router = APIRouter(prefix="/api/manage/groups/{group}/insights", tags=["群聊趣味工具"])
MessageId = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


class InsightBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""
    window: Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)] = "今天"
    limit: int = Field(50, strict=True, ge=1, le=200)
    after_id: MessageId | None = None
    message_ids: list[MessageId] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def valid_window(self):
        try:
            window_bounds(self.window)
        except ModelError as exc:
            raise ValueError(str(exc)) from None
        if self.message_ids and self.after_id is not None:
            raise ValueError("指定消息与起始消息不能同时使用")
        return self


def image_data(path):
    with path.open("rb") as file:
        content = file.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise ModelError("漫画图片过大，无法预览。")
    return "data:image/png;base64," + base64.b64encode(content).decode("ascii")


@router.post("/{kind}")
async def generate(group: GroupId, kind: Literal["meme", "catchup", "comic"], body: InsightBody, request: Request):
    worker = request.app.state.group_insights
    try:
        result = await worker.generate(group, InsightRequest(kind, body.term, body.window, body.limit, body.after_id),
                                       message_ids=body.message_ids)
        if "image" in result:
            result["image_data_url"] = await asyncio.to_thread(image_data, result.pop("image"))
        contributors = result.pop("_contributors", [])
        worker.check(group, [{"group_id": group, "sender": sender} for sender in contributors])
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (ModelError, BudgetError) as exc:
        raise HTTPException(409, str(exc)) from None
