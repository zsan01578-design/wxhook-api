"""Isolated FastAPI server for browser smoke tests; never connects to a real Hook."""

import os
from pathlib import Path
import sys


project = Path(__file__).resolve().parents[2]
root = project / ".tools" / "browser-smoke"
root.mkdir(parents=True, exist_ok=True)
(project / ".tools" / "project-audit").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(project / "src"))
os.environ["WXHOOK_CONFIG"] = str(project / "version.example.yml")
for name, filename in {
    "WXHOOK_MANAGEMENT_DB": "management.sqlite3",
    "WXHOOK_CHAT_MODEL_CONFIG": "model.json",
    "WXHOOK_IMAGE_MODEL_CONFIG": "image-model.json",
    "WXHOOK_VISION_MODEL_CONFIG": "vision-model.json",
    "WXHOOK_SEARCH_CONFIG": "search.json",
    "WXHOOK_CHATBOT_CONFIG": "chatbot.yml",
    "WXHOOK_DOUYIN_CONFIG": "media.yml",
    "WXHOOK_AI_USAGE_DB": "usage.sqlite3",
}.items():
    os.environ[name] = str(root / filename)
os.environ["WXHOOK_IMAGE_OUTPUT_DIR"] = str(root / "generated-images")
for name in ("WXHOOK_SYNC_ENABLED", "WXHOOK_MEDIA_CLEANUP_ENABLED", "WXHOOK_CHATBOT_ENABLED", "WXHOOK_DOUYIN_FEEDBACK_ENABLED", "WXHOOK_BACKUP_ENABLED"):
    os.environ[name] = "false"
os.environ.pop("WXHOOK_ADMIN_TOKEN", None)

import httpx
import uvicorn
from wxhook_api.main import create_app


def respond(request):
    if request.url.path == "/QueryDB/status":
        return httpx.Response(200, json={"IsLogin": 0})
    if request.url.path == "/GetSelfProfile":
        return httpx.Response(200, json={"wxid": "mock_robot", "nickname": "模拟机器人"})
    return httpx.Response(503, json={"ret": -1, "retmsg": "smoke mock: no real Hook"})


if __name__ == "__main__":
    uvicorn.run(create_app(hook_transport=httpx.MockTransport(respond)), host="127.0.0.1", port=8138, log_level="warning")
