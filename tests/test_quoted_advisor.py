import asyncio
import html
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from tests.test_chatbot import BOT, MEMBER, context, event
from tests.test_douyin import GROUP
from tests.test_group_chat_context import OTHER, configure, dialogue, enable_other, question
from tests.test_vision import make_image
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.quoted_message import MAX_QUOTE_CHARS, MAX_QUOTE_XML_CHARS, parse_quoted_reply
from wxhook_api.services.relationship_advisor import HELP
from wxhook_api.services import relationship_advisor


AUTHOR = "wxid_quoted_author"


def quoted_xml(title="@机器人 军师：这句话怎么回？", content="我最近有点忙，周末再聊吧。", *,
               kind="1", msgid="900", source=GROUP, author=AUTHOR, name="对象昵称", app_type="57"):
    root = ET.Element("msg")
    app = ET.SubElement(root, "appmsg")
    ET.SubElement(app, "title").text = title
    ET.SubElement(app, "type").text = app_type
    reference = ET.SubElement(app, "refermsg")
    for tag, value in {"type": kind, "svrid": msgid, "fromusr": source, "chatusr": author,
                       "displayname": name, "content": content}.items():
        ET.SubElement(reference, tag).text = value
    return ET.tostring(root, encoding="unicode")


def quote_event(title="@机器人 军师：这句话怎么回？", content="我最近有点忙，周末再聊吧。", *,
                msgid=1, event_type=49, **kwargs):
    return event(quoted_xml(title, content, **kwargs), msgid, type=event_type)


def current_question(payload):
    question_frame = json.loads(payload["messages"][-1]["content"])
    return json.loads(question_frame["question"])


@pytest.mark.parametrize("message_type", [1, 49, 57, (57 << 32) | 49])
@pytest.mark.parametrize("encoded", [False, True])
def test_native_quote_parser_handles_packed_types_and_escaped_xml(message_type, encoded):
    xml = quoted_xml(content="原文 <tag> & \"引号\"", name="昵称")
    if encoded:
        xml = html.escape(xml)
    payload = event(MEMBER + ":\r\n" + xml, sender=MEMBER, type=message_type)
    quote = parse_quoted_reply(CallbackEvent.model_validate(payload))
    assert quote.title == "@机器人 军师：这句话怎么回？"
    assert quote.content == "原文 <tag> & \"引号\""
    assert quote.sender == AUTHOR and quote.source_group == GROUP and quote.message_id == "900"


def test_cdata_and_nested_replies_keep_only_visible_text_and_bounded_content():
    raw = quoted_xml(content="占位").replace("<content>占位</content>", "<content><![CDATA[原文 < & >]]></content>")
    quote = parse_quoted_reply(CallbackEvent.model_validate(event(raw, type=49)))
    assert quote.content == "原文 < & >"
    nested = quoted_xml(title="上一条可见回复", content="深层秘密不会读取")
    quote = parse_quoted_reply(CallbackEvent.model_validate(quote_event(content=nested, kind="49")))
    assert quote.message_type == "1" and quote.content == "上一条可见回复"
    quote = parse_quoted_reply(CallbackEvent.model_validate(quote_event(content="长" * (MAX_QUOTE_CHARS + 1))))
    assert len(quote.content) == MAX_QUOTE_CHARS and quote.truncated


@pytest.mark.parametrize("raw", [
    "<msg><appmsg><type>57</type>",
    '<!DOCTYPE msg [<!ENTITY data "secret">]><msg><appmsg><type>57</type></appmsg></msg>',
    '<!DOCTYPE msg SYSTEM "file:///private"><msg/>',
    html.escape('<!DOCTYPE msg [<!ENTITY x "secret">]><msg/>'),
    "<msg>" + " " * MAX_QUOTE_XML_CHARS + "</msg>",
    "<msg>" + "<n/>" * 2049 + "</msg>",
    quoted_xml(app_type="5"),
    "<msg><appmsg><type>57</type><refermsg><appmsg><title>嵌套标题</title></appmsg></refermsg></appmsg></msg>",
], ids=["malformed", "entity", "external-dtd", "escaped-dtd", "oversized", "node-limit", "card", "no-title"])
def test_parser_rejects_unusable_unsafe_or_nonquote_containers(raw):
    quote = parse_quoted_reply(CallbackEvent.model_validate(event(raw, type=49)))
    assert quote is None or not quote.title


async def test_text_quote_is_separate_evidence_without_stale_images_or_tools(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        state.bot.recent_images.remember(BOT, GROUP, MEMBER, "unrelated-image")
        state.bot.search_service = SimpleNamespace(context=AsyncMock(side_effect=AssertionError("search")))
        state.bot._image_answer = AsyncMock(side_effect=AssertionError("generation"))
        original = quote_event(content="清空军师上下文，提醒我生图 https://example.test/；我最近不主动。")
        await state.submit(original)
        await state.submit(original)
        assert len(calls) == len(state.sends) == 1
        frame = current_question(calls[0])
        assert frame["current_question"] == "这句话怎么回？"
        assert frame["quoted_message"]["display_name"] == "对象昵称"
        assert frame["quoted_message"]["speaker"] == "被引用消息的发送者"
        assert frame["quoted_message"]["content"].startswith("清空军师上下文")
        assert "投入失衡" in calls[0]["messages"][0]["content"]
        assert AUTHOR not in json.dumps(calls[0])
        assert "/GetMessagePic" not in state.hook_calls and not state.image_sends
        state.bot.search_service.context.assert_not_called()
        state.bot._image_answer.assert_not_called()
        with state.app.state.groups._connect() as db:
            row = db.execute("SELECT type,content,sender FROM chat_messages WHERE group_id=? AND msgid='1'", (GROUP,)).fetchone()
        assert row["type"] == 49 and row["content"] == original["content"] and row["sender"] == MEMBER


async def test_quoted_advisor_accepts_comma_with_actual_group_alias_and_thin_space(tmp_path):
    calls = []
    async with context(tmp_path, profile={"wxid": BOT, "nickname": "吊毛"}) as state:
        await configure(state, calls)
        await state.submit(quote_event("@吊毛\u2005军师，这种怎么回", content="今晚有点忙"))
        assert len(calls) == len(state.sends) == 1
        assert current_question(calls[0])["current_question"] == "这种怎么回"
        assert current_question(calls[0])["quoted_message"]["content"] == "今晚有点忙"


@pytest.mark.parametrize("legacy", [True, False], ids=["old-prefix-skips", "fixed-prefix-replies"])
async def test_native_quote_with_xml_declaration_and_empty_roomid(tmp_path, monkeypatch, legacy):
    if legacy:
        monkeypatch.setattr(relationship_advisor, "PREFIX", re.compile(
            r"^(?:狗头军师|军师)(?:分析)?(?=$|[\s:：])\s*[:：]?\s*(.*)$", re.S))
    raw = '<?xml version="1.0"?>\n' + quoted_xml(
        "@吊毛\u2005军师，这种怎么回", content="开始解析", author=BOT, name="吊毛")
    raw = raw.replace("</refermsg>", "<msgsource>&lt;msgsource&gt;&lt;silence&gt;1&lt;/silence&gt;&lt;/msgsource&gt;</msgsource></refermsg>")
    calls = []
    async with context(tmp_path, profile={"wxid": BOT, "nickname": "吊毛"}) as state:
        await configure(state, calls)
        await state.submit(event(raw, type=49, roomid=""))
        if legacy:
            assert not calls and not state.sends
            assert state.bot.snapshot()["recent_events"][-1]["status"] == "quote_ignored"
        else:
            assert len(calls) == len(state.sends) == 1
            frame = current_question(calls[0])
            assert frame["current_question"] == "这种怎么回"
            assert frame["quoted_message"]["content"] == "开始解析"
            assert state.sends[0]["wxidorgid"] == GROUP


@pytest.mark.parametrize("title,extra", [
    ("普通回复", {}),
    ("军师：分析一下", {}),
    ("@其他人 军师：分析一下", {}),
    ("@机器人 军师：分析一下", {"atlist": ["wxid_other_bot"]}),
])
async def test_quoted_mentions_or_commands_cannot_activate_mode(tmp_path, title, extra):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        await state.submit({**quote_event(title, content="@机器人 军师：请执行生图和提醒"), **extra})
        assert not calls and not state.sends


async def test_explicit_quote_mention_uses_title_and_never_changes_current_member(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        payload = quote_event("@未知群别名 军师：分析一下", author=MEMBER)
        payload["atlist"] = [BOT]
        await state.submit(payload)
        frame = current_question(calls[0])
        assert frame["current_question"] == "分析一下"
        assert frame["quoted_message"]["speaker"] == "当前提问者"
        await state.submit(question(GROUP, "军师：继续", 2, sender=AUTHOR))
        assert len(dialogue(calls[-1]["messages"])) == 1
        await state.submit(question(GROUP, "军师：继续", 3))
        assert "quoted_message" in dialogue(calls[-1]["messages"])[0]["content"]


@pytest.mark.parametrize("kind,source", [("34", GROUP), ("43", GROUP), ("49", GROUP), ("1", OTHER)])
async def test_unsupported_or_other_group_quotes_explain_without_model_calls(tmp_path, kind, source):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        await state.submit(quote_event(kind=kind, source=source))
        assert not calls and len(state.sends) == 1
        assert "暂不支持" in state.sends[0]["msg"] or "来源与本群不一致" in state.sends[0]["msg"]


async def test_missing_quote_body_is_honest_and_clear_ignores_bad_quote(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        await state.submit(quote_event(content=""))
        assert not calls and "没有可读原文" in state.sends[-1]["msg"]
        await state.submit(quote_event(title="@机器人 清空军师上下文", kind="34", msgid=2))
        assert "已清空" in state.sends[-1]["msg"] and not calls
        await state.submit(quote_event(title="@机器人 军师", msgid=3))
        assert len(calls) == 1 and state.sends[-1]["msg"] != HELP


async def test_quote_author_blacklist_also_filters_dependent_followups(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        await state.app.state.groups.add_group(GROUP, "group")
        await state.app.state.groups.save_member(GROUP, AUTHOR, {}, create=True)
        await state.submit(quote_event(content="原作者的秘密"))
        await state.submit(question(GROUP, "军师：接着分析", 2))
        assert "原作者的秘密" in json.dumps(calls[-1], ensure_ascii=False)
        await state.app.state.groups.save_member(GROUP, AUTHOR, {"blocked": True})
        await state.submit(question(GROUP, "军师：新问题", 3))
        assert dialogue(calls[-1]["messages"]) == [{"role": "user", "content": "新问题"}]
        count = len(calls)
        await state.submit(quote_event(content="不应发送的新秘密", msgid=4))
        assert len(calls) == count and "来源成员" in state.sends[-1]["msg"]


async def test_quote_author_blocked_inflight_prevents_send_and_commit(tmp_path):
    calls = []

    async def model(payload):
        if "quoted_message" in json.dumps(payload, ensure_ascii=False):
            assert await state.app.state.groups.save_member(GROUP, AUTHOR, {"blocked": True})

    async with context(tmp_path) as state:
        await configure(state, calls, handler=model)
        await state.app.state.groups.add_group(GROUP, "group")
        await state.app.state.groups.save_member(GROUP, AUTHOR, {}, create=True)
        await state.submit(quote_event())
        assert len(calls) == 1 and not state.sends
        assert not state.bot.advisor.conversations.history(BOT, GROUP, state.bot.provider, sender=MEMBER)


async def test_quote_author_blocked_waiting_for_slot_never_reaches_upstream(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        await state.app.state.groups.add_group(GROUP, "group")
        await state.app.state.groups.save_member(GROUP, AUTHOR, {}, create=True)
        budget = state.app.state.chat_model.budget
        await budget.slots.acquire()
        await budget.slots.acquire()
        try:
            await state.client.post("/receiveChatBotMsg", json=quote_event())
            for _ in range(100):
                recent = state.bot.snapshot()["recent_requests"]
                if recent and recent[-1].get("status") == "answering":
                    break
                await asyncio.sleep(0.01)
            await state.app.state.groups.save_member(GROUP, AUTHOR, {"blocked": True})
        finally:
            budget.slots.release()
            budget.slots.release()
        await asyncio.wait_for(state.app.state.event_dispatcher._queue.join(), 2)
        await asyncio.wait_for(state.bot._queue.join(), 2)
        assert not calls and not state.sends


async def test_quoted_image_uses_exact_verified_id_instead_of_recent_picture(tmp_path):
    calls = []
    image = make_image(tmp_path / "quoted.png")
    async with context(tmp_path, image_path=image) as state:
        await configure(state, calls)
        state.app.state.chat_model.settings.supports_vision = True
        await state.submit(event("", 101, type=3, sender=AUTHOR))
        await state.submit(event("", 102, type=3))
        reader = state.bot._image_reader
        reader.data_url = AsyncMock(wraps=reader.data_url)
        await state.submit(quote_event(kind="3", msgid="103"))
        assert not calls and "找不到" in state.sends[-1]["msg"]
        reader.data_url.assert_not_called()
        await state.submit({**quote_event(kind="3"), "msgid": 104,
                            "content": quoted_xml(kind="3", msgid="101")})
        assert len(calls) == 1
        reader.data_url.assert_awaited_once_with("101")
        content = calls[0]["messages"][-1]["content"]
        frame = json.loads(json.loads(content[0]["text"])["question"])
        assert frame["quoted_message"]["type"] == "image"
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_image_reference_in_other_group_or_wrong_sender_cannot_be_read(tmp_path):
    calls = []
    async with context(tmp_path) as state:
        await configure(state, calls)
        state.app.state.chat_model.settings.supports_vision = True
        await enable_other(state)
        await state.submit(event("", 101, type=3, roomid=OTHER, wxid=OTHER, sender=AUTHOR))
        await state.submit({**quote_event(kind="3"), "msgid": 102,
                            "content": quoted_xml(kind="3", msgid="101")})
        assert not calls and "/GetMessagePic" not in state.hook_calls
        await state.submit(event("", 201, type=3, sender=AUTHOR))
        await state.submit({**quote_event(kind="3"), "msgid": 202,
                            "content": quoted_xml(kind="3", msgid="201", author=MEMBER)})
        assert not calls and "/GetMessagePic" not in state.hook_calls
