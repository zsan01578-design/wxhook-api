from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CallbackEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_type: int
    msgid: int | str
    type: int
    timestamp: int
    wxid: str
    sender: str
    roomid: str
    content: str

    @property
    def extra(self) -> dict[str, Any]:
        known = set(type(self).model_fields)
        return {key: value for key, value in self.model_extra.items() if key not in known}
