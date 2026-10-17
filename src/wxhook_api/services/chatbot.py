from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import html
import json
import re
from typing import Callable, Protocol
from uuid import uuid4

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin_delivery import DeliveryError, DouyinMessenger
from wxhook_api.services.groups import GroupManager, event_group


class AnswerProvider(Protocol):
    async def answer(self, question: str) -> str: ...


def mention_ids(event: CallbackEvent) -> tuple[set[str], bool]:
    """Structured IDs take priority over the display-name fallback."""
    values = []
    for key in ("atlist", "at_list", "atuserlist", "at_user_list", "atUserList"):
        value = event.extra.get(key)
        if value:
            values.append(value)
    for key in ("source", "msgsource", "msg_source"):
        value = event.extra.get(key)
        if isinstance(value, str) and len(value) <= 65536:
            values.extend(re.findall(r"<atuserlist\b[^>]*>(.*?)</atuserlist>", value, re.S | re.I))
    ids = set()
    for value in values:
        if isinstance(value, str):
            value = html.unescape(value.replace("<![CDATA[", "").replace("]]>", "")).strip()
            if value.startswith("["):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            else:
                value = re.split(r"[,;\s]+", value)
        if isinstance(value, list):
            ids.update(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ids, bool(ids)


def addressed_question(event: CallbackEvent, identity: str, names: tuple[str, ...]) -> str | None:
    ids, structured = mention_ids(event)
    if structured and identity not in ids:
        return None
    text = event.content
    prefix = event.sender + ":\n"
    if text.startswith(prefix):
        text = text[len(prefix):]
    labels = tuple(dict.fromkeys((*names, identity)))
    pattern = re.compile(r"@(?:" + "|".join(re.escape(name) for name in sorted(labels, key=len, reverse=True) if name) + r")(?=[\s\u2005]|$)")
    matched = pattern.search(text) if any(labels) else None
    if not structured and not matched:
        return None
    return pattern.sub("", text).strip() if any(labels) else text.strip()


class MentionChatService:
    """A single answer and a single send attempt per claimed incoming message."""

    def __init__(self, options: ChatbotOptions, *, provider: AnswerProvider | None = None):
        self.options = options
        self.provider = provider
        self.groups: GroupManager | None = None
        self.default_group_enabled: Callable[[str, str], bool] = lambda group, sender: False
        self._client: HookClient | None = None
        self._messenger: DouyinMessenger | None = None
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        self._worker: asyncio.Task | None = None
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._identity = options.bot_wxid
        self._names = options.mention_names
        self._profile_checked = 0.0
        self._profile_valid = bool(options.bot_wxid and options.mention_names)
        self._accepting = False
        self.last_error = ""
        self.sent = 0

    def snapshot(self) -> dict:
        return {"enabled": self.options.enabled, "answer_configured": self.provider is not None,
                "identity_known": bool(self._identity), "pending": self._queue.qsize(), "sent": self.sent, "last_error": self.last_error}

    def allowed(self, group: str, sender: str) -> bool:
        return bool(group and self.groups and self.groups.allowed(group, sender)
                    and self.groups.feature(group, "chat_enabled", self.default_group_enabled(group, sender)))

    def own_message(self, event: CallbackEvent) -> bool:
        flag = event.extra.get("is_self")
        return bool(self._identity and event.sender == self._identity) or flag is True or flag == 1 or flag in ("1", "true")

    async def start(self, client: HookClient) -> None:
        self._client = client
        self._messenger = DouyinMessenger(client)
        self._accepting = self.options.enabled
        if self._accepting:
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._accepting = False
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    async def _identify(self) -> bool:
        if self.options.bot_wxid and self.options.mention_names:
            return True
        now = asyncio.get_running_loop().time()
        if self._profile_checked and now - self._profile_checked < 30:
            return self._profile_valid
        self._profile_checked = now
        try:
            async with asyncio.timeout(5):
                response = await self._client.get_self_profile()
            profile = response.data
            if isinstance(profile, dict) and isinstance(profile.get("data"), dict):
                profile = profile["data"]
            identity = profile.get("wxid") if isinstance(profile, dict) else None
            if response.status_code != 200 or not isinstance(identity, str) or not identity or identity.endswith("@chatroom"):
                raise ValueError
            if self.options.bot_wxid and identity != self.options.bot_wxid:
                self._profile_valid = False
                self.last_error = "配置的机器人 wxid 与当前登录账号不一致"
                return False
            await self.groups.claim_account(identity)
            self._identity = identity
            self._profile_valid = True
            nickname = profile.get("nickname")
            if isinstance(nickname, str) and 0 < len(nickname) <= 80:
                self._names = tuple(dict.fromkeys((*self.options.mention_names, nickname)))
            self.last_error = ""
            return True
        except Exception:
            self._profile_valid = False
            self.last_error = "无法确认机器人账号，请检查微信连接或本机 chatbot.local.yml"
            return False

    async def receive(self, event: CallbackEvent) -> bool:
        # Returning True also prevents our own model answer (which may contain
        # URLs) or an @ question from triggering a second automatic feature.
        if self.own_message(event):
            return True
        group = event_group(event)
        if not self._accepting or event.type != 1 or not self.allowed(group, event.sender):
            return False
        ids, _ = mention_ids(event)
        if not ids and "@" not in event.content:
            return False
        if not await self._identify() or self.own_message(event):
            return self.own_message(event)
        question = addressed_question(event, self._identity, self._names)
        if question is None:
            return False
        msgid = str(event.msgid)
        if msgid not in {"", "0"}:
            key = "msg:" + (msgid if len(msgid) <= 128 else hashlib.sha256(msgid.encode()).hexdigest())
        else:
            key = "callback:" + uuid4().hex
        item = (group, key)
        if item in self._seen:
            return True
        self._seen[item] = None
        while len(self._seen) > 1000:
            self._seen.popitem(last=False)
        try:
            self._queue.put_nowait({"group": group, "sender": event.sender, "key": key, "question": question})
        except asyncio.QueueFull:
            self.last_error = "问答队列已满，未创建本次回复"
        return True

    async def _answer(self, question: str) -> str:
        if not question:
            return "我在，请在 @ 后写下你的问题。"
        if len(question) > 8000:
            return "问题太长了，请缩短后重新 @ 我。"
        if self.provider is None:
            return "问答接口尚未配置，请联系管理员。"
        try:
            async with asyncio.timeout(60):
                answer = await self.provider.answer(question)
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError
            return answer.strip()
        except Exception:
            self.last_error = "问答接口返回失败或超时，请检查接口配置"
            return "暂时无法回答，请稍后重新 @ 我。"

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            claimed = False
            status = "failed"
            attempted = False
            try:
                if not self.allowed(job["group"], job["sender"]):
                    continue
                claimed = await self.groups.claim_chat_reply(job["group"], job["key"])
                if not claimed:
                    continue
                answer = await self._answer(job["question"])
                limit = self.options.max_reply_chars
                if len(answer) > limit:
                    answer = answer[:limit - 1] + "…"
                if not self._accepting or not self.allowed(job["group"], job["sender"]):
                    status = "skipped"
                    continue
                # The durable claim precedes the only outbound send attempt.
                await self.groups.finish_chat_reply(job["group"], job["key"], "sending")
                if not self._accepting or not self.allowed(job["group"], job["sender"]):
                    status = "skipped"
                    continue
                attempted = True
                await self._messenger.text(job["group"], answer)
                status = "sent"
                self.sent += 1
            except asyncio.CancelledError:
                status = "unknown" if attempted else "interrupted"
                raise
            except DeliveryError as exc:
                status = "unknown" if exc.uncertain else "failed"
                self.last_error = "回复发送未确认，请检查群聊；同一条消息不会自动重发"
            except Exception:
                status = "unknown" if attempted else "failed"
                self.last_error = "问答处理失败；同一条消息不会自动重发"
            finally:
                try:
                    if claimed:
                        await self.groups.finish_chat_reply(job["group"], job["key"], status)
                except Exception:
                    self.last_error = "无法保存问答状态，请检查资料库；不会补发第二条消息"
                self._queue.task_done()
