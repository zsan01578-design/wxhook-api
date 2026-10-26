from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


class TextMessageRequest(BaseModel):
    wxidorgid: str
    msg: str
    atlist: str | list[str] | None = None


class VoiceMessageRequest(BaseModel):
    wxid: str
    path: str


class ImageMessageRequest(BaseModel):
    wxidorgid: str
    path: str


class DecodePictureRequest(BaseModel):
    src_path: str
    dst_path: str


class MessagePictureRequest(BaseModel):
    msgid: str


class FileStage(str, Enum):
    construct = "construct"
    fill = "fill"
    scene = "scene"
    send = "send"


class FileMessageRequest(BaseModel):
    wxid: str
    filepath: str
    stage: FileStage = FileStage.send


class CallbackRequest(BaseModel):
    url: str


class ParseLinkRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        return value


class ExecuteQueryRequest(BaseModel):
    optDbName: str
    SQL: str


class ContactSearchRequest(BaseModel):
    keywords: Annotated[list[str], Field(min_length=1)]


class HistoryRequest(BaseModel):
    wxid: str
    size: int = 500
    page: int = 1

    @field_validator("size", mode="before")
    @classmethod
    def normalize_size(cls, value: object) -> int:
        try:
            size = int(value)
        except (TypeError, ValueError, OverflowError):
            return 500
        if size < 1:
            return 500
        return min(size, 500)

    @field_validator("page", mode="before")
    @classmethod
    def validate_page(cls, value: object) -> int:
        try:
            page = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("page must be a positive integer") from exc
        if page < 1:
            raise ValueError("page must be a positive integer")
        return page
