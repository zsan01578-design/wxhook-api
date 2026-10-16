from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

import httpx

from wxhook_api.core.douyin_config import DouyinOptions, GroupSettings, save_group_settings
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin_parser import DouyinError, DouyinParser, USER_AGENT, checked_url, extract_links


def media_extension(prefix: bytes, kind: str) -> str:
    if kind == "image":
        if prefix.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if prefix[:6] in {b"GIF87a", b"GIF89a"}:
            return ".gif"
        if prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
            return ".webp"
        if prefix[4:8] == b"ftyp" and b"avif" in prefix[:32]:
            return ".avif"
    elif kind == "video" and prefix[4:8] == b"ftyp":
        return ".mp4"
    raise DouyinError("下载响应不是预期的视频或图片，可能需要登录或访问验证")


class DouyinService:
    def __init__(self, options: DouyinOptions, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.options = options
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._worker: asyncio.Task | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._accepting = False
        self._config_lock = asyncio.Lock()
        self.revision = uuid4().hex

    def allowed(self, group: str) -> bool:
        return self.options.settings.enabled and group in self.options.settings.groups

    async def configure(self, settings: GroupSettings) -> None:
        async with self._config_lock:
            self.options = await asyncio.to_thread(save_group_settings, self.options, settings)

    def _changed(self) -> None:
        self.revision = uuid4().hex

    def snapshot(self) -> dict[str, Any]:
        return {"revision": self.revision, "jobs": list(reversed(self._jobs.values()))[:100], "pending": self._queue.qsize()}

    def _load_saved(self) -> None:
        if not self.options.output_dir.is_dir():
            return
        paths = sorted(self.options.output_dir.glob("*/result.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:100]
        for path in reversed(paths):
            try:
                if path.stat().st_size > 3 * 1024 * 1024:
                    continue
                job = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(job, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(job.get("id", ""))) or path.parent.name != job["id"]:
                    continue
                if job.get("status") in {"queued", "parsing", "downloading"}:
                    job.update(status="interrupted", error="上次服务已停止，请重新发送链接")
                self._jobs[job["id"]] = job
            except (OSError, ValueError):
                continue

    async def start(self) -> None:
        await asyncio.to_thread(self._load_saved)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"},
            timeout=httpx.Timeout(20, connect=8), follow_redirects=False,
            trust_env=False, transport=self._transport,
        )
        self._accepting = True
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._accepting = False
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def receive(self, event: CallbackEvent) -> None:
        group = event.roomid or (event.wxid if event.wxid.endswith("@chatroom") else "")
        if not self._accepting or not self.allowed(group):
            return
        for link in extract_links(event.content):
            key = (group, str(event.msgid), link)
            deduplicate = str(event.msgid) not in {"", "0"}
            if deduplicate and key in self._seen:
                continue
            job = {
                "id": uuid4().hex, "group_id": group, "sender": event.sender[:256],
                "msgid": str(event.msgid), "source_url": link,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "queued", "title": "", "author": "", "kind": "", "assets": [], "files": [], "error": "",
            }
            self._jobs[job["id"]] = job
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                job.update(status="failed", error="解析队列已满，请稍后重新发送链接")
            else:
                if deduplicate:
                    self._seen[key] = None
                    if len(self._seen) > 1000:
                        self._seen.popitem(last=False)
            while len(self._jobs) > 150:
                self._jobs.popitem(last=False)
            self._changed()

    def _save(self, job: dict[str, Any]) -> None:
        folder = self.options.output_dir / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / "result.json.tmp"
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(folder / "result.json")

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if not self.allowed(job["group_id"]):
                    raise DouyinError("该群已移出配置或解析功能已停用")
                job["status"] = "parsing"
                self._changed()
                async with asyncio.timeout(240):
                    parsed = await DouyinParser(self._client).parse(job["source_url"])
                    job.update(item_id=parsed.item_id, title=parsed.title, author=parsed.author, kind=parsed.kind, assets=parsed.assets)
                    if not self.allowed(job["group_id"]):
                        raise DouyinError("该群已移出配置或解析功能已停用")
                    if self.options.settings.auto_download:
                        job["status"] = "downloading"
                        self._changed()
                        folder = self.options.output_dir / job["id"]
                        folder.mkdir(parents=True, exist_ok=True)
                        downloaded = 0
                        for index, asset in enumerate(parsed.assets, 1):
                            file = await self._download_asset(asset, folder, index, job["group_id"], 500 * 1024 * 1024 - downloaded)
                            job["files"].append(file)
                            downloaded += file["size"]
                            self._changed()
                        job["status"] = "completed"
                    else:
                        job["status"] = "parsed"
            except asyncio.CancelledError:
                job.update(status="interrupted", error="服务已停止，请重新发送链接")
                raise
            except DouyinError as exc:
                job.update(status="failed", error=str(exc))
            except httpx.HTTPStatusError as exc:
                job.update(status="failed", error=f"抖音请求返回 HTTP {exc.response.status_code}，请确认链接可访问")
            except (httpx.HTTPError, TimeoutError):
                job.update(status="failed", error="网络连接失败或解析下载超时，请检查网络后重试")
            except OSError:
                job.update(status="failed", error="无法保存文件，请检查下载目录权限和剩余磁盘空间")
            except Exception:
                job.update(status="failed", error="分享数据格式不受支持，请提供原始分享链接以排查")
            finally:
                try:
                    await asyncio.to_thread(self._save, job)
                except OSError:
                    job.update(status="failed", error="无法保存解析记录，请检查下载目录权限和剩余磁盘空间")
                self._queue.task_done()
                self._changed()

    async def _download_asset(self, asset: dict, folder: Path, index: int, group: str, remaining: int) -> dict:
        last_error = None
        for url in asset["urls"]:
            try:
                return await self._download(url, asset["kind"], folder, index, group, remaining)
            except (DouyinError, httpx.HTTPError) as exc:
                last_error = exc
        if isinstance(last_error, DouyinError):
            raise last_error
        raise DouyinError("媒体下载失败，请确认作品可公开访问后重新发送链接") from last_error

    async def _download(self, url: str, kind: str, folder: Path, index: int, group: str, remaining: int) -> dict:
        if remaining <= 0:
            raise DouyinError("单个作品下载量超过 500 MB，已停止")
        limit = min(self.options.max_file_bytes, remaining)
        current = url
        for _ in range(7):
            current = checked_url(current, media=True)
            async with self._client.stream("GET", current) as response:
                if response.is_redirect:
                    current = checked_url(urljoin(current, response.headers.get("location", "")), media=True)
                    continue
                response.raise_for_status()
                if response.status_code != 200:
                    raise DouyinError("媒体响应不完整，已停止保存")
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > limit:
                    raise DouyinError("媒体文件超过配置的大小限制，已停止下载")
                partial = folder / f"{index:03d}.part"
                size = 0
                prefix = bytearray()
                try:
                    with partial.open("wb") as output:
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            if not self.allowed(group):
                                raise DouyinError("该群已移出配置或解析功能已停用")
                            size += len(chunk)
                            if size > limit:
                                raise DouyinError("媒体文件超过配置的大小限制，已停止下载")
                            if len(prefix) < 32:
                                prefix.extend(chunk[:32 - len(prefix)])
                            output.write(chunk)
                    extension = media_extension(bytes(prefix), kind)
                    if length.isdigit() and size != int(length) and not response.headers.get("content-encoding"):
                        raise DouyinError("下载内容不完整，请重新发送链接")
                    filename = f"{index:03d}{extension}"
                    partial.replace(folder / filename)
                    return {"name": filename, "kind": kind, "size": size}
                finally:
                    partial.unlink(missing_ok=True)
        raise DouyinError("媒体链接跳转次数过多")

    def file_path(self, job_id: str, filename: str) -> Path | None:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not re.fullmatch(r"\d{3}\.(mp4|jpg|png|webp|gif|avif)", filename):
            return None
        job = self._jobs.get(job_id)
        if not job or not any(file.get("name") == filename for file in job.get("files", [])):
            return None
        path = (self.options.output_dir / job_id / filename).resolve()
        if not path.is_relative_to(self.options.output_dir.resolve()) or not path.is_file():
            return None
        return path
