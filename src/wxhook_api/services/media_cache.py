"""Bounded local media cache keyed by platform and source URL."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from wxhook_api.services.media_cleanup import MEDIA_NAME, safe_child


CACHE_VERSION = 6
CACHE_TTL_DAYS = 7
KEY_PATTERN = re.compile(r"[a-z]+_[a-f0-9]{32}")
META_NAME = "meta.json"


class MediaCache:
    def __init__(self, root: Path, *, ttl_days: float = CACHE_TTL_DAYS):
        self.root = root
        self.ttl_days = max(0.0, float(ttl_days))

    @staticmethod
    def key(platform: str, source: str) -> str:
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
        return f"{platform}_{digest}"

    def _root(self) -> Path:
        return self.root.resolve()

    def _entry(self, key: str) -> Path | None:
        if not KEY_PATTERN.fullmatch(key):
            return None
        root = self._root()
        entry = root / key
        try:
            if not entry.is_dir() or not safe_child(entry, root, directory=True):
                return None
        except OSError:
            return None
        return entry

    def load(self, key: str) -> dict | None:
        entry = self._entry(key)
        if entry is None:
            return None
        meta_path = entry / META_NAME
        try:
            if not safe_child(meta_path, entry) or meta_path.stat().st_size > 256 * 1024:
                return None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(meta, dict) or meta.get("version") != CACHE_VERSION or meta.get("key") != key:
            return None
        try:
            created = datetime.fromisoformat(meta["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            try:
                created = datetime.fromtimestamp(meta_path.stat().st_mtime, timezone.utc)
            except OSError:
                return None
        if (datetime.now(timezone.utc) - created).total_seconds() > self.ttl_days * 86400:
            return None
        files = meta.get("files")
        if not isinstance(files, list) or not files:
            return None
        checked = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not MEDIA_NAME.fullmatch(item["name"]):
                return None
            if not isinstance(item.get("kind"), str):
                return None
            path = entry / item["name"]
            try:
                if not safe_child(path, entry) or not path.is_file():
                    return None
                checked.append({"name": item["name"], "kind": item["kind"], "size": path.stat().st_size})
            except OSError:
                return None
        meta["files"] = checked
        return meta

    def materialize(self, meta: dict, folder: Path) -> list[dict]:
        entry = self._entry(str(meta.get("key", "")))
        if entry is None:
            raise OSError("cache entry is missing")
        folder.mkdir(parents=True, exist_ok=True)
        target_root = folder.resolve()
        created = []
        files = []
        try:
            for item in meta["files"]:
                if not MEDIA_NAME.fullmatch(str(item.get("name", ""))):
                    raise OSError("cache file name is invalid")
                source = entry / item["name"]
                if not safe_child(source, entry):
                    raise OSError("cache file is unsafe")
                target = folder / item["name"]
                if target.resolve().parent != target_root:
                    raise OSError("cache target escapes the job folder")
                shutil.copy2(source, target)
                created.append(target)
                files.append({"name": item["name"], "kind": item["kind"], "size": target.stat().st_size})
            return files
        except OSError:
            for path in created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise

    def store(self, key: str, folder: Path, job: dict) -> None:
        if not KEY_PATTERN.fullmatch(key):
            raise OSError("cache key is invalid")
        root = self._root()
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / f".tmp-{uuid4().hex}"
        temporary.mkdir()
        try:
            files = []
            for item in job.get("files", []):
                name = item.get("name", "") if isinstance(item, dict) else ""
                source = folder / name
                if not MEDIA_NAME.fullmatch(name) or not source.is_file():
                    raise OSError("job media is missing")
                target = temporary / name
                shutil.copy2(source, target)
                files.append({"name": name, "kind": item.get("kind", ""), "size": target.stat().st_size})
            meta = {
                "version": CACHE_VERSION, "key": key, "platform": job.get("platform", ""),
                "source_url": job.get("source_url", ""), "item_id": job.get("item_id", ""),
                "title": job.get("title", ""), "author": job.get("author", ""), "kind": job.get("kind", ""),
                "assets": job.get("assets", []), "files": files,
                "parser_provider": job.get("parser_provider", ""), "parser_fallback": bool(job.get("parser_fallback")),
                "media_notice": job.get("media_notice", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            (temporary / META_NAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            final = root / key
            if final.exists():
                if not safe_child(final, root, directory=True):
                    raise OSError("existing cache entry is unsafe")
                shutil.rmtree(final)
            os.replace(temporary, final)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def cleanup(self) -> None:
        root = self._root()
        if not root.is_dir():
            return
        cutoff = time.time() - self.ttl_days * 86400
        for entry in root.iterdir():
            try:
                if entry.name.startswith(".tmp-"):
                    if entry.stat().st_mtime < cutoff and safe_child(entry, root, directory=True):
                        shutil.rmtree(entry)
                    continue
                if not KEY_PATTERN.fullmatch(entry.name) or not safe_child(entry, root, directory=True):
                    continue
                meta = entry / META_NAME
                modified = meta.stat().st_mtime if meta.is_file() else entry.stat().st_mtime
                if modified >= cutoff:
                    continue
                for path in entry.iterdir():
                    if safe_child(path, entry):
                        path.unlink()
                entry.rmdir()
            except OSError:
                continue
