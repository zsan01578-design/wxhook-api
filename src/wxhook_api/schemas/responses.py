from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RetResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    ret: int
    retmsg: str | None = None


class PBResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    wxid: str | None = None
    alias: str | None = None
    nickname: str | None = None
    email: str | None = None
    qq: int | None = None
    phone: str | None = None
    proiv: str | None = None
    area: str | None = None
    signinfo: str | None = None


class DatabaseResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    dbName: str
    dbHandle: int | None = None


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    IsLogin: int | None = None
    hWeixin: int | None = None


class FileSavePathResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    path: str | None = None


KnownResponse = Any
