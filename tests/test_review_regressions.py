import asyncio
import sqlite3
import threading

import pytest
import yaml

from tests.test_chatbot import BOT, MEMBER, context, event
from tests.test_douyin import GROUP, SHORT, options
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.wechat_sync import SyncOptions


def keyword_rule(reply="sunny", action="reply"):
    return dict(name="weather", keyword="weather", match_mode="contains", action=action,
                reply=reply, enabled=True, cooldown=10)


@pytest.mark.parametrize("fail", [False, True])
async def test_cancelled_media_save_keeps_lock_until_commit_settles(tmp_path, monkeypatch, fail):
    import wxhook_api.services.douyin as module

    service = DouyinService(options(tmp_path))
    entered, release = threading.Event(), threading.Event()
    original = module.save_group_settings

    def delayed(config, settings):
        if settings.enabled:
            entered.set()
            assert release.wait(5)
            if fail:
                raise OSError("synthetic write failure")
        return original(config, settings)

    monkeypatch.setattr(module, "save_group_settings", delayed)
    pending = asyncio.create_task(service.configure(service.options.settings))
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        following = asyncio.create_task(service.configure(
            service.options.settings.model_copy(update={"enabled": False})))
        await asyncio.sleep(0)
        assert service._config_lock.locked() and not pending.done() and not following.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        await asyncio.wait_for(following, 3)
        saved = yaml.safe_load(service.options.config_path.read_text(encoding="utf-8"))
        assert saved["enabled"] is False and service.options.settings.enabled is False
    finally:
        release.set()
        await asyncio.gather(*(task for task in (pending, following) if task), return_exceptions=True)


@pytest.mark.parametrize("action", ["record", "reply"])
async def test_duplicate_keyword_callback_cannot_trigger_replacement_rule(tmp_path, action):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        first = await tools.save_rule(GROUP, keyword_rule(action=action))
        await state.submit(event("weather", 77))
        await asyncio.wait_for(tools._queue.join(), 2)
        await tools.delete_rule(GROUP, first)
        await tools.save_rule(GROUP, keyword_rule("rainy"))
        await state.submit(event("weather", 77))
        await asyncio.wait_for(tools._queue.join(), 2)
        assert [item["msg"] for item in state.sends] == (["sunny"] if action == "reply" else [])
        assert len((await tools.snapshot(GROUP))["hits"]) == 1
        await state.submit(event("weather", 78))
        await asyncio.wait_for(tools._queue.join(), 2)
        assert state.sends[-1]["msg"] == "rainy"


async def test_legacy_keyword_claim_survives_rule_deletion_and_restart(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        rule_id = await tools.save_rule(GROUP, keyword_rule())
        await tools.db(lambda db: db.execute(
            "INSERT INTO chat_reply_claims VALUES (?,?,'keyword','old','old')",
            (GROUP, f"keyword:{rule_id}:77")))
        await tools.delete_rule(GROUP, rule_id)
    for _ in range(2):
        async with context(tmp_path) as state:
            tools = state.app.state.group_tools
            await tools.save_rule(GROUP, keyword_rule())
            await state.submit(event("weather", 77))
            await asyncio.wait_for(tools._queue.join(), 2)
            assert not state.sends and not (await tools.snapshot(GROUP))["hits"]
            claims = await tools.db(lambda db: db.execute(
                "SELECT count(*) FROM chat_reply_claims WHERE group_id=? AND message_key='keyword-message:77'",
                (GROUP,)).fetchone()[0])
            assert claims == 1


@pytest.mark.parametrize("enabled", [False, True])
async def test_first_self_share_is_ignored_after_account_discovery(tmp_path, enabled):
    async with context(tmp_path, bot_options=ChatbotOptions(enabled, "", ())) as state:
        state.app.state.wechat_sync.options = SyncOptions(enabled=True)
        await state.submit(event(SHORT, sender=BOT))
        assert state.bot.identity == BOT
        assert not state.media.snapshot()["jobs"] and not state.media_calls
        await state.submit(event(SHORT, 2))
        assert len(state.media.snapshot()["jobs"]) == 1 and state.media_calls


@pytest.mark.parametrize("original_sender", ["", GROUP, "wxid_wrong", MEMBER])
async def test_history_sender_enrichment_updates_counts_without_double_counting(tmp_path, original_sender):
    groups = GroupManager(tmp_path / "management.sqlite3")
    await groups.start([GROUP])
    await groups.receive(CallbackEvent.model_validate(event("hi", sender=original_sender)))
    history = [dict(msgid=1, timestamp=1800000000, sender=MEMBER, type=1, content="hi")]
    assert await groups.import_history(GROUP, history) == (0, 1)
    assert await groups.import_history(GROUP, history) == (0, 0)
    # An empty historical sender must preserve the known sender and its count.
    await groups.import_history(GROUP, [{**history[0], "sender": ""}])
    people = {member["id"]: member for member in await groups.members(GROUP)}
    assert people[MEMBER]["message_count"] == 1
    if original_sender == "wxid_wrong":
        assert people[original_sender]["message_count"] == 0
    with groups._connect() as db:
        assert db.execute("SELECT message_count FROM groups WHERE id=?", (GROUP,)).fetchone()[0] == 1


async def test_startup_repairs_existing_member_counts_once_and_preserves_policies(tmp_path):
    path = tmp_path / "management.sqlite3"
    groups = GroupManager(path)
    await groups.start([GROUP])
    await groups.import_history(GROUP, [
        dict(msgid=1, timestamp=1800000000, sender=MEMBER, type=1, content="hi"),
        dict(msgid=2, timestamp=1800000001, sender=MEMBER, type=1, content="again"),
    ])
    await groups.save_member(GROUP, MEMBER, {"name": "local name", "blocked": True})
    await groups.save_member(GROUP, "wxid_wrong", {"name": "previous sender"}, create=True)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM sync_metadata WHERE key='member_message_counts_v1'")
        db.execute("UPDATE members SET message_count=99")
    restored = GroupManager(path)
    await restored.start([])
    people = {member["id"]: member for member in await restored.members(GROUP)}
    assert people[MEMBER]["message_count"] == 2 and people["wxid_wrong"]["message_count"] == 0
    assert people[MEMBER]["name"] == "local name" and people[MEMBER]["blocked"]
    with restored._connect() as db:
        marker = db.execute("SELECT value FROM sync_metadata WHERE key='member_message_counts_v1'").fetchone()[0]
    await restored.start([])
    with restored._connect() as db:
        assert db.execute("SELECT value FROM sync_metadata WHERE key='member_message_counts_v1'").fetchone()[0] == marker
