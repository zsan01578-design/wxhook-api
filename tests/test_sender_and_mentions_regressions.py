import asyncio
import json
import threading

import pytest
import yaml

from tests.test_chatbot import MEMBER, Answers, context, event
from tests.test_douyin import GROUP, SHORT
from tests.test_review_regressions import keyword_rule
from wxhook_api.services.groups import GroupManager


@pytest.mark.parametrize("sender", ["", GROUP])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("feature", ["chat", "keyword", "media", "reminder"])
async def test_prefixed_member_identity_applies_blacklist_and_attribution(tmp_path, sender, newline, feature):
    answers = Answers()
    async with context(tmp_path, provider=answers) as state:
        groups, tools = state.app.state.groups, state.app.state.group_tools
        if feature == "keyword":
            await tools.save_rule(GROUP, keyword_rule())
        text = {"chat": "@机器人\u2005你好", "keyword": "weather", "media": SHORT,
                "reminder": "@机器人\u2005提醒一分钟后喝水"}[feature]
        content = f"{MEMBER}:{newline}{text}"
        await groups.save_member(GROUP, MEMBER, {"blocked": True}, create=True)
        await state.submit(event(content, sender=sender))
        await asyncio.wait_for(tools._queue.join(), 2)
        assert not answers.questions and not state.sends and not state.media.snapshot()["jobs"]
        assert not (await tools.snapshot(GROUP))["hits"]
        with groups._connect() as db:
            archived = db.execute("SELECT sender,raw_json FROM chat_messages WHERE group_id=?", (GROUP,)).fetchone()
        assert archived["sender"] == MEMBER
        assert json.loads(archived["raw_json"])["sender"] == sender
        await groups.save_member(GROUP, MEMBER, {"blocked": False})
        await state.submit(event(content, 2, sender=sender))
        await asyncio.wait_for(tools._queue.join(), 2)
        if feature == "chat":
            assert answers.questions == ["你好"]
        elif feature == "keyword":
            hit = (await tools.snapshot(GROUP))["hits"][0]
            assert hit["sender"] == MEMBER and hit["content"] == "weather"
        elif feature == "media":
            assert state.media.snapshot()["jobs"][0]["sender"] == MEMBER
        else:
            task = (await tools.snapshot(GROUP))["tasks"][0]
            assert task["creator_id"] == MEMBER and task["title"] == "喝水"


async def test_explicit_member_identity_is_not_replaced_by_quoted_prefix(tmp_path):
    async with context(tmp_path) as state:
        groups = state.app.state.groups
        await groups.save_member(GROUP, MEMBER, {"blocked": True}, create=True)
        await state.submit(event(f"{MEMBER}:\n{SHORT}", sender="wxid_another_member"))
        assert state.media.snapshot()["jobs"][0]["sender"] == "wxid_another_member"


@pytest.mark.parametrize("sender", ["", GROUP])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_history_uses_same_prefixed_member_identity(tmp_path, sender, newline):
    groups = GroupManager(tmp_path / "history.sqlite3")
    await groups.start([GROUP])
    row = dict(msgid=1, timestamp=1800000000, sender=sender, type=1,
               content=f"{MEMBER}:{newline}hello")
    assert await groups.import_history(GROUP, [row]) == (1, 1)
    assert await groups.import_history(GROUP, [row]) == (0, 0)
    people = await groups.members(GROUP)
    assert len(people) == 1 and people[0]["id"] == MEMBER and people[0]["message_count"] == 1
    with groups._connect() as db:
        archived = db.execute("SELECT sender,raw_json FROM chat_messages").fetchone()
    assert archived["sender"] == MEMBER and json.loads(archived["raw_json"])["sender"] == sender


@pytest.mark.parametrize("fail", [False, True])
async def test_cancelled_mention_save_settles_before_following_save(tmp_path, monkeypatch, fail):
    import wxhook_api.core.chatbot_config as module

    entered, release = threading.Event(), threading.Event()
    original = module.tempfile.NamedTemporaryFile

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        if fail:
            raise OSError("synthetic write failure")
        return original(*args, **kwargs)

    async with context(tmp_path) as state:
        monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", delayed)
        pending = asyncio.create_task(state.bot.configure_mentions(["first"]))
        following = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", original)
            following = asyncio.create_task(state.bot.configure_mentions(["second"]))
            await asyncio.sleep(0)
            assert state.bot._names_lock.locked() and not pending.done() and not following.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 3)
            await asyncio.wait_for(following, 3)
            saved = yaml.safe_load((tmp_path / "chatbot.local.yml").read_text(encoding="utf-8"))
            assert saved["mention-names"] == ["second"]
            assert state.bot.options.mention_names == ("second",)
            assert "second" in state.bot.snapshot()["mention_names"]
        finally:
            release.set()
            await asyncio.gather(*(task for task in (pending, following) if task), return_exceptions=True)
