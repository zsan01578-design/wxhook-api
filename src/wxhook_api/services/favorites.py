"""Explicit, group-scoped link collections with durable local media copies."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from wxhook_api.core.ai_safety import DATA_SYSTEM, ai_permission, ai_scope
from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.services.douyin import media_extension
from wxhook_api.services.douyin_api import DouyinAPIParser
from wxhook_api.services.douyin_parser import (DouyinError, DouyinParser, LINK_PATTERN, USER_AGENT,
                                             checked_url, item_identity, read_response_bytes, request_error)
from wxhook_api.services.groups import now
from wxhook_api.services.media_cleanup import safe_child
from wxhook_api.services.media_parser import MediaParserClient
from wxhook_api.services.parse_video import ParseVideoParser, REFERERS, source_platform
from wxhook_api.services.storage_guard import StorageGuard
from wxhook_api.services.word_cloud import frequent_words


SAVE = re.compile(r"^(?:请)?(?:帮我)?收藏(?:一下)?(?:[\s：:]|$)")
FIND = re.compile(r"^(?:搜索收藏|查找收藏|找收藏|搜收藏)(?:[\s：:]|$)")
NATURAL_FIND = re.compile(r"(?:上次|之前|以前|收藏过|收藏的|收藏里).{0,160}(?:在哪|哪里|找|搜|有没有)")
LIST = {"收藏夹", "群收藏", "本群收藏", "我的收藏"}
FILE_NAME = re.compile(r"\d{3}\.(?:mp4|jpg|png|webp|gif|avif)")


def is_favorite_command(text: str) -> bool:
    text = text.strip().rstrip("。！!？?")
    return text in LIST or bool(SAVE.match(text) or FIND.match(text) or NATURAL_FIND.search(text))


def bookmark_url(value: str) -> str:
    """Bookmarks are never fetched unless the platform parser allows the host."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (len(value) > 8192 or any(ord(c) < 32 for c in value) or parsed.scheme not in {"http", "https"}
                or not host or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
                or "." not in host or host.lower().endswith((".localhost", ".local"))):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError
    except ValueError:
        raise ValueError("请提供不含账号密码的公开 HTTP/HTTPS 分享链接。") from None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def share_links(text: str) -> list[str]:
    text = html.unescape(html.unescape(text.replace(r"\/", "/")))
    links = []
    for match in LINK_PATTERN.finditer(text):
        try:
            url = bookmark_url(match.group().rstrip(".,;!?)]。"))
        except ValueError:
            continue
        if url not in links:
            links.append(url)
    return links[:6]


def source_key(url: str) -> str:
    try:
        identity = item_identity(url)
    except DouyinError:
        identity = None
    return "douyin:" + identity[1] if identity else hashlib.sha256(url.encode()).hexdigest()


def clean_tags(tags) -> list[str]:
    if not isinstance(tags, list) or len(tags) > 12 or any(not isinstance(tag, str) or len(tag) > 24 for tag in tags):
        raise ValueError("最多填写 12 个标签，每个不超过 24 字。")
    return list(dict.fromkeys(tag.strip().lstrip("#") for tag in tags if tag.strip().lstrip("#")))


def auto_tags(text: str) -> list[str]:
    tags = re.findall(r"#([^\s#，。！？,!?]{1,24})", text)
    topics = {"美食": r"厨师|做菜|食谱|红烧|烹饪|教程.*菜", "旅行": r"旅游|旅行|景点|攻略|酒店",
              "技术": r"代码|编程|Python|GitHub|开发|教程", "运动": r"健身|跑步|运动|训练"}
    tags.extend(tag for tag, pattern in topics.items() if re.search(pattern, text, re.I))
    tags.extend(word["text"] for word in frequent_words([text], limit=5))
    return list(dict.fromkeys(tag[:24] for tag in tags))[:8]


def query_terms(query: str) -> list[str]:
    text = query.casefold()
    for filler in ("帮我", "找一下", "上次", "之前", "以前", "那个", "收藏过", "收藏的", "收藏里", "有没有", "在哪里", "在哪", "哪里", "教程", "链接"):
        text = text.replace(filler, " ")
    chunks = re.findall(r"[\u3400-\u9fff]{2,}|[a-z0-9+#.-]{2,}", text)
    terms = list(chunks)
    for chunk in chunks:
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            terms.extend(chunk[i:i+size] for size in (3, 2) for i in range(len(chunk)-size+1))
    return list(dict.fromkeys(terms))[:60]


class FavoritesService:
    def __init__(self, tools, media):
        self.tools, self.groups, self.models, self.chatbot = tools, tools.groups, tools.models, tools.chatbot
        self.media = media
        self.root = Path(os.getenv("WXHOOK_FAVORITES_DIR") or self.groups.path.parent / "favorites").resolve()
        self.storage = StorageGuard(self.root)
        self._slots = asyncio.Semaphore(2)
        self._active: set[str] = set()
        self._leases: dict[str, int] = {}

    async def start(self):
        await self.tools.db(lambda db: db.execute("UPDATE group_favorites SET status='partial',error='上次收藏被中断，原链接已保留，可重试保存媒体' WHERE status='saving'"))

    def check(self, group: str, sender: str = ""):
        if not self.groups.allowed(group, sender):
            raise ValueError("此群或成员已暂停使用，收藏操作已停止。")

    @staticmethod
    def record(row) -> dict:
        result = dict(row)
        for key in ("tags", "assets", "files"):
            result[key] = json.loads(result[key])
        return result

    async def get(self, group, item_id):
        row = await self.tools.read(lambda db: db.execute("SELECT * FROM group_favorites WHERE group_id=? AND id=?", (group, item_id)).fetchone())
        return self.record(row) if row else None

    async def recent_source(self, group, sender, key):
        """Only use this sender's preceding live share, never another member's."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        msgid = key.removeprefix("msg:") if key.startswith("msg:") else ""
        def read(db):
            boundary = db.execute("SELECT id FROM chat_messages WHERE group_id=? AND msgid=? AND sender=? ORDER BY id DESC LIMIT 1", (group, msgid, sender)).fetchone() if msgid else None
            rows = db.execute("""SELECT content FROM chat_messages WHERE group_id=? AND sender=? AND is_self=0
                AND source='callback' AND received_at>=? AND id<? ORDER BY id DESC LIMIT 20""",
                (group, sender, cutoff, boundary[0] if boundary else 2**63-1)).fetchall()
            return next((row[0] for row in rows if share_links(row[0])), "")
        return await self.tools.read(read)

    async def search(self, group, query="", *, page=1, size=20, semantic=False, sender=""):
        self.check(group, sender)
        terms = query_terms(query[:300])
        mode = "keyword"
        if semantic and query and self.models.settings:
            try:
                with ai_scope(group, sender), ai_permission(lambda: self.groups.allowed(group, sender)):
                    async with asyncio.timeout(20):
                        raw = await self.models.answer(query[:300], group=group, system=DATA_SYSTEM +
                            "将搜索问题提取为最多8个关键词，可包含常用同义词。只返回JSON字符串数组。不回答问题，不生成网址。")
                expanded = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
                if isinstance(expanded, list):
                    terms.extend(word.casefold() for word in expanded[:8] if isinstance(word, str) and 2 <= len(word) <= 24)
                    mode = "semantic"
            except Exception:
                mode = "keyword"
        self.check(group, sender)
        terms = list(dict.fromkeys(terms))[:68]
        # Each group has a hard capacity of 1,000 cards; never scan other groups.
        def read(db):
            return [dict(row) for row in db.execute("""SELECT id,title,author,platform,kind,tags,notes,source_text,source_url,
                files,status,error,created_by,created_at,updated_at FROM group_favorites
                WHERE group_id=? ORDER BY created_at DESC,id DESC LIMIT 1000""", (group,))]
        rows = await self.tools.read(read)
        ranked = []
        for row in rows:
            fields = [(row["title"].casefold(), 5), (row["tags"].casefold(), 4), (row["notes"].casefold(), 3),
                      (row["source_text"].casefold(), 1), (row["author"].casefold(), 1)]
            score = sum(min(len(term), 12) * weight for term in terms for text, weight in fields if term in text)
            if not query or score:
                item = dict(row)
                item.update(group_id=group, tags=json.loads(item["tags"]), files=json.loads(item["files"]))
                ranked.append((score, item))
        ranked.sort(key=lambda pair: -pair[0])
        total = len(ranked)
        offset = (page-1)*size
        return {"group_id": group, "items": [item for _, item in ranked[offset:offset+size]], "total": total,
                "page": page, "size": size, "has_more": offset+size < total, "search_mode": mode}

    async def collect(self, group, text, *, sender="", title="", tags=None, notes="", retry_id=None):
        self.check(group, sender)
        links = share_links(text)
        if len(links) != 1:
            raise ValueError("请每次收藏一个分享链接；可发送“收藏＋链接”。")
        url = links[0]
        initial_title = title.strip()[:220]
        if not initial_title:
            match = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", text, re.S)
            initial_title = html.unescape(match[1])[:220] if match else LINK_PATTERN.sub("", re.sub(r"<[^>]*>", "", text)).strip()[:220]
            initial_title = SAVE.sub("", initial_title).strip() or urlsplit(url).hostname
        tags = clean_tags(tags or [])
        item_id = retry_id or uuid4().hex
        if item_id in self._active or self._leases.get(item_id):
            raise ValueError("这条收藏正在保存，请稍后查看。")
        self._active.add(item_id)
        try:
            try:
                await asyncio.wait_for(self._slots.acquire(), 5)
            except TimeoutError:
                raise ValueError("收藏任务较多，请稍后再试。") from None
            try:
                return await self._collect(group, url, text[:4000], sender, initial_title, tags, notes[:1000], item_id, retry_id, bool(title.strip()))
            finally:
                self._slots.release()
        except asyncio.CancelledError:
            # Cancellation may arrive after the reservation committed but
            # before _collect received it. Do not leave a permanently busy card.
            await finish_before_cancel(self.tools.db(lambda db: db.execute(
                "UPDATE group_favorites SET status='partial',error='收藏保存被中断，原链接已保留，可重试保存媒体' WHERE group_id=? AND id=? AND status='saving'",
                (group,item_id))))
            raise
        finally:
            self._active.discard(item_id)

    async def _collect(self, group, url, text, sender, title, tags, notes, item_id, retry_id, custom_title):
        stamp = now()
        def reserve(db):
            self.check(group, sender)
            if retry_id:
                existing = db.execute("SELECT * FROM group_favorites WHERE group_id=? AND id=?", (group, retry_id)).fetchone()
                if not existing:
                    raise ValueError("收藏不存在。")
                db.execute("UPDATE group_favorites SET status='saving',error='',updated_at=? WHERE id=?", (stamp, item_id))
                return self.record(existing), False
            existing = db.execute("SELECT * FROM group_favorites WHERE group_id=? AND source_key=?", (group, source_key(url))).fetchone()
            if existing:
                return self.record(existing), True
            if db.execute("SELECT count(*) FROM group_favorites WHERE group_id=?", (group,)).fetchone()[0] >= 1000:
                raise ValueError("本群收藏已达 1000 条，请先删除不需要的收藏。")
            db.execute("""INSERT INTO group_favorites(id,group_id,source_key,source_url,source_text,title,tags,notes,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (item_id,group,source_key(url),url,text,title,json.dumps(tags),notes,sender,stamp,stamp))
            return None, False
        previous, duplicate = await self.tools.db(reserve)
        if duplicate:
            return {"item": previous, "duplicate": True}
        card = previous or dict(id=item_id,group_id=group,source_url=url,source_text=text,title=title,author="",platform="link",kind="link",
                                tags=tags,notes=notes,files=[],assets=[],created_by=sender,created_at=stamp)
        card.update(status="saved", error="")
        try:
            try:
                platform = source_platform(url)
            except DouyinError:
                platform = "link"
            if platform != "link":
                card["platform"] = platform
                async with asyncio.timeout(180), httpx.AsyncClient(transport=self.media._transport, trust_env=False,
                        follow_redirects=False, timeout=httpx.Timeout(20, connect=8), headers={"User-Agent": USER_AGENT},
                        proxy=os.getenv("PARSE_VIDEO_PROXY") or None) as client:
                    self.check(group, sender)
                    config = self.media.options.settings
                    if config.parser_provider == "media_parser":
                        parsed = await MediaParserClient(config.media_parser_url,
                                                         transport=self.media._transport).parse(url)
                    else:
                        try:
                            if platform == "douyin" and config.parser_provider == "builtin":
                                parsed = await DouyinParser(client).parse(url)
                            elif platform == "douyin" and config.parser_provider == "api":
                                parsed = await DouyinAPIParser(client, config.parser_api_url).parse(url)
                            else:
                                try:
                                    parsed = await ParseVideoParser(transport=self.media._transport).parse(url)
                                except (DouyinError, httpx.HTTPError):
                                    if platform != "douyin" or not config.parser_api_url:
                                        raise
                                    parsed = await DouyinAPIParser(client, config.parser_api_url).parse(url)
                        except (DouyinError, httpx.HTTPError) as exc:
                            primary_error = exc if isinstance(exc, DouyinError) else request_error(exc)
                            if not config.media_parser_url:
                                if primary_error is exc:
                                    raise
                                raise primary_error from exc
                            try:
                                parsed = await MediaParserClient(config.media_parser_url,
                                                                 transport=self.media._transport).parse(url)
                            except (DouyinError, httpx.HTTPError) as fallback_exc:
                                fallback_error = (fallback_exc if isinstance(fallback_exc, DouyinError)
                                                  else request_error(fallback_exc))
                                raise DouyinError(
                                    f"{str(primary_error)[:300]}；media-parser 兜底也失败：{str(fallback_error)[:200]}"
                                ) from fallback_exc
                    card.update(title=card["title"] if custom_title or retry_id else parsed.title[:220],
                                source_text=(parsed.title + "\n\n" + text)[:4000],author=parsed.author,
                                platform=platform,kind=parsed.kind,assets=parsed.assets)
                    folder = self._folder(item_id)
                    folder.mkdir(parents=True, exist_ok=True)
                    # Retry reuses already committed files; download any missing entries.
                    files = {file["name"].split(".")[0]: file for file in card["files"] if safe_child(folder / file["name"], folder)}
                    card["files"] = list(files.values())
                    remaining = 500*1024*1024 - sum(file["size"] for file in files.values())
                    for index, asset in enumerate(parsed.assets, 1):
                        self.check(group, sender)
                        if f"{index:03d}" in files:
                            continue
                        file = await self._download(client, asset, folder, index, remaining, platform, group, sender)
                        card["files"].append(file)
                        remaining -= file["size"]
        except asyncio.CancelledError:
            card.update(status="partial", error="收藏保存被中断，原链接已保留，可重试保存媒体")
            raise
        except (DouyinError, httpx.HTTPError, TimeoutError):
            card.update(status="partial", error="媒体解析或下载失败，原链接已收藏，可重试保存媒体")
        except (OSError, ValueError):
            card.update(status="partial", error="媒体未完整保存，请检查群权限、磁盘空间和目录权限后重试")
        except Exception:
            card.update(status="partial", error="媒体数据暂不支持，原链接已收藏，可重试保存媒体")
        finally:
            card["tags"] = card["tags"] or auto_tags(card["title"] + " " + card["notes"])
            card["updated_at"] = now()
            async def persist():
                await self.tools.db(lambda db: db.execute("""UPDATE group_favorites SET title=?,source_text=?,author=?,platform=?,kind=?,tags=?,
                    assets=?,files=?,status=?,error=?,updated_at=? WHERE group_id=? AND id=?""",
                    (card["title"],card["source_text"],card["author"],card["platform"],card["kind"],json.dumps(card["tags"],ensure_ascii=False),
                     json.dumps(card["assets"]),json.dumps(card["files"]),card["status"],card["error"],card["updated_at"],group,item_id)))
            await finish_before_cancel(persist())
        return {"item": card, "duplicate": False}

    def _folder(self, item_id):
        if not re.fullmatch(r"[a-f0-9]{32}", item_id):
            raise ValueError("收藏 ID 无效。")
        folder = self.root / item_id
        if folder.resolve().parent != self.root or folder.is_symlink():
            raise ValueError("收藏目录无效。")
        return folder

    async def _download(self, client, asset, folder, index, remaining, platform, group, sender):
        limit = min(self.media.options.max_file_bytes, remaining)
        if limit <= 0:
            raise DouyinError("单条收藏超过 500 MB")
        for url in asset["urls"]:
            current = url
            try:
                for _ in range(7):
                    self.check(group, sender)
                    current = checked_url(current, media=True)
                    await self.storage.ensure()
                    async with client.stream("GET", current, headers={"Referer": REFERERS[platform], "Accept-Encoding": "gzip, deflate"}) as response:
                        if response.is_redirect:
                            current = checked_url(urljoin(current, response.headers.get("location", "")), media=True)
                            continue
                        response.raise_for_status()
                        if response.status_code != 200:
                            raise DouyinError("媒体响应不完整")
                        length = response.headers.get("content-length", "")
                        if length.isdigit() and int(length) > limit:
                            raise DouyinError("媒体文件过大")
                        await self.storage.ensure(int(length) if length.isdigit() else 0)
                        partial = folder / f"{index:03d}.part"
                        size, prefix = 0, bytearray()
                        try:
                            with partial.open("wb") as output:
                                async for chunk in read_response_bytes(response, limit, size_error="收藏媒体文件过大", kind="媒体"):
                                    self.check(group, sender)
                                    size += len(chunk)
                                    prefix.extend(chunk[:max(0,32-len(prefix))])
                                    await self.storage.ensure(len(chunk))
                                    output.write(chunk)
                                    self.storage.written(len(chunk))
                            extension = media_extension(bytes(prefix), asset["kind"])
                            if length.isdigit() and size != int(length) and not response.headers.get("content-encoding"):
                                raise DouyinError("媒体下载不完整")
                            name = f"{index:03d}{extension}"
                            partial.replace(folder / name)
                            return {"name":name,"kind":asset["kind"],"size":size}
                        finally:
                            partial.unlink(missing_ok=True)
            except (DouyinError, httpx.HTTPError):
                continue
        raise DouyinError("收藏媒体下载失败")

    async def update(self, group, item_id, title, tags, notes):
        self.check(group)
        if item_id in self._active:
            raise ValueError("收藏正在保存，请稍后编辑。")
        return bool(await self.tools.db(lambda db: db.execute("UPDATE group_favorites SET title=?,tags=?,notes=?,updated_at=? WHERE group_id=? AND id=?",
                         (title,json.dumps(clean_tags(tags),ensure_ascii=False),notes,now(),group,item_id)).rowcount))

    async def delete(self, group, item_id):
        return await finish_before_cancel(self._delete(group, item_id))

    async def _delete(self, group, item_id):
        self.check(group)
        if item_id in self._active or self._leases.get(item_id):
            raise ValueError("收藏正在保存或读取，请稍后再删除。")
        self._active.add(item_id)
        try:
            card = await self.get(group, item_id)
            if not card:
                return False
            folder = self._folder(item_id)
            if folder.is_dir():
                # Only this validated direct child is touched; no recursive deletes.
                for path in folder.iterdir():
                    if re.fullmatch(r"\d{3}\.(?:part|mp4|jpg|png|webp|gif|avif)", path.name) and safe_child(path, folder):
                        path.unlink()
                if not any(folder.iterdir()):
                    folder.rmdir()
            await self.tools.db(lambda db: db.execute("DELETE FROM group_favorites WHERE group_id=? AND id=?", (group, item_id)))
            return True
        finally:
            self._active.discard(item_id)

    async def acquire_file(self, group, item_id, name):
        self.check(group)
        if item_id in self._active or not FILE_NAME.fullmatch(name):
            return None
        card = await self.get(group, item_id)
        if not card or item_id in self._active or not any(file["name"] == name for file in card["files"]):
            return None
        folder = self._folder(item_id)
        path = folder / name
        if not safe_child(path, folder):
            return None
        self._leases[item_id] = self._leases.get(item_id, 0)+1
        return path

    def release_file(self, item_id):
        self._leases[item_id] = max(0, self._leases.get(item_id, 0)-1)
        if not self._leases[item_id]:
            del self._leases[item_id]

    async def chat_command(self, question, group, sender, key):
        if not is_favorite_command(question):
            return None
        command = question.strip().rstrip("。！!？?")
        if SAVE.match(command):
            text = SAVE.sub("", command).strip()
            if not share_links(text):
                text = await self.recent_source(group, sender, key)
            if not text:
                return "请先发送分享链接，再在 10 分钟内说“收藏”；也可以直接发送“收藏＋链接”。"
            try:
                result = await self.collect(group, text, sender=sender)
            except ValueError as exc:
                return str(exc)
            item = result["item"]
            label = "本群已收藏" if result["duplicate"] else "已加入本群收藏"
            text = f"{label}：{item['title']}\n标签：{'、'.join(item['tags']) or '未设置'}\n本地媒体：{len(item['files'])} 个"
            if item["error"]:
                text += "\n"+item["error"]
            return text + "\n可以说“搜索收藏：关键词”查找。"
        query = "" if command in LIST else FIND.sub("", command).strip()
        result = await self.search(group, query, size=5, semantic=bool(query), sender=sender)
        if not result["items"]:
            return "本群暂无收藏。发送“收藏＋链接”即可添加。" if not query else "本群没有找到相关收藏，可换个关键词试试。"
        lines = [f"本群收藏（找到 {result['total']} 条，展示前 {len(result['items'])} 条）："]
        for index, item in enumerate(result["items"], 1):
            lines.append(f"{index}. {item['title'][:160]}\n{item['source_url']}")
        return "\n".join(lines)
