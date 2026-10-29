from __future__ import annotations

from dataclasses import dataclass
import html
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

import httpx

from wxhook_api.core.bounded_http import ResponseReadError, response_bytes


SHARE_HOSTS = {"douyin.com", "www.douyin.com", "v.douyin.com", "m.douyin.com", "iesdouyin.com", "www.iesdouyin.com", "jingxuan.douyin.com"}
PLATFORM_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "xhslink.cn", "bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}
MEDIA_DOMAINS = ("douyinvod.com", "douyinpic.com", "byteimg.com", "ibytedtos.com", "snssdk.com", "amemv.com", "pstatp.com", "bytecdn.cn", "zjcdn.com", "ixigua.com", "365yg.com", "xiaohongshu.com", "xhscdn.com", "bilibili.com", "bilivideo.com", "bilivideo.cn", "hdslb.com")
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
LINK_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]+", re.IGNORECASE)


class DouyinError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def stream_error(exc: ResponseReadError, size_error: str, kind: str = "页面") -> DouyinError:
    messages = {"size": size_error, "encoding": f"平台{kind}压缩格式不受支持，已停止处理",
                "format": f"平台压缩{kind}格式无效，已停止处理",
                "incomplete": f"平台压缩{kind}不完整，已停止处理"}
    return DouyinError(messages[str(exc)])


async def read_response_bytes(response: httpx.Response, limit: int, *, size_error: str, kind: str = "页面"):
    try:
        async for chunk in response_bytes(response, limit):
            yield chunk
    except ResponseReadError as exc:
        raise stream_error(exc, size_error, kind) from None


def checked_url(url: str, *, media: bool = False, multi_platform: bool = False) -> str:
    try:
        if len(url) > 8192 or any(ord(char) < 32 for char in url):
            raise ValueError
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        allowed = host in SHARE_HOSTS
        if multi_platform or media:
            allowed = allowed or host in PLATFORM_HOSTS
        if media:
            allowed = allowed or any(host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS)
            allowed = allowed or host == "jk.y-l.cc"
        if not allowed or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError
    except ValueError as exc:
        raise DouyinError("链接域名或端口不受支持，已停止请求") from exc
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def extract_links(content: str, *, multi_platform: bool = False) -> list[str]:
    text = html.unescape(html.unescape(content.replace(r"\/", "/")))
    results = []
    for match in LINK_PATTERN.finditer(text):
        candidate = match.group().rstrip(".,;!?)]\u3002")
        try:
            candidate = checked_url(candidate, multi_platform=multi_platform)
        except DouyinError:
            continue
        if candidate not in results:
            results.append(candidate)
    return results[:5]


def item_identity(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(checked_url(url))
    match = re.search(r"/(video|note|slides)/(\d{5,25})(?:/|$)", parsed.path)
    if match:
        return match.group(1), match.group(2)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id", "item_id"):
        value = query.get(key, [""])[0]
        if re.fullmatch(r"\d{5,25}", value):
            return "video", value
    return None


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[tuple[str, str]] = []
        self.current: list[str] | None = None
        self.identifier = ""

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.identifier = dict(attrs).get("id", "")
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append((self.identifier, "".join(self.current)))
            self.current = None


def page_data(document: str) -> list[Any]:
    parser = _Scripts()
    parser.feed(document)
    payloads = []
    decoder = json.JSONDecoder()
    for identifier, script in parser.scripts:
        candidates = []
        if identifier in {"RENDER_DATA", "__NEXT_DATA__", "_ROUTER_DATA"}:
            candidates.append(unquote(script.strip()))
        for assignment in re.finditer(r"(?:window\.)?(?:_ROUTER_DATA|__INITIAL_STATE__)\s*=\s*", script):
            candidates.append(script[assignment.end():].lstrip())
        for candidate in candidates:
            try:
                candidate = re.sub(r'"(?:\\.|[^"\\])*"|(?<![\w$])undefined(?![\w$])', lambda match: "null" if match.group() == "undefined" else match.group(), candidate)
                value, _ = decoder.raw_decode(candidate)
                if isinstance(value, str):
                    value = json.loads(value)
            except (ValueError, RecursionError):
                continue
            if isinstance(value, (dict, list)):
                payloads.append(value)
    return payloads


def find_item(payload: Any, item_id: str) -> dict | None:
    pending = [payload]
    visited = 0
    while pending and visited < 50_000:
        value = pending.pop()
        visited += 1
        if isinstance(value, dict):
            identity = value.get("aweme_id", value.get("id"))
            if str(identity) == item_id and any(key in value for key in ("video", "images", "image_post_info")):
                return value
            pending.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            pending.extend(reversed(value))
    return None


def address_urls(address: Any) -> list[str]:
    if isinstance(address, dict):
        values = address.get("url_list") or address.get("urlList") or []
    elif isinstance(address, str):
        values = [address]
    else:
        values = []
    result = []
    for value in values:
        if isinstance(value, str):
            try:
                checked = checked_url(value, media=True)
            except DouyinError:
                continue
            if checked not in result:
                result.append(checked)
    return result[:4]


@dataclass(frozen=True)
class ParsedMedia:
    item_id: str
    title: str
    author: str
    kind: str
    assets: list[dict[str, Any]]
    platform: str = "douyin"


def parse_item(item: dict, item_id: str) -> ParsedMedia:
    assets = []
    pictures = item.get("images") or (item.get("image_post_info") or {}).get("images") or []
    if not isinstance(pictures, list):
        raise DouyinError("图集格式不受支持，已停止解析")
    if len(pictures) > 100:
        raise DouyinError("图集超过 100 张，已停止自动下载")
    for image in pictures:
        if not isinstance(image, dict):
            raise DouyinError("图集图片条目格式不受支持，未回传不完整图集")
        urls = address_urls(image) or address_urls(image.get("display_image")) or address_urls(image.get("image"))
        if not urls:
            raise DouyinError("图集缺少可用的图片地址，未回传不完整图集")
        assets.append({"kind": "image", "urls": urls})
    if not assets:
        video = item.get("video") or {}
        for key in ("play_addr", "play_addr_h264", "playAddr", "download_addr"):
            urls = address_urls(video.get(key))
            if urls:
                assets.append({"kind": "video", "urls": urls})
                break
    if not assets:
        raise DouyinError("作品数据中没有可获取的视频或图片")
    author = item.get("author") or {}
    return ParsedMedia(item_id, str(item.get("desc") or "未命名作品")[:2000], str(author.get("nickname") or "")[:200], "images" if assets[0]["kind"] == "image" else "video", assets)


class DouyinParser:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def document(self, url: str) -> tuple[str, str]:
        current = url
        for _ in range(7):
            current = checked_url(current)
            async with self.client.stream("GET", current, headers={"User-Agent": USER_AGENT,
                                          "Referer": "https://www.douyin.com/", "Accept-Encoding": "gzip, deflate"}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise DouyinError("分享链接跳转地址为空")
                    current = checked_url(urljoin(current, location))
                    continue
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in read_response_bytes(response, 3 * 1024 * 1024, size_error="分享页面过大，已停止解析"):
                    chunks.extend(chunk)
                return current, bytes(chunks).decode("utf-8", errors="replace")
        raise DouyinError("分享链接跳转次数过多")

    async def parse(self, source: str) -> ParsedMedia:
        identity = item_identity(source)
        try:
            final_url, document = await self.document(source)
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                identity = item_identity(str(exc.request.url)) or identity
            if identity is None:
                raise
            # A standard desktop URL can fail while the public mobile share
            # page for the same ID remains available.
            final_url, document = source, ""
        identity = item_identity(final_url) or identity
        if not identity:
            raise DouyinError("未找到抖音作品 ID；链接可能无效或需要在浏览器验证")
        kind, item_id = identity

        def from_document(text):
            for payload in page_data(text):
                item = find_item(payload, item_id)
                if item:
                    return parse_item(item, item_id)
            return None

        parsed = from_document(document)
        if parsed:
            return parsed
        # A note's /share/note/ page can contain only an empty loader while
        # /share/video/ for the same ID still exposes the complete image list.
        candidates = [("www.iesdouyin.com", route) for route in dict.fromkeys((kind, "video", "note", "slides"))]
        candidates.append(("m.douyin.com", kind))
        for host, route in candidates:
            url = f"https://{host}/share/{route}/{item_id}/"
            if url.rstrip("/") == final_url.split("?", 1)[0].rstrip("/"):
                continue
            try:
                _, text = await self.document(url)
                parsed = from_document(text)
                if parsed:
                    return parsed
            except httpx.HTTPError:
                continue
        raise DouyinError("公开分享页未提供媒体数据，可能需要登录验证、作品不可用或页面结构变化；请在浏览器确认该作品可访问")
