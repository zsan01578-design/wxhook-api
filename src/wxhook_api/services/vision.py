"""Read WeChat image callbacks and prepare bounded data URLs for vision models."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
import time
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import url2pathname
import warnings

from PIL import Image, ImageOps

from wxhook_api.clients.hook_client import HookClient, HookResponse
from wxhook_api.schemas.requests import MessagePictureRequest


MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 3 * 1024 * 1024
MAX_DATA_URL_CHARS = 4 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_EDGE = 1600


class VisionError(RuntimeError):
    """A safe image-reading diagnostic without local paths or response bodies."""


def image_path(response: HookResponse) -> str:
    if response.status_code != 200:
        raise VisionError("微信接口未能读取这张图片，请让发送者重新发送。")
    data = response.data
    for _ in range(3):
        if not isinstance(data, dict):
            raise VisionError("微信接口返回的图片信息格式无效。")
        codes = [data[key] for key in ("ret", "status", "db_status") if key in data]
        if any(str(code).strip().lower() not in {"0", "false", "none"} for code in codes):
            raise VisionError("微信接口未能读取这张图片，请让发送者重新发送。")
        path = data.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()
        nested = data.get("data")
        if not isinstance(nested, dict):
            break
        data = nested
    raise VisionError("微信接口返回的图片信息格式无效。")


def _local_path(value: str) -> Path:
    if len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise VisionError("微信接口返回的图片路径无效。")
    if value.lower().startswith("file://"):
        parsed = urlsplit(value)
        value = url2pathname(parsed.path)
    target = Path(value).expanduser()
    if not target.is_absolute():
        target = target.resolve()
    return target


def _read_file(value: str) -> bytes:
    target = _local_path(value)
    try:
        if not target.is_file():
            raise VisionError("微信图片文件不存在，请让发送者重新发送。")
        if target.stat().st_size > MAX_SOURCE_BYTES:
            raise VisionError("图片文件过大，无法进行理解。")
        content = target.read_bytes()
    except VisionError:
        raise
    except OSError:
        raise VisionError("无法读取微信图片文件。") from None
    if len(content) > MAX_SOURCE_BYTES:
        raise VisionError("图片文件过大，无法进行理解。")
    return content


def _jpeg(content: bytes) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source:
                if source.width * source.height > MAX_PIXELS:
                    raise VisionError("图片尺寸过大，无法进行理解。")
                source.load()
                image = ImageOps.exif_transpose(source)
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.split()[-1])
                    image = background
                else:
                    image = image.convert("RGB")
                for edge, quality in ((MAX_EDGE, 85), (1280, 78), (1024, 72)):
                    resized = image.copy()
                    resized.thumbnail((edge, edge))
                    output = BytesIO()
                    resized.save(output, format="JPEG", quality=quality, optimize=True)
                    encoded = output.getvalue()
                    if len(encoded) <= MAX_OUTPUT_BYTES:
                        return encoded
    except VisionError:
        raise
    except Exception:
        raise VisionError("无法解析这张图片，请发送 JPG、PNG 或 WebP 图片。") from None
    raise VisionError("图片压缩后仍然过大，无法进行理解。")


class ImageReader:
    def __init__(self, client: HookClient):
        self.client = client

    async def data_url(self, msgid: str) -> str:
        if not msgid or msgid in {"0", "None"}:
            raise VisionError("这条图片消息缺少可用的消息 ID，无法读取图片。")
        response = await self.client.get_message_pic(MessagePictureRequest(msgid=str(msgid)))
        path = image_path(response)
        content = await asyncio.to_thread(_read_file, path)
        jpeg = await asyncio.to_thread(_jpeg, content)
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        if len(data_url) > MAX_DATA_URL_CHARS:
            raise VisionError("图片过大，无法发送给模型。")
        return data_url


class RecentImageMemory:
    """Per-member recent image IDs; never stores image bytes."""

    def __init__(self, *, ttl: float = 600.0, max_items: int = 100, now: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self.max_items = max_items
        self._now = now
        self._items: OrderedDict[tuple[str, str, str], tuple[float, str]] = OrderedDict()

    @staticmethod
    def _key(account: str, group: str, sender: str):
        if not account or not group or sender in {"", "0", group, "notify@all"}:
            return None
        return account, group, sender

    def _prune(self):
        now = self._now()
        expired = [key for key, (expires, _) in self._items.items() if expires <= now]
        for key in expired:
            self._items.pop(key, None)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def remember(self, account: str, group: str, sender: str, msgid: str) -> None:
        key = self._key(account, group, sender)
        if key is None or not msgid or msgid in {"0", "None"}:
            return
        self._items[key] = (self._now() + self.ttl, str(msgid)[:128])
        self._items.move_to_end(key)
        self._prune()

    def get(self, account: str, group: str, sender: str) -> str:
        key = self._key(account, group, sender)
        if key is None:
            return ""
        item = self._items.get(key)
        if item is None:
            return ""
        expires, msgid = item
        if expires <= self._now():
            self._items.pop(key, None)
            return ""
        return msgid

    def clear_group(self, account: str, group: str):
        for key in list(self._items):
            if key[:2] == (account, group):
                self._items.pop(key, None)

    def clear_member(self, account: str, group: str, sender: str):
        self._items.pop(self._key(account, group, sender), None)

    def clear(self):
        self._items.clear()
