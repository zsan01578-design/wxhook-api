from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat

from wxhook_api.core.douyin_config import MediaRetention


CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")
JOB_ID = re.compile(r"[a-f0-9]{32}")
MEDIA_NAME = re.compile(r"(?:[0-9]{3}\.(?:mp4|jpg|jpeg|png|webp|gif|avif|part)|send-[0-9]{3}\.png(?:\.part)?)")
ACTIVE_STATES = {"queued", "parsing", "downloading", "sending"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def next_cleanup_at(now: datetime, hour: int = 3) -> datetime:
    local = now.astimezone(CHINA)
    deadline = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return deadline if deadline > local else deadline + timedelta(days=1)


def timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def regular(path: Path, *, directory: bool = False) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        return False  # Includes Windows junctions and other reparse points.
    return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def safe_child(path: Path, parent: Path, *, directory: bool = False) -> bool:
    return regular(path, directory=directory) and path.resolve().parent == parent


@dataclass
class CleanupReport:
    deleted_files: int = 0
    deleted_records: int = 0
    bytes_freed: int = 0
    skipped_active: int = 0
    skipped_unsafe: int = 0
    errors: int = 0
    updated: dict[str, dict] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)

    def summary(self) -> dict:
        return {key: getattr(self, key) for key in ("deleted_files", "deleted_records", "bytes_freed", "skipped_active", "skipped_unsafe", "errors")}


def cleanup_media(output: Path, policy: MediaRetention, active: set[str], now: datetime) -> CleanupReport:
    """Delete only owned, individually validated files; never recursively delete a directory."""
    report = CleanupReport()
    if not policy.enabled or not output.is_dir():
        return report
    root = output.resolve()
    for folder in root.iterdir():
        if not JOB_ID.fullmatch(folder.name):
            continue
        if folder.name in active:
            report.skipped_active += 1
            continue
        try:
            if not safe_child(folder, root, directory=True):
                report.skipped_unsafe += 1
                continue
            record = folder / "result.json"
            if not safe_child(record, folder) or record.stat().st_size > 3 * 1024 * 1024:
                report.skipped_unsafe += 1
                continue
            temporary = folder / "result.json.tmp"
            if os.path.lexists(temporary) and not safe_child(temporary, folder):
                report.skipped_unsafe += 1
                continue
            job = json.loads(record.read_text(encoding="utf-8"))
            if not isinstance(job, dict) or job.get("id") != folder.name or not isinstance(job.get("files", []), list):
                report.skipped_unsafe += 1
                continue
            files = [path for path in folder.iterdir() if MEDIA_NAME.fullmatch(path.name)]
            if any(not safe_child(path, folder) for path in files):
                report.skipped_unsafe += 1
                continue
            finished = timestamp(job.get("finished_at"))
            if finished is None:
                # Old versions did not store a finish time. Be conservative and freeze
                # the latest observed write before cleanup rewrites the metadata.
                finished = max(filter(None, [timestamp(job.get("created_at")), datetime.fromtimestamp(record.stat().st_mtime, timezone.utc),
                                            *[datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) for path in files]]))
            newest_media = max((datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) for path in files), default=finished)
            age = now - max(finished, newest_media)
            sent = job.get("delivery_status") == "sent" and all(file.get("send_status") == "sent" for file in job.get("files", []))
            retention = timedelta(hours=policy.sent_hours if sent else policy.other_hours)
            if age < retention:
                continue
            changed = False
            if timestamp(job.get("finished_at")) is None:
                job["finished_at"] = finished.isoformat()
                changed = True
            if job.get("status") in ACTIVE_STATES:
                job.update(status="interrupted", error="上次服务中断，未完成的媒体已到保留期限")
                if job.get("delivery_status") == "sending":
                    job.update(delivery_status="unknown", delivery_error="上次回传中断，部分文件可能已发出；不会自动重发")
                changed = True
            error = False
            for path in files:
                try:
                    # Validate both absolute targets again, immediately before each unlink.
                    if not safe_child(folder, root, directory=True) or not safe_child(path, folder):
                        report.skipped_unsafe += 1
                        error = True
                        continue
                    size = path.stat().st_size
                    path.unlink()
                    report.deleted_files += 1
                    report.bytes_freed += size
                    changed = True
                except OSError:
                    report.errors += 1
                    error = True
            for file in job.get("files", []):
                name = file.get("name", "") if isinstance(file, dict) else ""
                if not isinstance(name, str) or not MEDIA_NAME.fullmatch(name):
                    error = True
                    continue
                if not (folder / name).exists() and not file.get("cleaned"):
                    file["cleaned"] = True
                    changed = True
            if error:
                job["cleanup_error"] = "部分文件被占用或无法安全清理，下次清理时重试"
                changed = True
            else:
                if job.pop("cleanup_error", None):
                    changed = True
                if (files or job.get("files")) and not job.get("files_cleaned_at"):
                    job["files_cleaned_at"] = now.isoformat()
                    changed = True
            if not error and now - finished >= timedelta(days=policy.record_days):
                if not safe_child(folder, root, directory=True) or not safe_child(record, folder):
                    report.skipped_unsafe += 1
                    continue
                # The record expires only after all managed media has been handled.
                record.unlink()
                report.deleted_records += 1
                report.removed.add(folder.name)
                if os.path.lexists(temporary) and safe_child(temporary, folder):
                    temporary.unlink()
                if next(folder.iterdir(), None) is None:
                    folder.rmdir()
                continue
            if changed:
                if os.path.lexists(temporary) and not safe_child(temporary, folder):
                    report.skipped_unsafe += 1
                    continue
                if not safe_child(folder, root, directory=True):
                    report.skipped_unsafe += 1
                    continue
                try:
                    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
                    temporary.replace(record)
                except OSError:
                    report.errors += 1
                    job["cleanup_error"] = "清理结果无法写入磁盘，下次启动或清理时重试"
                report.updated[folder.name] = job
        except FileNotFoundError:
            report.skipped_unsafe += 1
        except (OSError, ValueError, TypeError, AttributeError):
            report.errors += 1
    return report
