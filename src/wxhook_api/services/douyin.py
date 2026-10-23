from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import heapq
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

import httpx

from wxhook_api.core.douyin_config import DouyinOptions, GroupSettings, save_group_settings
from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin_parser import DouyinError, DouyinParser, USER_AGENT, checked_url, extract_links, read_response_bytes
from wxhook_api.services.douyin_delivery import DeliveryBlocked, DeliveryError, DouyinMessenger
from wxhook_api.services.douyin_api import DouyinAPIParser
from wxhook_api.services.parse_video import ParseVideoParser, REFERERS, source_platform
from wxhook_api.services.groups import GroupManager, event_group, normalize_group_sender
from wxhook_api.services.media_cleanup import ACTIVE_STATES, cleanup_media, next_cleanup_at, safe_child, timestamp, utcnow, valid_job_record
from wxhook_api.services.storage_guard import LowDiskSpace, StorageGuard


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
        self._cleanup_worker: asyncio.Task | None = None
        self._maintenance_lock = asyncio.Lock()
        self._active_jobs: set[str] = set()
        self._file_leases: dict[str, int] = {}
        self._notices: asyncio.Queue[tuple[dict, str, str]] = asyncio.Queue(maxsize=20)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._accepting = False
        self._config_lock = asyncio.Lock()
        self.revision = uuid4().hex
        self.group_manager: GroupManager | None = None
        self.messenger: DouyinMessenger | None = None
        self.account_check = None
        self.own_message_check = None
        self.cleanup_status = {"enabled": options.retention.enabled, "next_run_at": "", "last_run_at": "", "error": ""}
        self.storage = StorageGuard(options.output_dir)

    def allowed(self, group: str, sender: str = "") -> bool:
        fallback = self.options.settings.enabled and group in self.options.settings.groups
        if self.group_manager is None:
            return fallback
        return self.group_manager.allowed(group, sender) and self.group_manager.feature(group, "douyin_enabled", fallback)

    def auto_download(self, group: str) -> bool:
        fallback = self.options.settings.auto_download
        return self.group_manager.feature(group, "auto_download", fallback) if self.group_manager else fallback

    async def configure(self, settings: GroupSettings) -> None:
        async def commit():
            self.options = await asyncio.to_thread(save_group_settings, self.options, settings)

        async with self._config_lock:
            await finish_before_cancel(commit())

    def _changed(self) -> None:
        self.revision = uuid4().hex

    def _trim_jobs(self) -> None:
        # This index also authorizes files used by outbound delivery. Queue-full
        # failures must not displace jobs that still need that lookup.
        for job_id in tuple(self._jobs):
            if len(self._jobs) <= 150:
                break
            if job_id not in self._active_jobs and self._jobs[job_id].get("status") not in ACTIVE_STATES:
                self._jobs.pop(job_id)

    def snapshot(self) -> dict[str, Any]:
        return {"revision": self.revision, "jobs": list(reversed(self._jobs.values()))[:100], "pending": self._queue.qsize(), "cleanup": dict(self.cleanup_status), "storage": self.storage.snapshot()}

    def _load_saved(self) -> None:
        if not self.options.output_dir.is_dir():
            return
        root = self.options.output_dir.resolve()
        recent = []
        for path in root.glob("*/result.json"):
            try:
                if not re.fullmatch(r"[a-f0-9]{32}", path.parent.name) or not safe_child(path.parent, root, directory=True) or not safe_child(path, path.parent):
                    continue
                if path.stat().st_size > 3 * 1024 * 1024:
                    continue
                job = json.loads(path.read_text(encoding="utf-8"))
                if not valid_job_record(job, path.parent.name):
                    continue
                if job.get("status") in {"queued", "parsing", "downloading", "sending"}:
                    job.update(status="interrupted", error="上次服务已停止，请重新发送链接")
                    if job.get("delivery_status") == "sending":
                        job.update(delivery_status="unknown", delivery_error="上次回传被中断，部分文件可能已发送，请先查看群聊；不会自动重发")
                # Cleanup rewrites old metadata. Its filesystem mtime must not
                # push old cleaned records ahead of genuinely recent tasks.
                created = timestamp(job.get("created_at")) or timestamp(job.get("finished_at")) or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                entry = (created, job["id"], job)
                if len(recent) < 100:
                    heapq.heappush(recent, entry)
                elif entry[:2] > recent[0][:2]:
                    heapq.heapreplace(recent, entry)
            except (OSError, ValueError):
                continue
        for _, _, job in sorted(recent):
            self._jobs[job["id"]] = job
            if str(job.get("msgid", "0")) not in {"", "0"}:
                self._seen[(job.get("group_id", ""), str(job["msgid"]), job.get("source_url", ""))] = None

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
        if self.options.retention.enabled:
            self._cleanup_worker = asyncio.create_task(self._run_cleanup())

    async def stop(self) -> None:
        self._accepting = False
        if self._cleanup_worker is not None:
            self._cleanup_worker.cancel()
            await asyncio.gather(self._cleanup_worker, return_exceptions=True)
            self._cleanup_worker = None
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
        event = normalize_group_sender(event)
        group = event_group(event)
        if not self._accepting or not self.allowed(group, event.sender):
            return
        # Recognize all supported platforms even when an older parser is selected.
        # A configuration mismatch must produce a visible job and failure notice.
        links = extract_links(event.content, multi_platform=True)
        if links and self.account_check is not None and not await self.account_check():
            return
        # Account verification may have discovered our identity for the first
        # time; exclude self messages again before creating any parsing jobs.
        if self.own_message_check is not None and self.own_message_check(event):
            return
        for link in links:
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
            self._trim_jobs()
            self._changed()

    def _save(self, job: dict[str, Any]) -> None:
        folder = self.options.output_dir / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / "result.json.tmp"
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(folder / "result.json")

    async def _persist(self, job: dict[str, Any]) -> None:
        # A cancelled await cannot stop the writer thread. Settle this write
        # before finalization starts another one or releases cleanup protection.
        async def write():
            try:
                await asyncio.to_thread(self._save, job)
            except OSError:
                # Record failures even when cancellation wins at the boundary.
                job["storage_error"] = "无法保存解析记录，请检查下载目录权限和剩余磁盘空间"
                raise
        await finish_before_cancel(write())

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
            await self.messenger.text(job["group_id"], text, allowed=lambda: self._accepting
                and self.options.settings.notify_group and self.allowed(job["group_id"], job["sender"]))
        except DeliveryBlocked as exc:
            notifications[stage] = {"status": "skipped", "error": str(exc)}
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
                async with self._maintenance_lock:
                    self._active_jobs.add(job["id"])
                await self._notice(job, stage, message)
                job["finished_at"] = utcnow().isoformat()
                await self._persist(job)
            except OSError:
                job["storage_error"] = "无法保存群提示记录，请检查下载目录"
            finally:
                self._active_jobs.discard(job["id"])
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
                    await self._persist(job)
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
                        await self.messenger.send(job["group_id"], path, mode, allowed=lambda: self._accepting
                            and self.allowed(job["group_id"], job["sender"]) and self.auto_download(job["group_id"])
                            and self.options.settings.send_results)
                    except DeliveryError as exc:
                        if file.get("send_status") != "skipped":
                            file["send_status"] = "skipped" if isinstance(exc, DeliveryBlocked) else "unknown" if exc.uncertain else "failed"
                        raise
                    file["send_status"] = "sent"
                    await self._persist(job)
                    self._changed()
        except Exception as exc:
            uncertain = isinstance(exc, TimeoutError) or getattr(exc, "uncertain", False) or any(file.get("send_status") == "sending" for file in job["files"])
            sent = sum(file.get("send_status") == "sent" for file in job["files"])
            error = str(exc) if isinstance(exc, (DeliveryError, DouyinError)) else "回传中断，请先检查群消息与本地文件"
            job.update(delivery_status="unknown" if uncertain else "partial" if sent else "skipped" if isinstance(exc, DeliveryBlocked) else "failed", delivery_error=error)
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
                async with self._maintenance_lock:
                    self._active_jobs.add(job["id"])
                if not self.allowed(job["group_id"], job["sender"]):
                    raise DouyinError("群聊或成员已被限制，或解析功能已停用")
                if self.account_check is not None and not await self.account_check():
                    raise DouyinError("微信账号无法确认或已变化，已停止解析")
                job["status"] = "parsing"
                self._changed()
                await self._persist(job)
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
            except LowDiskSpace as exc:
                job.update(status="failed", error=str(exc))
            except OSError:
                job.update(status="failed", error="无法保存文件，请检查下载目录权限和剩余磁盘空间")
            except Exception:
                job.update(status="failed", error="分享数据格式不受支持，请提供原始分享链接以排查")
            finally:
                try:
                    if job["status"] == "failed":
                        await self._notice(job, "failure", f"解析失败：{job['error']}")
                    try:
                        job["finished_at"] = utcnow().isoformat()
                        await self._persist(job)
                    except OSError:
                        job["storage_error"] = "无法保存解析记录，请检查下载目录权限和剩余磁盘空间"
                finally:
                    self._active_jobs.discard(job["id"])
                    self._queue.task_done()
                    self._changed()

    async def cleanup_expired(self, *, now: datetime | None = None):
        policy = self.options.retention
        if not policy.enabled:
            return None
        async with self._maintenance_lock:
            checked = now or utcnow()
            active = self._active_jobs | self._file_leases.keys() | {
                job_id for job_id, job in self._jobs.items() if job.get("status") in ACTIVE_STATES
            }
            async def sweep():
                report = await asyncio.to_thread(cleanup_media, self.options.output_dir, policy, active, checked)
                for job_id, job in report.updated.items():
                    if job_id in self._jobs:
                        self._jobs[job_id].clear()
                        self._jobs[job_id].update(job)
                for job_id in report.removed:
                    self._jobs.pop(job_id, None)
                self.cleanup_status.update(report.summary(), last_run_at=checked.isoformat(),
                    error="部分文件无法清理，将在下次清理时重试" if report.errors else "")
                self._changed()
                return report

            # Repeated cancellation must not release file protection while the
            # deletion thread or its in-memory status update is still running.
            return await finish_before_cancel(sweep())

    async def _run_cleanup(self) -> None:
        # Catch up expired files when the computer was off at the scheduled time.
        deadline = utcnow()
        while True:
            self.cleanup_status["next_run_at"] = deadline.isoformat()
            delay = (deadline - utcnow()).total_seconds()
            if delay > 0:
                await asyncio.sleep(min(delay, 60))
                continue
            try:
                await self.cleanup_expired()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.cleanup_status["error"] = "自动清理暂时失败，将在 1 分钟后重试"
                deadline = utcnow() + timedelta(minutes=1)
            else:
                deadline = next_cleanup_at(utcnow(), self.options.retention.hour)

    async def acquire_file(self, job_id: str, filename: str) -> Path | None:
        async with self._maintenance_lock:
            path = self.file_path(job_id, filename)
            if path is not None:
                self._file_leases[job_id] = self._file_leases.get(job_id, 0) + 1
            return path

    def release_file(self, job_id: str) -> None:
        remaining = self._file_leases.get(job_id, 0) - 1
        if remaining > 0:
            self._file_leases[job_id] = remaining
        else:
            self._file_leases.pop(job_id, None)

    def is_file_cleaned(self, job_id: str, filename: str) -> bool:
        job = self._jobs.get(job_id, {})
        return any(file.get("name") == filename and (job.get("files_cleaned_at") or file.get("cleaned")) for file in job.get("files", []))

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
        await self.storage.ensure()
        current = url
        for _ in range(7):
            self._check_download_policy(group, sender)
            current = checked_url(current, media=True)
            async with self._client.stream("GET", current, headers={"Referer": REFERERS.get(platform, REFERERS["douyin"]), "Accept-Encoding": "gzip, deflate"}) as response:
                if response.is_redirect:
                    current = checked_url(urljoin(current, response.headers.get("location", "")), media=True)
                    continue
                response.raise_for_status()
                if response.status_code != 200:
                    raise DouyinError("媒体响应不完整，已停止保存")
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > limit:
                    raise DouyinError("媒体文件超过配置的大小限制，已停止下载")
                await self.storage.ensure(int(length) if length.isdigit() else 0)
                partial = folder / f"{index:03d}.part"
                size = 0
                prefix = bytearray()
                try:
                    with partial.open("wb") as output:
                        async for chunk in read_response_bytes(response, limit, size_error="媒体文件超过配置的大小限制，已停止下载", kind="媒体"):
                            self._check_download_policy(group, sender)
                            size += len(chunk)
                            if size > limit:
                                raise DouyinError("媒体文件超过配置的大小限制，已停止下载")
                            if len(prefix) < 32:
                                prefix.extend(chunk[:32 - len(prefix)])
                            await self.storage.ensure(len(chunk))
                            output.write(chunk)
                            self.storage.written(len(chunk))
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
        if not job or job.get("files_cleaned_at") or not any(file.get("name") == filename and not file.get("cleaned") for file in job.get("files", [])):
            return None
        path = (self.options.output_dir / job_id / filename).resolve()
        if not path.is_relative_to(self.options.output_dir.resolve()) or not path.is_file():
            return None
        return path
