from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
import html
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Annotated, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator

from wxhook_api.core.config import PROJECT_ROOT


DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_DAILY_LIMIT = 1000
MAX_CONTEXT_CHARS = 6000


class SearchError(RuntimeError):
    """A safe search diagnostic without the query or credentials."""


class SearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    base_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)] = DEFAULT_BASE_URL
    api_key: SecretStr | None = None
    result_count: int = Field(default=5, ge=1, le=10)
    daily_limit: int = Field(default=DEFAULT_DAILY_LIMIT, ge=1, le=100_000)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or any(c.isspace() for c in value)):
            raise ValueError("搜索 Base URL 须为 HTTP(S) 地址，不含账号、查询参数或片段")
        try:
            parts.port
        except ValueError:
            raise ValueError("搜索 Base URL 端口无效") from None
        value = value.rstrip("/")
        if value.endswith("/tools/search"):
            value = value[:-len("/tools/search")]
        return value

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        key = value.get_secret_value().strip()
        if len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError("搜索 API Key 格式无效")
        return SecretStr(key) if key else None


@dataclass(frozen=True)
class SearchResult:
    title: str
    snippet: str
    url: str = ""


def _plain_text(value: object, limit: int = 1000) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()[:limit]


def format_search_context(results: list[SearchResult]) -> str:
    parts = []
    for index, result in enumerate(results, 1):
        title = _plain_text(result.title, 200)
        snippet = _plain_text(result.snippet, 1000)
        if not title and not snippet:
            continue
        parts.append(f"{index}. {title or '未命名结果'}\n{snippet}")
        if sum(len(part) for part in parts) >= MAX_CONTEXT_CHARS:
            break
    return "\n\n".join(parts)[:MAX_CONTEXT_CHARS]


class DeepSeekSearchProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        result_count: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/tools/search"):
            self.base_url = self.base_url[:-len("/tools/search")]
        self.api_key = api_key
        self.result_count = min(max(result_count, 1), 50)
        self.transport = transport
        self.timeout = timeout

    async def search(self, query: str) -> list[SearchResult]:
        query = " ".join((query or "").split())[:128]
        if not query:
            return []
        try:
            async with asyncio.timeout(self.timeout + 5), httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(4.0, self.timeout)),
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self.base_url + "/tools/search",
                    headers={"Authorization": "Bearer " + self.api_key},
                    json={"query": query, "count": self.result_count},
                )
        except (httpx.TimeoutException, TimeoutError):
            raise SearchError("联网搜索超时，已回退普通回答。") from None
        except httpx.RequestError:
            raise SearchError("无法连接联网搜索服务，已回退普通回答。") from None
        if response.status_code != 200:
            hints = {
                401: "联网搜索 API Key 无效。",
                403: "没有联网搜索权限或额度不足。",
                429: "联网搜索请求受限或额度不足。",
            }
            raise SearchError(hints.get(response.status_code, f"联网搜索服务返回 HTTP {response.status_code}。"))
        try:
            payload = response.json()
            rows = payload.get("results") if isinstance(payload, dict) else None
        except ValueError:
            raise SearchError("联网搜索返回了无法解析的数据。") from None
        if not isinstance(rows, list):
            return []
        results = []
        for row in rows[: self.result_count]:
            if not isinstance(row, dict):
                continue
            title = _plain_text(row.get("title"), 200)
            snippet = _plain_text(row.get("snippet"), 1000)
            if title or snippet:
                results.append(SearchResult(title=title, snippet=snippet, url=_plain_text(row.get("url"), 2048)))
        return results


class WebSearchConfiguration:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        chatbot=None,
        provider: DeepSeekSearchProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], date] | None = None,
    ) -> None:
        self.path = Path(path or os.getenv("WXHOOK_SEARCH_CONFIG") or PROJECT_ROOT / "data" / "search.local.json")
        self.chatbot = chatbot
        self.settings = SearchSettings()
        self.usage_date = ""
        self.used_today = 0
        self.load_error = ""
        self.last_error = ""
        self._provider_override = provider
        self._transport = transport
        self._now = now or (lambda: datetime.now().astimezone().date())
        self._lock = asyncio.Lock()
        self._load()
        if chatbot is not None:
            chatbot.search_service = self

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("search config must be an object")
            settings = {key: raw.get(key) for key in ("enabled", "base_url", "api_key", "result_count", "daily_limit")}
            if "api_key" not in raw:
                settings["api_key"] = None
            self.settings = SearchSettings.model_validate(settings)
            self.usage_date = str(raw.get("usage_date") or "")
            self.used_today = max(0, int(raw.get("used_today") or 0))
        except (OSError, ValueError, TypeError):
            self.load_error = "无法读取本机联网搜索配置，请重新保存。"

    def snapshot(self) -> dict:
        today = self._today()
        used = 0 if self.usage_date != today else self.used_today
        limit = self.settings.daily_limit
        return {
            "enabled": self.settings.enabled,
            "base_url": self.settings.base_url,
            "has_api_key": bool(self.settings.api_key),
            "configured": bool(self.settings.api_key),
            "result_count": self.settings.result_count,
            "daily_limit": limit,
            "used_today": used,
            "remaining_today": max(0, limit - used),
            "usage_date": today,
            "load_error": self.load_error,
            "last_error": self.last_error,
        }

    def _today(self) -> str:
        return self._now().isoformat()

    async def _save(self) -> None:
        payload = {
            "enabled": self.settings.enabled,
            "base_url": self.settings.base_url,
            "api_key": self.settings.api_key.get_secret_value() if self.settings.api_key else None,
            "result_count": self.settings.result_count,
            "daily_limit": self.settings.daily_limit,
            "usage_date": self.usage_date,
            "used_today": self.used_today,
        }

        def write():
            self.path.parent.mkdir(parents=True, exist_ok=True)
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

        await asyncio.to_thread(write)

    async def save(self, requested: SearchSettings) -> dict:
        async with self._lock:
            settings = requested
            if settings.api_key is None and self.settings.api_key:
                if settings.base_url != self.settings.base_url:
                    raise SearchError("更换搜索 Base URL 时请重新填写 API Key。")
                settings = settings.model_copy(update={"api_key": self.settings.api_key})
            self.settings = settings
            self.load_error = ""
            await self._save()
        self.last_error = ""
        return {"saved": True, "settings": self.snapshot()}

    def _provider(self) -> DeepSeekSearchProvider | None:
        if not self.settings.enabled or not self.settings.api_key:
            return None
        if self._provider_override is not None:
            return self._provider_override
        return DeepSeekSearchProvider(
            self.settings.base_url,
            self.settings.api_key.get_secret_value(),
            result_count=self.settings.result_count,
            transport=self._transport,
        )

    async def context(self, question: str) -> str | None:
        provider = self._provider()
        if provider is None:
            return None
        reserved_date = ""
        async with self._lock:
            today = self._today()
            if self.usage_date != today:
                self.usage_date, self.used_today = today, 0
            if self.used_today >= self.settings.daily_limit:
                return None
            self.used_today += 1
            reserved_date = today
            await self._save()
        try:
            results = await provider.search(question)
        except SearchError as exc:
            async with self._lock:
                if self.usage_date == reserved_date and self.used_today > 0:
                    self.used_today -= 1
                    await self._save()
            self.last_error = str(exc)
            return None
        except Exception:
            async with self._lock:
                if self.usage_date == reserved_date and self.used_today > 0:
                    self.used_today -= 1
                    await self._save()
            self.last_error = "联网搜索失败，已回退普通回答。"
            return None
        self.last_error = ""
        return format_search_context(results) if results else None
