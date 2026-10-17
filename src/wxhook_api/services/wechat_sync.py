from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Any

from wxhook_api.clients.hook_client import HookClient, HookResponse
from wxhook_api.core.douyin_config import GroupSettings
from wxhook_api.core.errors import HookTransportError
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.schemas.requests import ExecuteQueryRequest, HistoryRequest
from wxhook_api.services.groups import GROUP_PATTERN, GroupManager, event_group, now


class SyncError(ValueError):
    pass


@dataclass(frozen=True)
class SyncOptions:
    enabled: bool = True
    interval: float = 60
    retry_interval: float = 5
    page_size: int = 200
    pages_per_group: int = 5

    @classmethod
    def from_environment(cls):
        return cls(enabled=os.getenv("WXHOOK_SYNC_ENABLED", "true").lower() not in {"0", "false", "off"},
                   interval=max(5, float(os.getenv("WXHOOK_SYNC_INTERVAL", "60"))))


def quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def canonical(value: str) -> str:
    return value.lower().replace("_", "")


def column(columns: dict[str, str], *names: str) -> str | None:
    return next((columns[canonical(name)] for name in names if canonical(name) in columns), None)


def select_name(columns: dict[str, str], prefix: str = "") -> str:
    names = [column(columns, "remark"), column(columns, "nick_name", "nickname", "display_name")]
    expressions = [f"NULLIF({prefix}{quoted(name)},'')" for name in dict.fromkeys(names) if name]
    return "COALESCE(" + ",".join([*expressions, "''"]) + ")" if expressions else "''"


def records(response: HookResponse, *, array: bool = False) -> list[dict]:
    if response.status_code != 200:
        raise SyncError("微信数据接口请求失败，稍后自动重试")
    data = response.data
    if array and isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and str(data.get("status")) == "0":
        rows = data.get("data")
    else:
        raise SyncError("微信数据库暂不可读或接口返回异常，稍后自动重试")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SyncError("微信数据格式不受支持，未将该批数据标记为同步完成")
    return rows


class WeChatSync:
    """Read-only Hook synchronization; imported history never enters the automation dispatcher."""

    def __init__(self, manager: GroupManager, options: SyncOptions):
        self.manager, self.options = manager, options
        self.client: HookClient | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._pass_lock = asyncio.Lock()
        self.status: dict[str, Any] = {"enabled": options.enabled, "phase": "waiting" if options.enabled else "disabled",
            "message": "等待微信登录后自动同步" if options.enabled else "自动同步已关闭", "last_synced_at": "",
            "groups": 0, "members": 0, "imported_messages": 0, "pending_groups": 0, "warnings": []}

    def start(self, client: HookClient) -> None:
        self.client = client
        if self.options.enabled and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def request(self) -> bool:
        if not self.options.enabled:
            return False
        self._wake.set()
        return True

    async def _account(self) -> str:
        response = await self.client.get_self_profile()
        profile = response.data
        if isinstance(profile, dict) and isinstance(profile.get("data"), dict):
            profile = profile["data"]
        account = profile.get("wxid") if isinstance(profile, dict) else None
        if response.status_code != 200 or not isinstance(account, str) or not account or len(account) > 256:
            raise SyncError("无法确认当前微信账号，等待登录后重试")
        await self.manager.claim_account(account)
        return account

    async def _same_account(self, account: str):
        if await self._account() != account:
            raise SyncError("微信账号已变化，已停止本轮同步")

    async def receive(self, event: CallbackEvent):
        if not event_group(event):
            return
        # Verify the account before persisting callbacks as well as historical pages.
        await self._account()
        await self.manager.receive(event)

    async def _query(self, database: str, sql: str) -> list[dict]:
        if not sql.lstrip().upper().startswith(("SELECT ", "PRAGMA TABLE_INFO(")):
            raise SyncError("同步仅允许只读查询")
        return records(await self.client.execute_query(ExecuteQueryRequest(optDbName=database, SQL=sql)))

    async def _pages(self, database: str, sql: str) -> list[dict]:
        output, offset = [], 0
        previous = None
        while True:
            rows = await self._query(database, f"{sql} LIMIT 500 OFFSET {offset}")
            signature = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if rows and signature == previous:
                raise SyncError("微信数据库分页没有推进，未将群名单标记为同步完成")
            output.extend(rows)
            if len(rows) < 500:
                return output
            previous = signature
            offset += len(rows)

    async def _contacts(self) -> tuple[list[dict], dict[str, list[dict]], list[str]]:
        databases = records(await self.client.get_all_db_names(), array=True)
        candidates = list(dict.fromkeys(row["dbName"] for row in databases if isinstance(row.get("dbName"), str)
                          and re.search(r"(?:^|[/\\])(?:contact|micromsg|session)\.db$", row["dbName"], re.I)))
        if not candidates:
            raise SyncError("Hook 未提供 contact.db / MicroMsg.db / session.db，无法读取完整群名单")
        groups: dict[str, dict] = {}
        rosters: dict[str, list[dict]] = {}
        warnings = []
        recognized = False
        for database in candidates:
            tables = await self._query(database, "SELECT name FROM sqlite_master WHERE type='table'")
            schemas = {}
            for row in tables:
                table = row.get("name", "")
                if canonical(table) not in {"contact", "chatroom", "chatroommember", "sessiontable"}:
                    continue
                fields = await self._query(database, f"PRAGMA table_info({quoted(table)})")
                schemas[canonical(table)] = (table, {canonical(field["name"]): field["name"] for field in fields})
            for role in ("contact", "chatroom", "sessiontable"):
                if role not in schemas:
                    continue
                table, fields = schemas[role]
                identity = column(fields, "username", "chatroomname", "chatroomid", "roomid", "wxid")
                if not identity:
                    continue
                recognized = True
                name_expr = select_name(fields)
                rows = await self._pages(database, f"SELECT {quoted(identity)} AS username,{name_expr} AS name FROM {quoted(table)} WHERE {quoted(identity)} LIKE '%@chatroom' ORDER BY {quoted(identity)}")
                for row in rows:
                    group = row.get("username", "")
                    if not re.fullmatch(GROUP_PATTERN, group):
                        continue
                    existing = groups.setdefault(group, {"id": group, "name": ""})
                    if row.get("name"):
                        existing["name"] = str(row["name"])
            if {"contact", "chatroom", "chatroommember"} <= schemas.keys():
                contact, c = schemas["contact"]
                room, r = schemas["chatroom"]
                relation, m = schemas["chatroommember"]
                cid, cname = column(c, "id"), column(c, "username")
                rid, rname = column(r, "id"), column(r, "username")
                mroom, mmember = column(m, "roomid"), column(m, "memberid")
                if all((cid, cname, rid, rname, mroom, mmember)):
                    room_rows = await self._pages(database, f"SELECT {quoted(rname)} AS username FROM {quoted(room)} ORDER BY {quoted(rid)}")
                    db_rooms = {row["username"] for row in room_rows if isinstance(row.get("username"), str) and re.fullmatch(GROUP_PATTERN, row["username"])}
                    for group in db_rooms:
                        rosters[group] = []
                    sql = (f"SELECT r.{quoted(rname)} AS group_id,c.{quoted(cname)} AS member_id,{select_name(c, 'c.')} AS name "
                           f"FROM {quoted(relation)} m JOIN {quoted(room)} r ON r.{quoted(rid)}=m.{quoted(mroom)} "
                           f"JOIN {quoted(contact)} c ON c.{quoted(cid)}=m.{quoted(mmember)} ORDER BY m.{quoted(mroom)},m.{quoted(mmember)}")
                    joined = await self._pages(database, sql)
                    expected = await self._query(database, f"SELECT COUNT(*) AS total FROM {quoted(relation)}")
                    if len(joined) != int(expected[0]["total"]):
                        for group in db_rooms:
                            rosters.pop(group, None)
                        warnings.append("部分成员关系无法关联到联系人，未将该数据库的成员名单标记为完整")
                        continue
                    for row in joined:
                        if row.get("group_id") in db_rooms and row.get("member_id"):
                            rosters[row["group_id"]].append({"id": str(row["member_id"]), "name": str(row.get("name") or "")})
            # Older Hook databases expose a delimited member list directly.
            if "chatroom" in schemas:
                table, fields = schemas["chatroom"]
                group_col = column(fields, "username", "chatroomname", "chatroomid", "roomid")
                member_col = column(fields, "memberlist", "userlist")
                if group_col and member_col:
                    rows = await self._pages(database, f"SELECT {quoted(group_col)} AS group_id,{quoted(member_col)} AS members FROM {quoted(table)} ORDER BY {quoted(group_col)}")
                    for row in rows:
                        group = row.get("group_id")
                        value = row.get("members")
                        if group not in groups or not isinstance(value, str):
                            continue
                        ids = re.split(r"\x07|\^G|[;,\n]", value)
                        if all(not item or re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", item) for item in ids):
                            rosters[group] = [{"id": item, "name": ""} for item in dict.fromkeys(ids) if item]
        if not recognized:
            raise SyncError("微信数据库表结构尚不受支持，请检查 Hook 版本；未使用有限的联系人搜索冒充全量同步")
        unsupported = len(set(groups) - rosters.keys())
        if unsupported:
            warnings.append(f"{unsupported} 个群暂未提供可读的完整成员关系，成员列表会明确标注为部分发现")
        return list(groups.values()), rosters, warnings

    async def _history(self, group: dict, account: str) -> bool:
        size = self.options.page_size
        start = max(1, group["history_next_page"] - 1)
        pages = list(dict.fromkeys([1, *range(start, start + max(2, self.options.pages_per_group))]))
        previous = None
        for page in pages:
            response = await self.client.get_history(HistoryRequest(wxid=group["id"], size=size, page=page))
            rows = records(response)
            if response.data.get("wxid", group["id"]) != group["id"] or int(response.data.get("page", page)) != page or len(rows) > size:
                raise SyncError("微信历史接口返回的会话或分页信息不一致，未导入该页")
            await self._same_account(account)
            signature = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if rows and previous == signature:
                raise SyncError("微信历史消息分页没有推进，未标记为完整导入")
            previous = signature
            added, unseen_history = await self.manager.import_history(group["id"], rows)
            self.status["imported_messages"] += added
            # The front page is refreshed during long imports; it must not reset the saved deep cursor.
            if page == 1 and start > 1:
                continue
            overlap = page < group["history_next_page"]
            done = len(rows) < size or (group["history_complete"] and not unseen_history and not overlap)
            await self.manager.history_checkpoint(group["id"], next_page=1 if done else page + 1,
                complete=bool(group["history_complete"] or done))
            if done:
                return True
        return False

    async def run_once(self):
        async with self._pass_lock:
            response = await self.client.get_status()
            state = response.data
            if response.status_code != 200 or not isinstance(state, dict) or str(state.get("IsLogin", state.get("data", {}).get("IsLogin") if isinstance(state.get("data"), dict) else "")) != "1":
                self.status.update(phase="waiting", message="等待微信登录，已有聊天记录可离线查看")
                return
            account = await self._account()
            self.status.update(phase="running", message="正在同步微信群聊和成员…", warnings=[])
            groups, rosters, warnings = await self._contacts()
            await self._same_account(account)
            await self.manager.sync_contacts(groups, rosters)
            self.status.update(groups=len(groups), members=sum(len(people) for people in rosters.values()), warnings=warnings)
            pending, failures = 0, 0
            # Include manually managed or callback-discovered groups whose history is available in this account.
            for group in await self.manager.groups(GroupSettings()):
                self.status["message"] = f'正在导入「{group["name"] or group["id"]}」的聊天记录…'
                try:
                    if not await self._history(group, account):
                        pending += 1
                except (ValueError, RuntimeError) as exc:
                    failures += 1
                    error = str(exc) if isinstance(exc, (SyncError, ValueError)) else "微信历史接口暂不可用，将自动重试"
                    await self.manager.history_failure(group["id"], error)
                    if isinstance(exc, HookTransportError) or "账号" in error:
                        raise
            self.status.update(pending_groups=pending + failures, phase="partial" if failures or warnings else "running" if pending else "idle",
                last_synced_at=now(), message=f"已同步 {len(groups)} 个微信群；" + (f"{pending + failures} 个群的历史记录待继续导入" if pending or failures else "聊天记录已更新") + ("；部分成员名单不可读" if warnings else ""))

    async def _run(self):
        while True:
            self._wake.clear()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.update(phase="error", message=str(exc) if isinstance(exc, ValueError) else "微信连接或本地存储暂不可用，将自动重试")
            delay = self.options.retry_interval if self.status["phase"] in {"waiting", "error", "running"} or self.status["pending_groups"] else self.options.interval
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
