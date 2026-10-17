from dataclasses import dataclass
import os
from pathlib import Path
import re

from wxhook_api.core.config import PROJECT_ROOT, load_yaml


@dataclass(frozen=True)
class ChatbotOptions:
    enabled: bool = True
    bot_wxid: str = ""
    mention_names: tuple[str, ...] = ()
    max_reply_chars: int = 2000


def load_chatbot_options() -> ChatbotOptions:
    explicit = os.getenv("WXHOOK_CHATBOT_CONFIG")
    local = PROJECT_ROOT / "chatbot.local.yml"
    path = Path(explicit) if explicit else local if local.is_file() else PROJECT_ROOT / "chatbot.yml"
    values = load_yaml(path) if path.is_file() else {}
    enabled = values.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("chatbot enabled 必须为 true 或 false")
    if "WXHOOK_CHATBOT_ENABLED" in os.environ:
        enabled = os.environ["WXHOOK_CHATBOT_ENABLED"].strip().lower() not in {"false", "0", "off"}
    identity = values.get("bot-wxid", "")
    names = values.get("mention-names", [])
    limit = values.get("max-reply-chars", 2000)
    if not isinstance(identity, str) or (identity and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", identity)):
        raise ValueError("bot-wxid 须填写当前机器人微信的真实 wxid，不能填群 ID")
    if not isinstance(names, list) or len(names) > 20 or any(not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 for name in names):
        raise ValueError("mention-names 须为最多 20 个昵称的列表，每个昵称 1–80 字")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 100 <= limit <= 4000:
        raise ValueError("max-reply-chars 须为 100–4000 的整数")
    return ChatbotOptions(enabled, identity, tuple(dict.fromkeys(name.strip() for name in names)), limit)
