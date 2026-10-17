from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints, field_validator

from wxhook_api.core.config import PROJECT_ROOT
from wxhook_api.services.chatbot import MentionChatService


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    api_key: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or any(c.isspace() for c in value)):
            raise ValueError("Base URL 须为 HTTP(S) 接口地址，不含账号、查询参数或片段")
        try:
            parts.port
        except ValueError:
            raise ValueError("Base URL 端口无效") from None
        value = value.rstrip("/")
        if value.endswith("/chat/completions"):
            value = value[:-len("/chat/completions")]
        return value

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        key = value.get_secret_value().strip()
        if len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError("API Key 格式无效")
        return SecretStr(key) if key else None


class ModelError(RuntimeError):
    """Safe, user-facing diagnostics without upstream bodies or credentials."""


class ChatCompletionsProvider:
    def __init__(self, settings: ModelSettings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def answer(self, question: str) -> str:
        headers = {}
        if self.settings.api_key:
            headers["Authorization"] = "Bearer " + self.settings.api_key.get_secret_value()
        try:
            async with asyncio.timeout(45), httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10),
                    transport=self.transport, follow_redirects=False) as client:
                response = await client.post(self.settings.base_url + "/chat/completions", headers=headers,
                    json={"model": self.settings.model, "messages": [{"role": "user", "content": question}], "stream": False})
        except (httpx.TimeoutException, TimeoutError):
            raise ModelError("模型响应超时，请检查网络或稍后重试。") from None
        except httpx.RequestError:
            raise ModelError("无法连接模型服务，请检查 Base URL、网络和代理设置。") from None
        hints = {401: "API Key 无效，请检查密钥。", 403: "没有模型访问权限，请检查账号和密钥权限。",
                 404: "接口或模型不存在，请检查 Base URL（通常包含 /v1）和模型名称。",
                 429: "请求受限或额度不足，请检查模型服务的额度和限流。"}
        if response.status_code != 200:
            raise ModelError(hints.get(response.status_code, f"模型服务返回 HTTP {response.status_code}，请检查模型名称与服务状态。"))
        try:
            answer = response.json()["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("模型未返回有效文本，请确认服务支持 Chat Completions 接口。") from None
        return answer.strip()


class ChatModelConfiguration:
    def __init__(self, chatbot: MentionChatService):
        self.chatbot = chatbot
        self.path = Path(os.getenv("WXHOOK_CHAT_MODEL_CONFIG") or PROJECT_ROOT / "data" / "chat-model.local.json")
        self.settings: ModelSettings | None = None
        self.transport: httpx.AsyncBaseTransport | None = None
        self._lock = asyncio.Lock()
        self.load_error = ""
        if self.path.is_file():
            try:
                self.settings = ModelSettings.model_validate_json(self.path.read_text(encoding="utf-8"))
                chatbot.provider = ChatCompletionsProvider(self.settings)
            except (OSError, ValueError):
                self.load_error = "无法读取本机模型配置，请重新保存。"

    def snapshot(self) -> dict:
        return {"base_url": self.settings.base_url if self.settings else "",
                "model": self.settings.model if self.settings else "",
                "has_api_key": bool(self.settings and self.settings.api_key),
                "configured": self.settings is not None, "load_error": self.load_error}

    def _save(self, settings: ModelSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = settings.model_dump(exclude={"api_key"})
        payload["api_key"] = settings.api_key.get_secret_value() if settings.api_key else None
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    async def configure(self, requested: ModelSettings) -> dict:
        async with self._lock:
            settings = requested
            if settings.api_key is None and self.settings and self.settings.api_key:
                # Never silently forward a saved key to a different endpoint.
                if settings.base_url != self.settings.base_url:
                    raise ModelError("更换 Base URL 时请重新填写 API Key。")
                settings = settings.model_copy(update={"api_key": self.settings.api_key})
            provider = ChatCompletionsProvider(settings, self.transport)
            answer = await provider.answer("你好，这是连接测试。请简短回复：模型连接正常。")
            await asyncio.to_thread(self._save, settings)
            self.settings = settings
            self.chatbot.provider = provider
            self.chatbot.last_error = ""
            self.load_error = ""
            if settings.api_key:
                answer = answer.replace(settings.api_key.get_secret_value(), "[已隐藏密钥]")
            return {"saved": True, "settings": self.snapshot(),
                    "test": {"ok": True, "message": "模型连接正常，配置已保存并立即生效。", "reply": answer[:500]}}
