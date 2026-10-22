from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CallbackEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_type: int
    msgid: int | str
    # Message IDs are stored as text, but the message type is a SQLite INTEGER.
    type: int = Field(ge=-(2**63), le=2**63 - 1)
    timestamp: int
    wxid: str
    sender: str
    # Some Hook builds identify group callbacks through wxid alone. The routing
    # layer already supports this; missing roomid must not reject the message.
    roomid: str = Field(default="")
    content: str

    @field_validator("roomid", mode="before")
    @classmethod
    def empty_roomid(cls, value):
        return "" if value is None else value

    @property
    def extra(self) -> dict[str, Any]:
        known = set(type(self).model_fields)
        return {key: value for key, value in self.model_extra.items() if key not in known}
