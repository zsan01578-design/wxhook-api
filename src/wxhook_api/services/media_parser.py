"""Adapter for the local ucmao/media-parser HTTP service."""

from __future__ import annotations

import asyncio
import json

import httpx

from wxhook_api.services.douyin_parser import DouyinError, ParsedMedia, checked_url, read_response_bytes
from wxhook_api.services.parse_video import source_platform


DEFAULT_MAX_BYTES = 2 * 1024 * 1024
MAX_IMAGES = 100
TOTAL_TIMEOUT = 35
COOKIE_MESSAGE = "media-parser 提示小红书需要登录 Cookie，请检查 parse-video.local.yml 的 xiaohongshu-cookie"


class MediaParserClient:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self.address = base_url.rstrip("/")
        self.endpoint = self.address + "/api/v1/parse"
        self.transport = transport
        self.max_bytes = max(1024, int(max_bytes))

    async def parse(self, source: str) -> ParsedMedia:
        source = checked_url(source, multi_platform=True)
        status, raw = await self._request(source)
        return _parse_payload(raw, source, status)

    async def _request(self, source: str) -> tuple[int, bytes]:
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT):
                async with httpx.AsyncClient(
                    transport=self.transport, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(30, connect=2),
                ) as client:
                    async with client.stream(
                        "POST", self.endpoint, json={"url": source},
                        headers={"Accept": "application/json"},
                    ) as response:
                        status = response.status_code
                        raw = bytearray()
                        async for chunk in read_response_bytes(
                            response, self.max_bytes,
                            size_error="media-parser 响应过大，已停止处理", kind="响应",
                        ):
                            raw.extend(chunk)
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            raise DouyinError(f"media-parser 本地服务连接失败，请确认 {self.address} 已启动") from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise DouyinError("media-parser 解析超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise DouyinError(f"media-parser 本地服务连接失败，请确认 {self.address} 已启动") from exc
        return status, bytes(raw)


def _parse_payload(raw: bytes, source: str, status: int) -> ParsedMedia:
    ok_status = 200 <= status < 300
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError, UnicodeError):
        payload = None
    if not isinstance(payload, dict):
        if not ok_status:
            raise DouyinError(f"media-parser 返回 HTTP {status}")
        raise DouyinError("media-parser 返回的不是有效 JSON")

    retcode = payload.get("retcode")
    if retcode is None and not ok_status:
        raise DouyinError(f"media-parser 返回 HTTP {status}")
    if retcode != 200 or payload.get("succ") is not True:
        code = str(payload.get("error_code") or "")[:50]
        if code == "XIAOHONGSHU_COOKIE_REQUIRED":
            raise DouyinError(COOKIE_MESSAGE)
        desc = str(payload.get("retdesc") or "解析失败")[:200]
        raise DouyinError(f"media-parser 解析失败：{desc}" + (f"（{code}）" if code else ""))
    if not ok_status:
        raise DouyinError(f"media-parser 返回 HTTP {status}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise DouyinError("media-parser 成功响应缺少 data 对象")

    images = data.get("image_list") or []
    if not isinstance(images, list):
        raise DouyinError("media-parser 返回的图片列表格式无效")
    if len(images) > MAX_IMAGES:
        raise DouyinError("media-parser 返回的图集超过 100 张，已停止处理")

    assets = []
    for item in images:
        value = item if isinstance(item, str) else item.get("url") if isinstance(item, dict) else None
        if not isinstance(value, str) or not value:
            raise DouyinError("media-parser 返回了无效图片地址")
        assets.append({"kind": "image", "urls": [checked_url(value, media=True)]})
        live_photo = item.get("live_photo_url") if isinstance(item, dict) else None
        if live_photo is not None and live_photo != "":
            if not isinstance(live_photo, str):
                raise DouyinError("media-parser 返回了无效实况图视频地址")
            assets.append({"kind": "video", "urls": [checked_url(live_photo, media=True)]})

    if assets:
        kind = "images"
    else:
        video = data.get("video_url")
        if not isinstance(video, str) or not video:
            raise DouyinError("media-parser 没有返回视频或图片地址")
        assets = [{"kind": "video", "urls": [checked_url(video, media=True)]}]
        kind = "video"

    raw_title = data.get("title") or data.get("desc") or ""
    title = (raw_title if isinstance(raw_title, str) else "")[:2000] or "未命名作品"
    author = data.get("author")
    if isinstance(author, dict):
        author = author.get("nickname")
    author = (author if isinstance(author, str) else "")[:200]
    item_id = str(data.get("video_id") or "")[:100]
    return ParsedMedia(item_id, title, author, kind, assets, platform=source_platform(source))
