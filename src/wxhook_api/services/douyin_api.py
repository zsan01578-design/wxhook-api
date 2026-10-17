from __future__ import annotations

import html
import json
import re
from typing import Any

import httpx

from wxhook_api.services.douyin_parser import DouyinError, LINK_PATTERN, ParsedMedia, checked_url, item_identity


def _media_urls(values: list[Any]) -> list[str]:
    result = []
    for value in values:
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise DouyinError("解析接口的媒体地址格式不受支持，请检查接口返回")
        url = checked_url(value, media=True)
        if url not in result:
            result.append(url)
    return result[:4]


def _error_message(payload: dict) -> str:
    code = str(payload.get("code", "缺失"))
    code = code if re.fullmatch(r"-?\d{1,8}", code) else "未知"
    if code == "108":
        return "解析接口返回 code=108：会员接口不存在，请检查接口账号、开通状态及完整地址"
    detail = payload.get("msg")
    if not isinstance(detail, str):
        detail = "接口未返回成功状态"
    # Notifications may be echoed by Hook: never repeat a share link in an error.
    detail = LINK_PATTERN.sub("[链接]", html.unescape(detail))
    detail = re.sub(r"<[^>]*>", "", detail)
    detail = re.sub(r"[A-Fa-f0-9]{24,}", "[标识]", detail)
    detail = " ".join(detail.split())[:120]
    return f"解析接口返回失败（code={code}）：{detail}"


def parse_api_response(payload: Any, source: str) -> ParsedMedia:
    if not isinstance(payload, dict):
        raise DouyinError("解析接口响应应为 JSON 对象")
    # This provider returns status=101 on success. Its success field is code=200.
    if type(payload.get("code")) not in {int, str} or str(payload["code"]) != "200":
        raise DouyinError(_error_message(payload))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DouyinError("解析接口成功响应缺少 data 对象")
    pictures = data.get("pics")
    if pictures is None or pictures == []:
        pictures = data.get("download_image") or []
    if not isinstance(pictures, list):
        raise DouyinError("解析接口 data.pics 应为图片数组，请提供图文返回示例以适配")
    if len(pictures) > 100:
        raise DouyinError("图集超过 100 张，已停止自动下载")
    assets = []
    for picture in pictures:
        if isinstance(picture, str):
            urls = _media_urls([picture])
        elif isinstance(picture, dict):
            urls = _media_urls([picture.get(key) for key in ("url", "src", "image_url", "down")])
        else:
            raise DouyinError("解析接口图片条目格式不受支持")
        if not urls:
            raise DouyinError("解析接口返回了空图片地址，图集未完整解析")
        assets.append({"kind": "image", "urls": urls})
    if not pictures:
        urls = _media_urls([data.get("url"), data.get("down")])
        if not urls:
            raise DouyinError("解析接口未返回视频地址或图片列表")
        assets.append({"kind": "video", "urls": urls})
    identity = item_identity(source)
    identifier = data.get("aweme_id") or data.get("item_id") or (identity[1] if identity else "")
    author = data.get("author", "")
    if isinstance(author, dict):
        author = author.get("nickname") or author.get("name") or ""
    return ParsedMedia(str(identifier)[:100], str(data.get("title") or "未命名作品")[:2000], str(author)[:200], "images" if pictures else "video", assets)


class DouyinAPIParser:
    def __init__(self, client: httpx.AsyncClient, endpoint: str):
        self.client, self.endpoint = client, endpoint

    async def parse(self, source: str) -> ParsedMedia:
        source = checked_url(source)
        if not self.endpoint:
            raise DouyinError("尚未配置解析接口，请在资源页面填写完整接口地址并保存")
        # The endpoint is validated local configuration, not a URL from a group.
        try:
            async with self.client.stream("GET", self.endpoint, params={"url": source},
                                          headers={"Accept": "application/json", "Referer": "https://jk.y-l.cc/"},
                                          timeout=httpx.Timeout(60, connect=8)) as response:
                if response.status_code != 200:
                    raise DouyinError(f"解析接口返回 HTTP {response.status_code}，请检查接口状态")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 3 * 1024 * 1024:
                        raise DouyinError("解析接口响应过大，已停止处理")
        except httpx.HTTPError as exc:
            raise DouyinError("解析接口连接失败或超时，请检查接口地址及服务状态") from exc
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise DouyinError("解析接口没有返回有效 JSON，请检查服务状态") from exc
        return parse_api_response(payload, source)
