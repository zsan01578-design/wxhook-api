"""Pinned parse-video-py integration; the library runs in our existing worker."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import importlib
import os
from pathlib import Path
import sys
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

from wxhook_api.core.bounded_http import LimitedStream, ResponseReadError
from wxhook_api.core.config import PROJECT_ROOT
from wxhook_api.services.douyin_parser import DouyinError, DouyinParser, ParsedMedia, USER_AGENT, checked_url, stream_error


PLATFORMS = {
    "douyin": ("抖音", {"douyin.com", "www.douyin.com", "v.douyin.com", "m.douyin.com", "iesdouyin.com", "www.iesdouyin.com", "jingxuan.douyin.com"}),
    "xiaohongshu": ("小红书", {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "xhslink.cn"}),
    "bilibili": ("B站", {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}),
}
REFERERS = {"douyin": "https://www.douyin.com/", "xiaohongshu": "https://www.xiaohongshu.com/", "bilibili": "https://www.bilibili.com/"}
_MAX_PAGE_BYTES = 3 * 1024 * 1024


def source_platform(url: str) -> str:
    host = urlsplit(checked_url(url, multi_platform=True)).hostname
    for platform, (_, hosts) in PLATFORMS.items():
        if host in hosts:
            return platform
    raise DouyinError("目前支持抖音、小红书和B站分享链接")


def _normalize_source(url: str) -> str:
    url = checked_url(url, multi_platform=True)
    parsed = urlsplit(url)
    if parsed.hostname in {"m.douyin.com", "jingxuan.douyin.com"}:
        return urlunsplit((parsed.scheme, "www.douyin.com", parsed.path, parsed.query, ""))
    if parsed.hostname in {"douyin.com", "iesdouyin.com", "xiaohongshu.com", "bilibili.com"}:
        return urlunsplit((parsed.scheme, "www." + parsed.netloc, parsed.path, parsed.query, ""))
    return url


@dataclass(frozen=True)
class _RequestContext:
    transport: httpx.AsyncBaseTransport | None = None
    xhs_cookie: str = field(default="", repr=False)


def parser_config_path() -> Path:
    return Path(os.getenv("WXHOOK_PARSE_VIDEO_CONFIG") or PROJECT_ROOT / "parse-video.local.yml").expanduser().resolve()


def _local_xhs_cookie() -> str:
    value = os.getenv("PARSE_VIDEO_XHS_COOKIE")
    if value is None:
        path = parser_config_path()
        try:
            if not path.is_file():
                return ""
            if path.stat().st_size > 32 * 1024:
                raise ValueError
            data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
            value = data.get("xiaohongshu-cookie", "")
        except (OSError, ValueError, AttributeError, yaml.YAMLError) as exc:
            raise DouyinError("无法读取 parse-video.local.yml，请检查本机解析配置格式") from exc
    if not isinstance(value, str) or len(value) > 16384 or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise DouyinError("小红书 Cookie 格式无效，请在本机配置单行 Cookie 请求头")
    return value.strip()


_request_context: ContextVar[_RequestContext | None] = ContextVar("wxhook_parse_video", default=None)
_install_lock = threading.Lock()


class _LimitedStream(LimitedStream):
    def __init__(self, stream: httpx.AsyncByteStream, *, headers_only: bool = False, encoding: str = ""):
        super().__init__(stream, _MAX_PAGE_BYTES, headers_only=headers_only, encoding=encoding)

    async def __aiter__(self):
        try:
            async for chunk in super().__aiter__():
                yield chunk
        except ResponseReadError as exc:
            raise stream_error(exc, "平台解析页面过大，已停止处理") from None


async def _guard_request(request: httpx.Request) -> None:
    # The SDK follows redirects. Check every outbound request, not just the
    # original share link, and never let platform content target the local Hook.
    checked_url(str(request.url), media=True)
    # These encodings have bounded streaming decoders below. Do not negotiate
    # optional codecs whose decoder may expand a single chunk without a limit.
    request.headers["Accept-Encoding"] = "gzip, deflate"
    # A stable mobile page layout avoids SDK random-UA-dependent SSR schemas.
    host = request.url.host
    if host in PLATFORMS["douyin"][1]:
        request.headers["User-Agent"] = USER_AGENT
        request.headers.setdefault("Referer", REFERERS["douyin"])
    elif host in PLATFORMS["xiaohongshu"][1]:
        request.headers.setdefault("Referer", REFERERS["xiaohongshu"])
    context = _request_context.get()
    if context and context.xhs_cookie and host == "www.xiaohongshu.com" and request.url.scheme == "https":
        request.headers["Cookie"] = context.xhs_cookie


async def _guard_response(response: httpx.Response) -> None:
    if response.url.host == "www.xiaohongshu.com" and response.url.path.rstrip("/") == "/login":
        raise DouyinError("小红书要求登录，请在测试机 parse-video.local.yml 配置本人的有效 Cookie，再重新发送分享链接")
    content_type = response.headers.get("content-type", "").lower()
    binary = content_type.startswith(("video/", "audio/", "image/", "application/octet-stream"))
    length = response.headers.get("content-length", "")
    if not binary and length.isdigit() and int(length) > _MAX_PAGE_BYTES:
        raise DouyinError("平台解析页面过大，已停止处理")
    if response.is_stream_consumed:
        if not binary and len(response.content) > _MAX_PAGE_BYTES:
            raise DouyinError("平台解析页面过大，已停止处理")
        return
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if not binary and encoding not in {"", "identity", "gzip", "deflate"}:
        raise DouyinError("平台页面压缩格式不受支持，已停止处理")
    response.stream = _LimitedStream(response.stream, headers_only=binary, encoding=encoding)
    if not binary and encoding in {"gzip", "deflate"}:
        # The wrapper yields decoded bytes. Prevent HTTPX from decoding again
        # or reporting the compressed Content-Length as the decoded length.
        response.headers.pop("content-encoding", None)
        response.headers.pop("content-length", None)


def _load_library():
    try:
        library = importlib.import_module("parse_video_py")
        utils = importlib.import_module("parse_video_py.utils")
    except ImportError as exc:
        raise DouyinError("缺少 parse-video-py 依赖，请在测试机项目终端执行 python -m pip install -e . 后重启") from exc
    # The pinned SDK centralizes HTTP clients in this small factory. Bind a
    # context-scoped network policy there; unrelated uses of the SDK retain
    # their original behaviour, and concurrent calls use their own transports.
    with _install_lock:
        original = utils.create_async_client
        if not getattr(original, "_wxhook_guarded", False):
            def guarded_client(**kwargs):
                context = _request_context.get()
                if context is not None:
                    kwargs["trust_env"] = False
                    kwargs.setdefault("timeout", httpx.Timeout(25, connect=8))
                    hooks = {key: list(value) for key, value in kwargs.get("event_hooks", {}).items()}
                    hooks["request"] = [_guard_request, *hooks.get("request", [])]
                    hooks["response"] = [_guard_response, *hooks.get("response", [])]
                    kwargs["event_hooks"] = hooks
                    if context.transport is not None:
                        kwargs["transport"] = context.transport
                    # The upstream factory still honours explicit PARSE_VIDEO_PROXY.
                return original(**kwargs)

            guarded_client._wxhook_guarded = True
            for module in list(sys.modules.values()):
                if module and getattr(module, "__name__", "").startswith("parse_video_py") and getattr(module, "create_async_client", None) is original:
                    module.create_async_client = guarded_client
    return library.parse_video_share_url


def _field(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def normalize_media(info: Any, source: str) -> ParsedMedia:
    platform = source_platform(source)
    images = _field(info, "images", []) or []
    if not isinstance(images, (list, tuple)) or len(images) > 100:
        raise DouyinError("图集格式不受支持或超过 100 张，已停止处理")
    assets = []
    for image in images:
        value = _field(image, "url")
        if not isinstance(value, str) or not value:
            raise DouyinError("解析器返回的图集缺少图片地址，未回传不完整图集")
        assets.append({"kind": "image", "urls": [checked_url(value, media=True)]})
        live_photo = _field(image, "live_photo_url")
        if live_photo is not None and live_photo != "":
            if not isinstance(live_photo, str):
                raise DouyinError("解析器返回的实况图视频地址无效")
            assets.append({"kind": "video", "urls": [checked_url(live_photo, media=True)]})
    if not assets:
        value = _field(info, "video_url")
        if not isinstance(value, str) or not value:
            raise DouyinError("解析器没有返回视频或图片，作品可能不可用或需要登录")
        assets.append({"kind": "video", "urls": [checked_url(value, media=True)]})
    # Keep each Live Photo video next to its still image; omit cover and music.
    return ParsedMedia("", str(_field(info, "title") or "未命名作品")[:2000], str(_field(_field(info, "author"), "name") or "")[:200], "images" if images else "video", assets, platform=platform)


class ParseVideoParser:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def parse(self, source: str) -> ParsedMedia:
        source = _normalize_source(source)
        try:
            return await self._parse_sdk(source)
        except DouyinError as primary_error:
            if source_platform(source) != "douyin":
                raise
            # Keep this recovery in the shared adapter: both queued group jobs
            # and /api/parse need it, without requiring a third-party endpoint.
            try:
                async with asyncio.timeout(60), httpx.AsyncClient(
                    transport=self.transport, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(12, connect=5), proxy=os.getenv("PARSE_VIDEO_PROXY") or None,
                ) as client:
                    return await DouyinParser(client).parse(source)
            except Exception:
                # Preserve the SDK's sanitized error if recovery also fails;
                # malformed platform data or proxy settings must not leak.
                raise primary_error from None

    async def _parse_sdk(self, source: str) -> ParsedMedia:
        platform = source_platform(source)
        name = PLATFORMS[platform][0]
        resolver = _load_library()
        cookie = _local_xhs_cookie() if platform == "xiaohongshu" else ""
        token = _request_context.set(_RequestContext(self.transport, cookie))
        try:
            result = await resolver(source)
            return normalize_media(result, source)
        except DouyinError:
            raise
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise DouyinError(f"{name}请求超时，请稍后重新发送链接") from exc
        except httpx.HTTPStatusError as exc:
            raise DouyinError(f"{name}返回 HTTP {exc.response.status_code}，请在浏览器确认分享可访问") from exc
        except httpx.HTTPError as exc:
            raise DouyinError(f"{name}连接失败，请检查网络或 PARSE_VIDEO_PROXY 配置") from exc
        except Exception as exc:
            # Upstream errors can include full share URLs or proxy credentials.
            # Keep feedback actionable without echoing them back into a group.
            raise DouyinError(f"{name}未返回有效媒体数据，分享可能失效、需要登录或平台解析规则已变化；请在浏览器确认作品可访问") from exc
        finally:
            _request_context.reset(token)
