"""Independent Images API profiles and bounded image generation."""
from __future__ import annotations

import asyncio
import base64
import binascii
from io import BytesIO
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Annotated, Literal
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
import warnings

import httpx
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator, model_validator

from wxhook_api.core.ai_safety import BudgetError, check_ai_permission
from wxhook_api.core.bounded_http import ResponseReadError, response_bytes
from wxhook_api.core.config import PROJECT_ROOT
from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.core.secrets import reveal_secret
from wxhook_api.services.chat_model import (ChatModelConfiguration, ModelError, ModelSettings,
    UpstreamModelError, upstream_failure, validate_fallbacks, run_model_chain)
from wxhook_api.services.media_cleanup import safe_child
from wxhook_api.services.storage_guard import StorageGuard


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_JSON_BYTES = 28 * 1024 * 1024
TEST_PROMPT = "A simple blue circle on a plain white background."


class ImageModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""
    base_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    api_key: SecretStr | None = None
    size: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] = "1024x1024"
    response_format: Literal["default", "url", "b64_json"] = "default"

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        value = ModelSettings.valid_url(value)
        return value.removesuffix("/images/generations")

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        return ModelSettings.valid_key(value)

    @field_validator("size")
    @classmethod
    def valid_size(cls, value):
        if value in {"", "auto"}:
            return value
        if not re.fullmatch(r"[1-9][0-9]{1,3}x[1-9][0-9]{1,3}", value):
            raise ValueError("尺寸须为宽x高、auto 或留空")
        if any(int(side) > 4096 for side in value.split("x")):
            raise ValueError("图片边长不能超过 4096")
        return value


class ImageModelProfile(ImageModelDefinition):
    id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


class ImageModelCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    active_id: str
    models: list[ImageModelProfile]
    fallback_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_active(self):
        ids = [profile.id for profile in self.models]
        if self.version != 1 or len(ids) != len(set(ids)) or self.active_id not in ids:
            raise ValueError("Invalid image model collection")
        validate_fallbacks(self.active_id, ids, self.fallback_ids)
        return self


def image_png(content: bytes, *, preview=False) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.width * image.height > 40_000_000 or getattr(image, "n_frames", 1) != 1:
                    raise ValueError
                image.load()
                converted = ImageOps.exif_transpose(image).convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
                if preview:
                    converted.thumbnail((512, 512))
                output = BytesIO()
                converted.save(output, format="PNG")
                if output.tell() > MAX_IMAGE_BYTES:
                    raise ValueError
                return output.getvalue()
    except Exception:
        raise ModelError("生图服务未返回可读取的静态图片，或图片尺寸/文件过大。") from None


class ImagesProvider:
    def __init__(self, settings, *, transport=None, budget, account_check):
        self.settings, self.transport = settings, transport
        self.budget, self.account_check = budget, account_check

    async def _download_url(self, value: str) -> str:
        try:
            parts, origin = urlsplit(value), urlsplit(self.settings.base_url)
            if (len(value) > 8192 or any(c.isspace() or ord(c) < 32 for c in value)
                    or not parts.hostname or parts.username or parts.password or parts.fragment):
                raise ValueError
            port = parts.port
            # Explicitly configured local services may return their own local images.
            local = origin.hostname == "localhost"
            try:
                local |= ipaddress.ip_address(origin.hostname).is_loopback
            except ValueError:
                pass
            if local and (parts.scheme, parts.hostname, port) == (origin.scheme, origin.hostname, origin.port):
                return value
            if parts.scheme != "https" or port not in {None, 443}:
                raise ValueError
            try:
                if not ipaddress.ip_address(parts.hostname).is_global:
                    raise ModelError("生图返回的图片地址不可访问或不安全，请改用 Base64 返回格式。")
            except ValueError:
                pass
            addresses = await asyncio.get_running_loop().getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                raise ValueError
            return value
        except (ValueError, OSError):
            raise ModelError("生图返回的图片地址不可访问或不安全，请改用 Base64 返回格式。") from None

    @staticmethod
    async def _body(response, limit):
        body = bytearray()
        async for chunk in response_bytes(response, limit):
            check_ai_permission()
            body.extend(chunk)
        return bytes(body)

    async def generate(self, prompt: str) -> bytes:
        if not prompt.strip() or len(prompt) > 8000:
            raise ModelError("请填写 1–8000 字的生图描述。")
        payload = {"model": self.settings.model, "prompt": prompt.strip(), "n": 1}
        if self.settings.size:
            payload["size"] = self.settings.size
        if self.settings.response_format != "default":
            payload["response_format"] = self.settings.response_format
        headers = {"Accept-Encoding": "identity"}
        if self.settings.api_key:
            headers["Authorization"] = "Bearer " + self.settings.api_key.get_secret_value()
        generation_accepted = False
        try:
            async with asyncio.timeout(180), self.budget.slots, httpx.AsyncClient(
                    timeout=httpx.Timeout(150, connect=10), transport=self.transport, follow_redirects=False) as client:
                check_ai_permission()
                await self.account_check()
                check_ai_permission()
                await self.budget.reserve()
                await self.account_check()
                check_ai_permission()
                async with client.stream("POST", self.settings.base_url + "/images/generations",
                                         headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        hints = {400: "生图参数或描述被拒绝，请检查模型、尺寸及返回格式。", 401: "生图 API Key 无效。",
                                 402: "生图账号余额不足，请充值或更换配置。",
                                 403: "没有生图模型访问权限。", 404: "生图接口或模型不存在，请检查 Base URL 和模型名称。",
                                 429: "生图请求受限或额度不足，请稍后重试。"}
                        await upstream_failure(response, hints.get(response.status_code, f"生图服务返回 HTTP {response.status_code}，请稍后重试。"))
                    generation_accepted = True
                    body = await self._body(response, MAX_JSON_BYTES)
                try:
                    item = json.loads(body)["data"][0]
                    encoded, url = item.get("b64_json"), item.get("url")
                    if isinstance(encoded, str) and encoded:
                        content = base64.b64decode(encoded, validate=True)
                        if len(content) > MAX_IMAGE_BYTES:
                            raise ValueError
                    elif isinstance(url, str) and url:
                        # Never send the API credential or cookies to an image URL.
                        async with httpx.AsyncClient(timeout=30, transport=self.transport, follow_redirects=False) as download:
                            for _ in range(4):
                                url = await self._download_url(url)
                                check_ai_permission()
                                async with download.stream("GET", url, headers={"Accept-Encoding": "identity"}) as image:
                                    if image.status_code in {301, 302, 303, 307, 308} and image.headers.get("location"):
                                        url = urljoin(url, image.headers["location"])
                                        continue
                                    if image.status_code != 200:
                                        raise ModelError("无法下载生成的图片，请稍后重试或改用 Base64 返回格式。")
                                    content = await self._body(image, MAX_IMAGE_BYTES)
                                    break
                            else:
                                raise ModelError("生成的图片重定向次数过多。")
                    else:
                        raise ValueError
                except (ValueError, KeyError, IndexError, TypeError, AttributeError, binascii.Error, RecursionError):
                    raise ModelError("生图响应格式无效，需要 data[0].url 或 data[0].b64_json。") from None
                check_ai_permission()
                return await asyncio.to_thread(image_png, content)
        except BudgetError as exc:
            raise ModelError(str(exc)) from None
        except ResponseReadError:
            raise ModelError("生图响应过大或传输不完整，已停止读取。") from None
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if generation_accepted:
                raise ModelError("图片已经生成，但下载连接失败，请检查图片服务或改用 Base64 返回格式。") from None
            raise UpstreamModelError("无法连接生图服务，请检查地址、网络和代理设置。") from None
        except (TimeoutError, httpx.TimeoutException):
            raise ModelError("生图超时，请稍后重试；本次请求可能已计费。") from None
        except httpx.RequestError:
            raise ModelError("无法连接生图服务，请检查地址、网络和代理设置。") from None


class ImageModelConfiguration:
    # Share atomic writes, secret protection, snapshots and account verification.
    _save = ChatModelConfiguration._save
    _profile = ChatModelConfiguration._profile
    _verify_group_account = ChatModelConfiguration._verify_group_account
    profile_snapshot = ChatModelConfiguration.profile_snapshot
    set_fallbacks = ChatModelConfiguration.set_fallbacks
    record_route = ChatModelConfiguration.record_route

    def __init__(self, chatbot, *, budget):
        self.chatbot, self.budget = chatbot, budget
        self.path = Path(os.getenv("WXHOOK_IMAGE_MODEL_CONFIG") or PROJECT_ROOT / "data" / "image-model.local.json")
        self.output_dir = Path(os.getenv("WXHOOK_IMAGE_OUTPUT_DIR") or PROJECT_ROOT / "data" / "generated-images").resolve()
        self.storage = StorageGuard(self.output_dir)
        self.models, self.active_id, self.load_error = {}, "", ""
        self.fallback_ids, self.last_route = [], {}
        self.transport = None
        self._lock = asyncio.Lock()
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                migrated = False
                for row in raw["models"]:
                    migrated |= bool(os.name == "nt" and row.get("api_key") and not str(row["api_key"]).startswith("dpapi:v1:"))
                    row["api_key"] = reveal_secret(row.get("api_key"))
                collection = ImageModelCollection.model_validate(raw)
                if migrated:
                    self._save(collection)
                self.models = {profile.id: profile for profile in collection.models}
                self.active_id = collection.active_id
                self.fallback_ids = collection.fallback_ids
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.load_error = "无法读取本机生图配置，请重新保存。"

    @property
    def settings(self):
        return self.models.get(self.active_id)

    def snapshot(self):
        return {"configured": self.settings is not None, "active_id": self.active_id,
                "fallback_ids": self.fallback_ids.copy(), "last_route": self.last_route,
                "models": [self.profile_snapshot(p) for p in self.models.values()], "load_error": self.load_error}

    def provider(self, profile):
        return ImagesProvider(profile, transport=self.transport, budget=self.budget, account_check=self._verify_group_account)

    async def _commit(self, models, active_id, *, fallback_ids=None):
        ordered = [key for key in (self.fallback_ids if fallback_ids is None else fallback_ids)
                   if key in models and key != active_id]
        async def commit():
            await asyncio.to_thread(self._save, ImageModelCollection(active_id=active_id, models=list(models.values()), fallback_ids=ordered))
            self.models, self.active_id, self.load_error = models, active_id, ""
            self.fallback_ids, self.last_route = ordered, {}
        await finish_before_cancel(commit())

    async def save_profile(self, requested, model_id=None):
        async with self._lock:
            previous = self._profile(model_id) if model_id else None
            if requested.api_key is None and previous and previous.api_key:
                if requested.base_url != previous.base_url:
                    raise ModelError("更换 Base URL 时请重新填写 API Key。")
                requested = requested.model_copy(update={"api_key": previous.api_key})
            profile = ImageModelProfile(**requested.model_dump(), id=model_id or uuid4().hex)
            profile.name = profile.name or profile.model[:80]
            content = await self.provider(profile).generate(TEST_PROMPT)
            preview = await asyncio.to_thread(image_png, content, preview=True)
            await self._commit({**self.models, profile.id: profile}, self.active_id or profile.id)
            return {"saved": True, "profile": self.profile_snapshot(profile), "settings": self.snapshot(),
                    "test": {"ok": True, "message": "生图测试通过，配置已保存。" + ("当前模型已设为首选。" if profile.id == self.active_id else "可在列表设为首选或加入备用。"),
                             "preview": "data:image/png;base64," + base64.b64encode(preview).decode("ascii")}}

    async def activate(self, model_id):
        # Saved profiles already passed a real image test; switching need not bill another image.
        async with self._lock:
            self._profile(model_id)
            await self._commit(self.models.copy(), model_id)
            return {"saved": True, "settings": self.snapshot()}

    async def delete(self, model_id):
        async with self._lock:
            self._profile(model_id)
            if model_id == self.active_id:
                raise ModelError("不能删除正在启用的生图模型，请先启用其他生图模型。")
            await self._commit({key: value for key, value in self.models.items() if key != model_id}, self.active_id)
            return {"saved": True, "settings": self.snapshot()}

    def _store(self, content):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Only our UUID-named regular files are eligible; retain for Hook's delayed file reads.
        for path in self.output_dir.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}\.png", path.name):
                try:
                    if safe_child(path, self.output_dir) and path.stat().st_mtime < time.time() - 86400:
                        path.unlink()
                except OSError:
                    pass
        path = self.output_dir / (uuid4().hex + ".png")
        created = False
        try:
            with path.open("xb") as file:
                created = True
                file.write(content)
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise
        return path

    async def generate(self, prompt):
        if self.settings is None:
            raise ModelError("生图模型尚未配置，请在管理页的 AI 模型中添加生图模型。")
        try:
            await self.storage.ensure(MAX_IMAGE_BYTES)
            profiles = [self.models[key] for key in [self.active_id, *self.fallback_ids]]
            content = await run_model_chain(profiles, lambda profile: self.provider(profile).generate(prompt),
                                           self.record_route, timeout=240)
            check_ai_permission()
            await self.storage.ensure(len(content))
            path = await finish_before_cancel(asyncio.to_thread(self._store, content))
            self.storage.written(len(content))
            return path
        except OSError:
            raise ModelError("无法保存生成的图片，请检查磁盘空间和生图目录写入权限。") from None
