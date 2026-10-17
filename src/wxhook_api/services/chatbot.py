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
from wxhook_api.core.chatbot_config import ChatbotOptions, save_mention_names
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin_delivery import DeliveryError, DouyinMessenger
from wxhook_api.services.groups import GroupManager, event_group, now


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
        self._recent: OrderedDict[str, dict] = OrderedDict()
        self._names_lock = asyncio.Lock()
        self._profile_names: tuple[str, ...] = ()

    def snapshot(self) -> dict:
        return {"enabled": self.options.enabled, "answer_configured": self.provider is not None,
                "identity_known": bool(self._identity and self._profile_valid), "mention_names": list(self._names),
                "configured_mention_names": list(self.options.mention_names),
                "pending": self._queue.qsize(), "sent": self.sent, "last_error": self.last_error,
                "recent_events": [dict(item) for item in self._recent.values()]}

    def _report(self, event: CallbackEvent) -> dict:
        group = event_group(event)
        report = {"group_id": group, "msgid": str(event.msgid)[:128], "received_at": now(),
                  "status": "received", "message": "已收到实时群消息"}
        self._recent[group] = report
        self._recent.move_to_end(group)
        while len(self._recent) > 100:
            self._recent.popitem(last=False)
        return report

    async def configure_mentions(self, names: list[str]) -> None:
        async with self._names_lock:
            self.options = await save_mention_names(self.options, names)
            self._names = tuple(dict.fromkeys((*self.options.mention_names, *self._profile_names)))
            self._profile_checked = 0.0

    async def _group_names(self, group: str) -> tuple[str, ...]:
        # The Hook can return a valid wxid but an empty nickname. Use only the
        # synchronized name of this exact account, never the question sender.
        if not self._identity or not self.groups:
            return self._names
        members = await self.groups.members(group)
        own = next((member for member in members if member["id"] == self._identity), None)
        name = own.get("wechat_name", "") if own else ""
        return tuple(dict.fromkeys((*self._names, name))) if name else self._names

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
            if isinstance(nickname, str) and 0 < len(nickname.strip()) <= 80:
                self._profile_names = (nickname.strip(),)
            else:
                self._profile_names = ()
            self._names = tuple(dict.fromkeys((*self.options.mention_names, *self._profile_names)))
            self.last_error = ""
            return True
        except Exception:
            self._profile_valid = False
            self.last_error = "无法确认机器人账号，请检查微信连接或本机 chatbot.local.yml"
            return False

    async def receive(self, event: CallbackEvent) -> bool:
        # Returning True also prevents our own model answer (which may contain
        # URLs) or an @ question from triggering a second automatic feature.
        group = event_group(event)
        report = self._report(event) if group else {}
        if self.own_message(event):
            report.update(status="self", message="机器人自己发送的消息不触发问答，请使用另一个微信账号提问")
            return True
        if not self._accepting:
            report.update(status="disabled", message="@问答全局开关已关闭")
            return False
        if event.type != 1:
            report.update(status="nontext", message="此消息不是文本，不触发问答")
            return False
        if not self.allowed(group, event.sender):
            report.update(status="policy", message="群功能、@问答开关或群/成员黑名单阻止了问答")
            return False
        ids, _ = mention_ids(event)
        if not ids and "@" not in event.content:
            report.update(status="no_mention", message="消息中没有 @提及，不触发问答")
            return False
        if not await self._identify() or self.own_message(event):
            report.update(status="identity", message=self.last_error or "消息来自机器人自己，不触发问答")
            return self.own_message(event)
        names = await self._group_names(group)
        report["mention_names"] = list(names)
        question = addressed_question(event, self._identity, names)
        if question is None:
            message = "未匹配到 @当前机器人，请在网页配置机器人群内显示名；明确 @其他人的消息不会触发"
            if not names and not ids:
                message = "微信接口返回的机器人昵称为空，请在网页填写机器人群内显示名后发送新的 @提问"
            report.update(status="not_addressed", message=message)
            return False
        msgid = str(event.msgid)
        if msgid not in {"", "0"}:
            key = "msg:" + (msgid if len(msgid) <= 128 else hashlib.sha256(msgid.encode()).hexdigest())
        else:
            key = "callback:" + uuid4().hex
        item = (group, key)
        if item in self._seen:
            report.update(status="duplicate", message="重复回调已忽略，同一条消息不会再次回复")
            return True
        self._seen[item] = None
        while len(self._seen) > 1000:
            self._seen.popitem(last=False)
        try:
            report.update(status="queued", message="已识别 @机器人，等待调用模型")
            self._queue.put_nowait({"group": group, "sender": event.sender, "key": key, "question": question, "report": report})
        except asyncio.QueueFull:
            self.last_error = "问答队列已满，未创建本次回复"
            report.update(status="queue_full", message=self.last_error)
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
            report = job["report"]
            try:
                if not self.allowed(job["group"], job["sender"]):
                    report.update(status="policy", message="排队期间群开关或黑名单发生变化，已停止问答")
                    continue
                claimed = await self.groups.claim_chat_reply(job["group"], job["key"])
                if not claimed:
                    report.update(status="duplicate", message="此消息已处理或已尝试发送，不会重复回复；请发送新的提问")
                    continue
                report.update(status="answering", message="正在等待模型回复")
                answer = await self._answer(job["question"])
                limit = self.options.max_reply_chars
                if len(answer) > limit:
                    answer = answer[:limit - 1] + "…"
                if not self._accepting or not self.allowed(job["group"], job["sender"]):
                    status = "skipped"
                    report.update(status="skipped", message="生成回复期间问答已被暂停，未发送")
                    continue
                # The durable claim precedes the only outbound send attempt.
                await self.groups.finish_chat_reply(job["group"], job["key"], "sending")
                if not self._accepting or not self.allowed(job["group"], job["sender"]):
                    status = "skipped"
                    report.update(status="skipped", message="发送前问答已被暂停，未发送")
                    continue
                report.update(status="sending", message="模型回复已生成，正在发送到微信群")
                attempted = True
                await self._messenger.text(job["group"], answer)
                status = "sent"
                self.sent += 1
                report.update(status="sent", message="Hook 已确认发送一条回复")
            except asyncio.CancelledError:
                status = "unknown" if attempted else "interrupted"
                raise
            except DeliveryError as exc:
                status = "unknown" if exc.uncertain else "failed"
                self.last_error = "回复发送未确认，请检查群聊；同一条消息不会自动重发"
                report.update(status=status, message=self.last_error)
            except Exception:
                status = "unknown" if attempted else "failed"
                self.last_error = "问答处理失败；同一条消息不会自动重发"
                report.update(status=status, message=self.last_error)
            finally:
                try:
                    if claimed:
                        await self.groups.finish_chat_reply(job["group"], job["key"], status)
                except Exception:
                    self.last_error = "无法保存问答状态，请检查资料库；不会补发第二条消息"
                self._queue.task_done()
