"""Small, in-memory callback diagnostics without message bodies or field values."""

from datetime import datetime, timezone
import logging
import re


logger = logging.getLogger(__name__)
KNOWN_FIELDS = {"event_type", "msgid", "type", "timestamp", "wxid", "sender", "roomid", "content", "data", "msg"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def value_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return "unknown"


def rejected_callback(app, exc, status: int) -> dict:
    fields = []
    for error in exc.errors()[:16]:
        path = [str(part) for part in error.get("loc", ()) if isinstance(part, str) and part in KNOWN_FIELDS]
        code = str(error.get("type", "unknown"))
        if not re.fullmatch(r"[a-z_]{1,64}", code):
            code = "unknown"
        fields.append({"field": ".".join(path) or "body", "error": code,
                       "input_type": "missing" if code == "missing" else value_type(error.get("input"))})
    shape = {key: value_type(value) for key, value in exc.body.items() if key in KNOWN_FIELDS} if isinstance(exc.body, dict) else {"body": value_type(exc.body)}
    result = {"received_at": utc_now(), "http_status": status, "fields": fields, "shape": shape}
    diagnostics = app.state.callback_status
    diagnostics["rejected"] += 1
    diagnostics["last_rejection"] = result
    logger.warning("Hook callback rejected (%s): %s", status, ", ".join(item["field"] + ":" + item["error"] for item in fields))
    return result
