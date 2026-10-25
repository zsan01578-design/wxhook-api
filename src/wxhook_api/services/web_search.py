from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Annotated, Callable
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator

from wxhook_api.core.config import PROJECT_ROOT
from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.core.access import loopback
from wxhook_api.core.bounded_http import ResponseReadError, response_bytes
from wxhook_api.core.secrets import protect_secret, reveal_secret
from wxhook_api.core.ai_safety import MAX_RESPONSE_BYTES, check_ai_permission


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
        if parts.scheme == "http" and parts.hostname != "localhost" and not loopback(parts.hostname):
            raise ValueError("远程搜索地址必须使用 HTTPS")
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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/tools/search"):
            self.base_url = self.base_url[:-len("/tools/search")]
        self.api_key = api_key
        self.result_count = min(max(result_count, 1), 50)
        self.transport = transport
        self.timeout = timeout
        self.client = client

    async def search(self, query: str) -> list[SearchResult]:
        query = " ".join((query or "").split())[:128]
        if not query:
            return []
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(asyncio.timeout(self.timeout + 5))
                if self.client is None:
                    client = await stack.enter_async_context(httpx.AsyncClient(
                        timeout=httpx.Timeout(self.timeout, connect=min(4.0, self.timeout)),
                        transport=self.transport,
                        follow_redirects=False,
                        trust_env=False,
                    ))
                else:
                    client = self.client
                async with client.stream("POST",
                    self.base_url + "/tools/search",
                    headers={"Authorization": "Bearer " + self.api_key, "Accept-Encoding": "gzip, deflate"},
                    json={"query": query, "count": self.result_count},
                    timeout=httpx.Timeout(self.timeout, connect=min(4.0, self.timeout)),
                ) as response:
                    body = bytearray()
                    if response.status_code == 200:
                        async for chunk in response_bytes(response, MAX_RESPONSE_BYTES):
                            body.extend(chunk)
        except ResponseReadError as exc:
            message = "联网搜索返回内容过大，已回退普通回答。" if str(exc) == "size" else "联网搜索响应传输不完整或压缩格式无效，已回退普通回答。"
            raise SearchError(message) from None
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
            payload = json.loads(body)
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
            snippet = _plain_text(row.get("summary") or row.get("snippet"), 1000)
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
        self._client: httpx.AsyncClient | None = None
        self._now = now or (lambda: datetime.now(timezone(timedelta(hours=8))).date())
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
            settings["api_key"] = reveal_secret(raw.get("api_key"))
            settings = SearchSettings.model_validate(settings)
            usage_date = raw.get("usage_date", "")
            used_today = raw.get("used_today", 0)
            if ("usage_date" in raw) != ("used_today" in raw):
                raise ValueError("Incomplete search usage")
            if not isinstance(usage_date, str) or type(used_today) is not int or used_today < 0:
                raise ValueError("Invalid search usage")
            if usage_date:
                if date.fromisoformat(usage_date).isoformat() != usage_date or usage_date > self._today():
                    raise ValueError("Invalid search usage date")
            elif used_today:
                raise ValueError("Search usage has no date")
            if os.name == "nt" and raw.get("api_key") and not raw["api_key"].startswith("dpapi:v1:"):
                raw["api_key"] = protect_secret(settings.api_key.get_secret_value() if settings.api_key else None)
                self._write(raw)
            self.settings = settings
            self.usage_date, self.used_today = usage_date, used_today
        except (OSError, ValueError, TypeError):
            self.settings = SearchSettings()
            self.usage_date, self.used_today = "", 0
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

    async def _save(self, settings: SearchSettings | None = None,
                    usage: tuple[str, int] | None = None) -> None:
        settings = settings or self.settings
        usage_date, used_today = usage if usage is not None else (self.usage_date, self.used_today)
        payload = {
            "enabled": settings.enabled,
            "base_url": settings.base_url,
            "api_key": protect_secret(settings.api_key.get_secret_value() if settings.api_key else None),
            "result_count": settings.result_count,
            "daily_limit": settings.daily_limit,
            "usage_date": usage_date,
            "used_today": used_today,
        }

        await asyncio.to_thread(self._write, payload)

    def _write(self, payload):
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

    async def save(self, requested: SearchSettings) -> dict:
        async with self._lock:
            settings = requested
            if settings.api_key is None and self.settings.api_key:
                if settings.base_url != self.settings.base_url:
                    raise SearchError("更换搜索 Base URL 时请重新填写 API Key。")
                settings = settings.model_copy(update={"api_key": self.settings.api_key})
            async def commit():
                await self._save(settings)
                self.settings = settings
                self.load_error = ""
                self.last_error = ""
            await finish_before_cancel(commit())
        return {"saved": True, "settings": self.snapshot()}

    def _provider(self) -> DeepSeekSearchProvider | None:
        if self.load_error or not self.settings.enabled or not self.settings.api_key:
            return None
        if self._provider_override is not None:
            return self._provider_override
        return DeepSeekSearchProvider(
            self.settings.base_url,
            self.settings.api_key.get_secret_value(),
            result_count=self.settings.result_count,
            transport=self._transport,
            client=self.http_client(),
        )

    def http_client(self) -> httpx.AsyncClient | None:
        if self._transport is not None:
            return None
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=4), follow_redirects=False, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def context(self, question: str) -> str | None:
        async with self._lock:
            provider = self._provider()
            if provider is None:
                return None
            today = self._today()
            used = self.used_today if self.usage_date == today else 0
            if used >= self.settings.daily_limit:
                return None

            async def reserve():
                usage = (today, used + 1)
                await self._save(None, usage)
                # Publish the reservation only after its durable write. A
                # storage failure must not consume quota for a skipped search.
                self.usage_date, self.used_today = usage

            try:
                await finish_before_cancel(reserve())
            except OSError:
                self.last_error = "无法保存搜索额度，已跳过搜索并回退普通回答。"
                return None
        try:
            check_ai_permission()
            results = await provider.search(question)
        except SearchError as exc:
            # A timeout or error does not prove the provider did not bill it.
            # Keep the durable reservation for every outbound attempt.
            self.last_error = str(exc)
            return None
        except Exception:
            self.last_error = "联网搜索失败，已回退普通回答。"
            return None
        self.last_error = ""
        return format_search_context(results) if results else None
