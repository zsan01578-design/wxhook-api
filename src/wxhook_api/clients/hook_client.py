from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from wxhook_api.core.errors import HookTimeoutError, HookTransportError
from wxhook_api.schemas.requests import (
    CallbackRequest,
    ContactSearchRequest,
    DecodePictureRequest,
    ExecuteQueryRequest,
    FileMessageRequest,
    HistoryRequest,
    ImageMessageRequest,
    MessagePictureRequest,
    TextMessageRequest,
    VoiceMessageRequest,
)


@dataclass(frozen=True)
class HookResponse:
    data: Any
    status_code: int


class HookClient:
    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 2.0,
        read_timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> "HookClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        try:
            response = await self._client.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            raise HookTimeoutError("Hook request timed out") from exc
        except httpx.HTTPError as exc:
            raise HookTransportError("Hook request failed") from exc
        try:
            data = response.json()
        except (UnicodeDecodeError, ValueError) as exc:
            raise HookTransportError("Hook response is not valid JSON") from exc
        return HookResponse(data=data, status_code=response.status_code)

    async def get_file_save_path(self) -> Any:
        return await self._request("GET", "/GetFileSavePath")

    async def get_decode_key(self) -> Any:
        return await self._request("POST", "/GetDecodeKey", {})

    async def get_message_pic(self, request: MessagePictureRequest) -> Any:
        return await self._request("POST", "/GetMessagePic", request.model_dump())

    async def send_voice(self, request: VoiceMessageRequest) -> Any:
        return await self._request("POST", "/SendVoiceMsg", request.model_dump())

    async def send_text(self, request: TextMessageRequest) -> Any:
        return await self._request("POST", "/SendTextMsg", request.model_dump(exclude_none=True))

    async def send_image(self, request: ImageMessageRequest) -> Any:
        return await self._request("POST", "/SendImgMsg", request.model_dump())

    async def decode_picture(self, request: DecodePictureRequest) -> Any:
        return await self._request("POST", "/Decode_Pic", request.model_dump())

    async def send_file(self, request: FileMessageRequest) -> Any:
        return await self._request("POST", "/send_file_msg", request.model_dump(mode="json"))

    async def set_callback(self, request: CallbackRequest) -> Any:
        return await self._request("POST", "/set_callback", request.model_dump())

    async def get_self_profile(self) -> Any:
        return await self._request("POST", "/GetSelfProfile", {})

    async def execute_query(self, request: ExecuteQueryRequest) -> Any:
        return await self._request("POST", "/QueryDB/execute", request.model_dump())

    async def get_all_db_names(self) -> Any:
        return await self._request("POST", "/QueryDB/GetAllDBName", {})

    async def get_status(self) -> Any:
        return await self._request("GET", "/QueryDB/status")

    async def search_contacts(self, request: ContactSearchRequest) -> Any:
        return await self._request("POST", "/Contact/search", request.model_dump())

    async def get_history(self, request: HistoryRequest) -> Any:
        return await self._request("POST", "/Message/history", request.model_dump())
