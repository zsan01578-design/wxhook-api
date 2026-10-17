from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import re

from wxhook_api.services.chat_model import ChatModelConfiguration, ModelError
from wxhook_api.services.groups import GroupManager


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

    async def get(self, group: str, *, force: bool = False, summary_date: str | None = None) -> dict:
        day = summary_date or local_date()
        async with self._lock:
            if not force:
                cached = await self.groups.get_summary(group, day)
                if cached:
                    return cached
            messages = await self.groups.summary_messages(group, day, bot_wxid=self.bot_wxid)
            if not messages:
                return await self.groups.save_summary(group, day, "今天暂无可总结的成员消息。", 0)
            if not self.models.settings:
                raise ModelError("AI 模型尚未配置，无法生成群聊总结。")
            lines = [f"[{datetime.fromtimestamp(item['timestamp'], timezone.utc).astimezone(CHINA).strftime('%H:%M')}] {item['sender']}: {str(item['content'])[:1200]}" for item in messages[-800:]]
            prompt = "请总结下面这一天的微信群聊天记录。只根据记录归纳，不要编造。输出中文，控制在 300 字以内，使用简洁的标题和项目符号，包含：主要话题、重要结论、待办或未解决问题；没有对应内容时省略该项。不要提及你是模型。\n\n聊天记录：\n" + "\n".join(lines)
            summary = (await self.models.answer(prompt)).strip()
            if not summary:
                raise ModelError("模型未返回有效的群聊总结。")
            return await self.groups.save_summary(group, day, summary[:4000], len(messages))

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
            current = datetime.now(CHINA)
            if current.hour >= 18 and self.last_run_date != current.date().isoformat():
                day = current.date().isoformat()
                self.last_error = ""
                complete = True
                for group in await self.groups.summary_group_ids():
                    try:
                        await self.get(group, summary_date=day)
                    except Exception as exc:
                        complete = False
                        self.last_error = str(exc)
                if complete:
                    self.last_run_date = day
            await asyncio.sleep(30)
