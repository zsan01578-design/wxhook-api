"""Bounded parsing of native WeChat appmsg/57 replies; never execute quoted XML."""
from dataclasses import dataclass
import html
import json
import re
from xml.etree import ElementTree as ET

from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.groups import event_text


MAX_QUOTE_XML_CHARS = 65536
MAX_QUOTE_CHARS = 4000
MEMBER_ID = re.compile(r"[A-Za-z0-9_.@-]{1,256}")


@dataclass(frozen=True)
class QuotedReply:
    title: str
    content: str = ""
    sender: str = ""
    source_group: str = ""
    display_name: str = ""
    message_id: str = ""
    message_type: str = ""
    truncated: bool = False
    error: str = ""

    def question(self, question: str, current_sender: str) -> str:
        # Names and quoted text stay in user data, never in system instructions.
        return json.dumps({"current_question": question, "quoted_message": {
            "source": "当前微信消息附带的引用，身份信息未经独立核实",
            "speaker": "当前提问者" if self.sender and self.sender == current_sender else "被引用消息的发送者",
            "display_name": self.display_name,
            "type": "image" if self.message_type == "3" else "text",
            "content": self.content if self.message_type != "3" else "被引用图片，内容以随附图片为准",
            "truncated": self.truncated,
        }}, ensure_ascii=False)


def _xml(value: str):
    if len(value) > MAX_QUOTE_XML_CHARS:
        return None
    value = value.strip()
    if value.startswith("&lt;"):
        value = html.unescape(value)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", value, re.I):
        return None
    try:
        root = ET.fromstring(value)
    except (ET.ParseError, ValueError):
        return None
    return root if sum(1 for _ in root.iter()) <= 2048 else None


def _field(element, name: str) -> str:
    child = element.find(name)
    return (child.text or "") if child is not None else ""


def parse_quoted_reply(event: CallbackEvent) -> QuotedReply | None:
    # Some hooks expose the packed local_type (57 << 32 | 49).
    if event.type not in {1, 49, 57} and not (event.type >= 0 and event.type & 0xffffffff == 49):
        return None
    raw = event_text(event)
    if not raw.lstrip().startswith(("<", "&lt;")):
        return None
    root = _xml(raw)
    if root is None:
        return None
    app = root if root.tag == "appmsg" else root.find("appmsg") if root.tag == "msg" else None
    if app is None or _field(app, "type").strip() != "57":
        return None
    title = _field(app, "title").strip()
    reference = app.find("refermsg")
    if reference is None:
        return QuotedReply(title, error="微信引用缺少原文，请重新引用，或直接粘贴要分析的文字。")
    from_user = _field(reference, "fromusr").strip()
    chat_user = _field(reference, "chatusr").strip()
    scope = next((value for value in (from_user, chat_user) if value.endswith("@chatroom")), "")
    sender = next((value for value in (chat_user, from_user)
                   if MEMBER_ID.fullmatch(value) and not value.endswith("@chatroom")
                   and value not in {"0", "notify@all"}), "")
    kind = _field(reference, "type").strip()
    content = _field(reference, "content")
    error = ""
    # Only the visible title of a quoted reply is usable, without recursively
    # loading its quoted history, mention metadata, URLs or media attachments.
    if kind == "49":
        nested = _xml(content)
        nested_app = nested if nested is not None and nested.tag == "appmsg" else (
            nested.find("appmsg") if nested is not None and nested.tag == "msg" else None)
        if nested_app is not None and _field(nested_app, "type").strip() == "57":
            content = _field(nested_app, "title")
            kind = "1"
        else:
            error = "暂不支持分析引用的文件、链接或其他卡片，请粘贴其中要分析的文字。"
    elif kind not in {"1", "3"}:
        error = "暂不支持分析引用的语音、视频或其他消息，请提供文字或图片。"
    if kind == "1" and not content.strip():
        error = "这条微信引用没有可读原文，请重新引用，或直接粘贴文字。"
    message_id = _field(reference, "svrid").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,39}", message_id):
        message_id = ""
    return QuotedReply(title, content[:MAX_QUOTE_CHARS] if kind == "1" else "", sender, scope,
                       _field(reference, "displayname")[:80], message_id, kind,
                       len(content) > MAX_QUOTE_CHARS if kind == "1" else False, error)
