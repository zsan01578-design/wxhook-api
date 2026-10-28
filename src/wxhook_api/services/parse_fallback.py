"""Optional third-party watermark-removal fallback for share parsing.

The endpoint is fixed and the secret is read from the local parse-video
configuration or WXHOOK_DUDUNAS_SECRET. The secret is never logged or returned
by an API, and is not part of the repository.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import yaml

from wxhook_api.services.douyin_parser import DouyinError, ParsedMedia, checked_url, item_identity, read_response_bytes
from wxhook_api.services.parse_video import parser_config_path, source_platform


FALLBACK_ENDPOINT = "https://api.dudunas.top/api/qushuiyin"
FALLBACK_SECRET_ENV = "WXHOOK_DUDUNAS_SECRET"
FALLBACK_SECRET_KEY = "dudunas-secret"
FALLBACK_ATTEMPTS = 3
FALLBACK_INTERVAL = 3.0
FALLBACK_MAX_BYTES = 3 * 1024 * 1024


def _configured_secret() -> str:
    value = os.getenv(FALLBACK_SECRET_ENV)
    if value is None:
        path = parser_config_path()
        try:
            if not path.is_file():
                return ""
            if path.stat().st_size > 32 * 1024:
                raise ValueError
            data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
            value = data.get(FALLBACK_SECRET_KEY, "")
        except (OSError, ValueError, AttributeError, yaml.YAMLError) as exc:
            raise DouyinError("无法读取 parse-video.local.yml，请检查本机解析配置格式") from exc
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DouyinError("去水印兜底密钥格式无效")
    value = value.strip()
    if not value:
        return ""
    if len(value) > 256 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise DouyinError("去水印兜底密钥格式无效")
    return value


def load_dudunas_parser(*, transport: httpx.AsyncBaseTransport | None = None) -> DudunasParser | None:
    """Return a configured fallback parser, or None when disabled/invalid."""
    try:
        secret = _configured_secret()
    except DouyinError:
        return None
    return DudunasParser(secret, transport=transport) if secret else None


class DudunasParser:
    def __init__(self, secret: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 attempts: int = FALLBACK_ATTEMPTS, interval: float = FALLBACK_INTERVAL,
                 max_bytes: int = FALLBACK_MAX_BYTES):
        self.secret = secret
        self.transport = transport
        self.attempts = max(1, int(attempts))
        self.interval = max(0.0, float(interval))
        self.max_bytes = max(1024, int(max_bytes))

    async def parse(self, source: str) -> ParsedMedia:
        source = checked_url(source, multi_platform=True)
        last_error: DouyinError | None = None
        for attempt in range(self.attempts):
            try:
                raw = await self._request(source)
                return self._parse_payload(raw, source)
            except DouyinError as exc:
                last_error = exc
            except (httpx.HTTPError, ValueError, RecursionError, UnicodeError):
                last_error = DouyinError("去水印兜底接口连接失败或返回无效数据")
            if attempt + 1 < self.attempts:
                await asyncio.sleep(self.interval)
        raise last_error or DouyinError("去水印兜底接口没有返回结果")

    async def _request(self, source: str) -> bytes:
        async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(20, connect=8)) as client:
            async with client.stream(
                "GET", FALLBACK_ENDPOINT,
                params={"AppSecret": self.secret, "text": source},
                headers={"Accept": "application/json", "Referer": "https://api.dudunas.top/"},
            ) as response:
                if response.status_code != 200:
                    raise DouyinError(f"去水印兜底接口返回 HTTP {response.status_code}")
                raw = bytearray()
                async for chunk in read_response_bytes(
                    response, self.max_bytes, size_error="去水印兜底接口响应过大，已停止处理"):
                    raw.extend(chunk)
        return bytes(raw)

    def _parse_payload(self, raw: bytes, source: str) -> ParsedMedia:
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise DouyinError("去水印兜底接口没有返回有效 JSON") from exc
        if not isinstance(payload, dict):
            raise DouyinError("去水印兜底接口响应格式无效")
        if str(payload.get("code")) != "200":
            raise DouyinError(f"去水印兜底接口未成功（code={str(payload.get('code'))[:20]}）")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DouyinError("去水印兜底接口成功响应缺少 data 对象")

        pictures = data.get("images")
        if pictures is None:
            pictures = data.get("pics")
        if pictures is not None and not isinstance(pictures, list):
            raise DouyinError("去水印兜底接口返回的图片列表格式无效")
        assets = []
        for picture in pictures or []:
            urls = [picture] if isinstance(picture, str) else (
                [picture.get(key) for key in ("url", "src", "image_url", "down")] if isinstance(picture, dict) else [])
            value = next((url for url in urls if isinstance(url, str) and url), "")
            if not value:
                raise DouyinError("去水印兜底接口返回了无效图片地址")
            assets.append({"kind": "image", "urls": [checked_url(value, media=True)]})
            if len(assets) > 100:
                raise DouyinError("去水印兜底返回的图集超过 100 张，已停止处理")

        title = data.get("title") or data.get("desc") or ""
        author = data.get("author") or ""
        if isinstance(author, dict):
            author = author.get("nickname") or author.get("name") or ""
        platform = source_platform(source)
        item_id = ""
        if platform == "douyin":
            try:
                identity = item_identity(source)
                item_id = identity[1] if identity else ""
            except DouyinError:
                item_id = ""
        if assets:
            return ParsedMedia(str(item_id)[:100], str(title)[:2000], str(author)[:200], "images", assets, platform=platform)

        video = data.get("video_url") or data.get("url") or data.get("down")
        if not isinstance(video, str) or not video:
            raise DouyinError("去水印兜底接口没有返回视频或图片地址")
        return ParsedMedia(str(item_id)[:100], str(title)[:2000], str(author)[:200], "video",
                           [{"kind": "video", "urls": [checked_url(video, media=True)]}], platform=platform)
