from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import re

from wxhook_api.services.chat_model import ChatModelConfiguration, ModelError
from wxhook_api.services.groups import GroupManager
from wxhook_api.core.ai_safety import DATA_SYSTEM, bounded_records, ai_permission


CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")
SUMMARY_REQUEST = re.compile(r"^(?:今天|今日)?(?:聊天)?总结[。！!？?]?$")


def local_date() -> str:
    return datetime.now(CHINA).date().isoformat()


def is_summary_request(question: str) -> bool:
    return bool(SUMMARY_REQUEST.fullmatch(re.sub(r"\s+", "", question or "")))


class ChatSummaryService:
    def __init__(self, groups: GroupManager, models: ChatModelConfiguration, bot_wxid: str = ""):
        self.groups = groups
        self.models = models
        self.bot_wxid = bot_wxid
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.last_run_date = ""
        self.last_error = ""
        self._retry_after = {}

    def automatic_allowed(self, group: str) -> bool:
        bot = self.models.chatbot
        return bool(bot.options.enabled and bot.allowed(group, "")
                    and self.groups.feature(group, "summary_enabled", False))

    async def get(self, group: str, *, force: bool = False, summary_date: str | None = None, automatic: bool = False) -> dict:
        day = summary_date or local_date()
        async with self._lock:
            if not self.groups.allowed(group) or (automatic and not self.automatic_allowed(group)):
                raise ModelError("此群的 AI 总结已暂停。")
            cached = None
            if not force:
                cached = await self.groups.get_summary(group, day)
                if cached and cached["message_count"] > 0:
                    return cached
            messages = await self.groups.summary_messages(group, day, bot_wxid=self.bot_wxid)
            if not messages:
                if cached:
                    return cached
                return await self.groups.save_summary(group, day, "今天暂无可总结的成员消息。", 0)
            if not self.models.settings:
                raise ModelError("AI 模型尚未配置，无法生成群聊总结。")
            records = [{"time": datetime.fromtimestamp(item['timestamp'], CHINA).strftime('%H:%M'),
                        "sender": item['sender'][:128], "content": str(item['content'])[:1200]} for item in messages]
            prompt, sampled = bounded_records(records)
            system = DATA_SYSTEM + "请总结这一天的微信群聊天记录。输出中文，控制在 300 字以内，包含主要话题、重要结论、待办或未解决问题；没有对应内容时省略该项。"
            if not self.groups.allowed(group) or (automatic and not self.automatic_allowed(group)):
                raise ModelError("此群的 AI 总结已暂停。")
            with ai_permission(lambda: self.groups.allowed(group) and (not automatic or self.automatic_allowed(group))):
                summary = (await self.models.answer(prompt, system=system, group=group)).strip()
            if not summary:
                raise ModelError("模型未返回有效的群聊总结。")
            return await self.groups.save_summary(group, day, summary[:4000], sampled)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                current = datetime.now(CHINA)
                if current.hour >= 18:
                    day = current.date().isoformat()
                    self.last_error = ""
                    complete = True
                    for group in await self.groups.summary_group_ids():
                        if not self.automatic_allowed(group) or current.timestamp() < self._retry_after.get(group, 0):
                            continue
                        try:
                            await self.get(group, summary_date=day, automatic=True)
                        except Exception as exc:
                            complete = False
                            self._retry_after[group] = current.timestamp() + 900
                            self.last_error = str(exc) if isinstance(exc, ModelError) else "自动总结失败，请检查模型配置和存储。"
                    if complete:
                        self.last_run_date = day
            except Exception:
                # Listing groups can fail before the per-group retry handler.
                # Keep the scheduler alive through temporary storage errors.
                self.last_error = "自动总结暂时无法读取群配置，将稍后重试。"
            await asyncio.sleep(30)
