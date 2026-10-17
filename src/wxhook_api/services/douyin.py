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
from wxhook_api.services.douyin_delivery import DeliveryError, DouyinMessenger
from wxhook_api.services.douyin_api import DouyinAPIParser
from wxhook_api.services.parse_video import ParseVideoParser, REFERERS, source_platform
from wxhook_api.services.groups import GroupManager, event_group


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
        self._notice_worker: asyncio.Task | None = None
        self._notices: asyncio.Queue[tuple[dict, str, str]] = asyncio.Queue(maxsize=20)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._accepting = False
        self._config_lock = asyncio.Lock()
        self.revision = uuid4().hex
        self.group_manager: GroupManager | None = None
        self.messenger: DouyinMessenger | None = None

    def allowed(self, group: str, sender: str = "") -> bool:
        fallback = self.options.settings.enabled and group in self.options.settings.groups
        if self.group_manager is None:
            return fallback
        return self.group_manager.allowed(group, sender) and self.group_manager.feature(group, "douyin_enabled", fallback)

    def auto_download(self, group: str) -> bool:
        fallback = self.options.settings.auto_download
        return self.group_manager.feature(group, "auto_download", fallback) if self.group_manager else fallback

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
                if job.get("status") in {"queued", "parsing", "downloading", "sending"}:
                    job.update(status="interrupted", error="上次服务已停止，请重新发送链接")
                    if job.get("delivery_status") == "sending":
                        job.update(delivery_status="unknown", delivery_error="上次回传被中断，部分文件可能已发送，请先查看群聊；不会自动重发")
                self._jobs[job["id"]] = job
                if str(job.get("msgid", "0")) not in {"", "0"}:
                    self._seen[(job.get("group_id", ""), str(job["msgid"]), job.get("source_url", ""))] = None
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
        self._notice_worker = asyncio.create_task(self._run_notices())

    async def stop(self) -> None:
        self._accepting = False
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._notice_worker is not None:
            self._notice_worker.cancel()
            await asyncio.gather(self._notice_worker, return_exceptions=True)
            self._notice_worker = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def receive(self, event: CallbackEvent) -> None:
        group = event_group(event)
        if not self._accepting or not self.allowed(group, event.sender):
            return
        # Recognize all supported platforms even when an older parser is selected.
        # A configuration mismatch must produce a visible job and failure notice.
        for link in extract_links(event.content, multi_platform=True):
            key = (group, str(event.msgid), link)
            deduplicate = str(event.msgid) not in {"", "0"}
            if deduplicate and key in self._seen:
                continue
            job = {
                "id": uuid4().hex, "group_id": group, "sender": event.sender[:256],
                "msgid": str(event.msgid), "source_url": link,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "queued", "title": "", "author": "", "kind": "", "assets": [], "files": [], "error": "",
                "delivery_status": "pending", "delivery_error": "", "notifications": {},
            }
            self._jobs[job["id"]] = job
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                job.update(status="failed", error="解析队列已满，请稍后重新发送链接")
                try:
                    self._notices.put_nowait((job, "failure", "解析队列已满，请稍后重新发送链接"))
                except asyncio.QueueFull:
                    job["notifications"]["failure"] = {"status": "failed", "error": "群提示队列已满，未发送提示"}
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

    async def _notice(self, job: dict, stage: str, message: str) -> None:
        notifications = job.setdefault("notifications", {})
        if not self.messenger or not self.options.settings.notify_group or not self.allowed(job["group_id"], job["sender"]):
            notifications[stage] = {"status": "skipped"}
            return
        notifications[stage] = {"status": "sending"}
        try:
            # Do not quote the original link/title: our own callbacks must not
            # create a new parsing task from an automatic notification.
            text = message if stage == "start" else f"[分享解析 {job['id'][:8]}] {message}"
            await self.messenger.text(job["group_id"], text)
        except DeliveryError as exc:
            notifications[stage] = {"status": "unknown" if exc.uncertain else "failed", "error": str(exc)}
        except Exception:
            notifications[stage] = {"status": "failed", "error": "群提示发送失败，请检查 Hook 连接"}
        else:
            notifications[stage] = {"status": "sent"}
        self._changed()

    async def _run_notices(self) -> None:
        while True:
            job, stage, message = await self._notices.get()
            try:
                await self._notice(job, stage, message)
                await asyncio.to_thread(self._save, job)
            except OSError:
                job["storage_error"] = "无法保存群提示记录，请检查下载目录"
            finally:
                self._notices.task_done()
                self._changed()

    async def _return_media(self, job: dict) -> None:
        if not self.options.settings.send_results:
            job["delivery_status"] = "disabled"
            return
        if not self.messenger:
            job["delivery_status"] = "unavailable"
            return
        if not job["files"]:
            job.update(delivery_status="skipped", delivery_error="此群自动保存已关闭，没有本地媒体可回传")
            return
        job.update(status="sending", delivery_status="sending")
        self._changed()
        try:
            async with asyncio.timeout(600):
                for file in job["files"]:
                    self._check_download_policy(job["group_id"], job["sender"])
                    if not self.options.settings.send_results:
                        raise DeliveryError("自动回传已关闭，已停止后续发送")
                    path = self.file_path(job["id"], file["name"])
                    if path is None:
                        raise DeliveryError("本地媒体文件不存在，已停止回传")
                    path, mode = await self.messenger.prepare(path, file["kind"])
                    if path.stat().st_size > self.options.max_file_bytes:
                        raise DeliveryError("回传文件超过配置的大小限制，已停止发送")
                    # Policies may change during image conversion.
                    self._check_download_policy(job["group_id"], job["sender"])
                    if not self.options.settings.send_results:
                        raise DeliveryError("自动回传已关闭，已停止后续发送")
                    file.update(send_status="sending", send_mode=mode)
                    await asyncio.to_thread(self._save, job)
                    self._changed()
                    try:
                        # Writing the durable record yields to policy updates.
                        # Recheck after that write, immediately before sending.
                        try:
                            self._check_download_policy(job["group_id"], job["sender"])
                            if not self.options.settings.send_results:
                                raise DeliveryError("自动回传已关闭，已停止后续发送")
                        except (DouyinError, DeliveryError):
                            file["send_status"] = "skipped"
                            raise
                        await self.messenger.send(job["group_id"], path, mode)
                    except DeliveryError as exc:
                        if file.get("send_status") != "skipped":
                            file["send_status"] = "unknown" if exc.uncertain else "failed"
                        raise
                    file["send_status"] = "sent"
                    await asyncio.to_thread(self._save, job)
                    self._changed()
        except Exception as exc:
            uncertain = isinstance(exc, TimeoutError) or getattr(exc, "uncertain", False) or any(file.get("send_status") == "sending" for file in job["files"])
            sent = sum(file.get("send_status") == "sent" for file in job["files"])
            error = str(exc) if isinstance(exc, (DeliveryError, DouyinError)) else "回传中断，请先检查群消息与本地文件"
            job.update(delivery_status="unknown" if uncertain else "partial" if sent else "failed", delivery_error=error)
            await self._notice(job, "delivery_failure", f"媒体回传未全部确认（已确认 {sent}/{len(job['files'])} 个）。{error}。已停止后续发送，文件仍保存在本机。")
        else:
            job["delivery_status"] = "sent"
        finally:
            job["status"] = "completed" if job["files"] else "parsed"
            self._changed()

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if not self.allowed(job["group_id"], job["sender"]):
                    raise DouyinError("群聊或成员已被限制，或解析功能已停用")
                job["status"] = "parsing"
                self._changed()
                await asyncio.to_thread(self._save, job)
                await self._notice(job, "start", "开始解析")
                async with asyncio.timeout(240):
                    config = self.options.settings
                    job.update(parser_provider=config.parser_provider, platform=source_platform(job["source_url"]))
                    if config.parser_provider != "parse_video" and job["platform"] != "douyin":
                        raise DouyinError("当前解析来源仅支持抖音，请在资源页选择 parse-video-py 并保存后重新发送链接")
                    if config.parser_provider == "parse_video":
                        parser = ParseVideoParser(transport=self._transport)
                    else:
                        parser = DouyinAPIParser(self._client, config.parser_api_url) if config.parser_provider == "api" else DouyinParser(self._client)
                    parsed = await parser.parse(job["source_url"])
                    job.update(item_id=parsed.item_id, title=parsed.title, author=parsed.author, kind=parsed.kind, assets=parsed.assets, platform=parsed.platform)
                    if not self.allowed(job["group_id"], job["sender"]):
                        raise DouyinError("群聊或成员已被限制，或解析功能已停用")
                    if self.auto_download(job["group_id"]):
                        job["status"] = "downloading"
                        self._changed()
                        folder = self.options.output_dir / job["id"]
                        folder.mkdir(parents=True, exist_ok=True)
                        downloaded = 0
                        for index, asset in enumerate(parsed.assets, 1):
                            file = await self._download_asset(asset, folder, index, job["group_id"], 500 * 1024 * 1024 - downloaded, job["sender"], parsed.platform)
                            job["files"].append(file)
                            downloaded += file["size"]
                            self._changed()
                        job["status"] = "completed"
                    else:
                        job["status"] = "parsed"
                await self._return_media(job)
            except asyncio.CancelledError:
                job.update(status="interrupted", error="服务已停止，请重新发送链接")
                if job.get("delivery_status") == "sending":
                    job.update(delivery_status="unknown", delivery_error="发送被中断，部分文件可能已发出，请先查看群聊")
                raise
            except DouyinError as exc:
                job.update(status="failed", error=str(exc))
            except httpx.HTTPStatusError as exc:
                job.update(status="failed", error=f"平台请求返回 HTTP {exc.response.status_code}，请确认链接可访问")
            except (httpx.HTTPError, TimeoutError):
                job.update(status="failed", error="网络连接失败或解析下载超时，请检查网络后重试")
            except OSError:
                job.update(status="failed", error="无法保存文件，请检查下载目录权限和剩余磁盘空间")
            except Exception:
                job.update(status="failed", error="分享数据格式不受支持，请提供原始分享链接以排查")
            finally:
                if job["status"] == "failed":
                    await self._notice(job, "failure", f"解析失败：{job['error']}")
                try:
                    await asyncio.to_thread(self._save, job)
                except OSError:
                    job["storage_error"] = "无法保存解析记录，请检查下载目录权限和剩余磁盘空间"
                self._queue.task_done()
                self._changed()

    async def _download_asset(self, asset: dict, folder: Path, index: int, group: str, remaining: int, sender: str = "", platform: str = "douyin") -> dict:
        last_error = None
        for url in asset["urls"]:
            self._check_download_policy(group, sender)
            try:
                return await self._download(url, asset["kind"], folder, index, group, remaining, sender, platform)
            except (DouyinError, httpx.HTTPError) as exc:
                last_error = exc
        if isinstance(last_error, DouyinError):
            raise last_error
        raise DouyinError("媒体下载失败，请确认作品可公开访问后重新发送链接") from last_error

    def _check_download_policy(self, group: str, sender: str) -> None:
        if not self.allowed(group, sender) or not self.auto_download(group):
            raise DouyinError("群聊或成员已被限制，或自动保存已停用")

    async def _download(self, url: str, kind: str, folder: Path, index: int, group: str, remaining: int, sender: str = "", platform: str = "douyin") -> dict:
        if remaining <= 0:
            raise DouyinError("单个作品下载量超过 500 MB，已停止")
        limit = min(self.options.max_file_bytes, remaining)
        current = url
        for _ in range(7):
            self._check_download_policy(group, sender)
            current = checked_url(current, media=True)
            async with self._client.stream("GET", current, headers={"Referer": REFERERS.get(platform, REFERERS["douyin"])}) as response:
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
                            self._check_download_policy(group, sender)
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
