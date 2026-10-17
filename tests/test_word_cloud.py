import httpx
import subprocess
import sys
from asgi_lifespan import LifespanManager

from tests.test_chatbot import event
from tests.test_manage import make_service
from tests.test_douyin import GROUP
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.word_cloud import frequent_words


def test_chinese_and_english_frequencies_exclude_metadata_links_mentions_and_noise():
    words = frequent_words([
        "wxid_member:\n@小明\u2005今天讨论模型，模型测试！Python python 的 了 我们 谢谢 [微笑]",
        "模型 https://example.com/secretWord wxid_abc123 12345 99999@chatroom",
        "<msg><appmsg>模型 XMLMetadata</appmsg></msg>",
    ])
    counts = {word["text"]: word["count"] for word in words}
    assert counts["模型"] == 3 and counts["python"] == 2
    assert counts["测试"] == 1
    assert not ({"小明", "微笑", "我们", "谢谢", "secretword", "wxid", "xmlmetadata", "appmsg"} & counts.keys())
    assert frequent_words(["的 了 啊 [微笑] 123 https://test.com"]) == []


async def test_group_word_cloud_api_is_local_scoped_and_updates_after_new_and_corrected_history(tmp_path):
    def no_network(request):
        raise AssertionError("Word cloud must not call Hook or model APIs")

    app = create_app(douyin_service=make_service(tmp_path), hook_transport=httpx.MockTransport(no_network))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        manager = app.state.groups
        for i, (group, kind, text) in enumerate([
            (GROUP, 1, "苹果 苹果 香蕉"), (GROUP, 49, "视频 视频"),
            ("other@chatroom", 1, "西瓜 西瓜"), (GROUP, 10000, "系统消息"),
        ], 1):
            await manager.receive(CallbackEvent.model_validate(event(text, msgid=i, roomid=group, wxid=group, type=kind)))
        url = f"/api/manage/groups/{GROUP}/word-cloud"
        response = await client.get(url)
        assert response.headers["cache-control"] == "no-store"
        first = response.json()
        assert first["sampled_messages"] == 1 and first["message_limit"] == 1000
        assert first["words"] == [{"text": "苹果", "count": 2}, {"text": "香蕉", "count": 1}]
        assert (await client.get(url)).json() == first
        await manager.receive(CallbackEvent.model_validate(event("香蕉 香蕉", msgid=5)))
        assert (await client.get(url)).json()["words"][0] == {"text": "香蕉", "count": 3}
        await manager.import_history(GROUP, [{"msgid": 5, "type": 1, "content": "苹果 苹果 苹果", "timestamp": 20}])
        assert (await client.get(url)).json()["words"][0] == {"text": "苹果", "count": 5}
        empty = (await client.get("/api/manage/groups/empty@chatroom/word-cloud")).json()
        assert empty["words"] == [] and empty["sampled_messages"] == 0


async def test_word_cloud_uses_latest_thousand_text_messages_and_limits_words(tmp_path):
    manager = GroupManager(tmp_path / "word-cloud.sqlite3")
    await manager.start([])
    rows = [{"msgid": i, "timestamp": i, "type": 1, "content": "旧词" if i == 1 else "新词"} for i in range(1, 1002)]
    await manager.import_history(GROUP, rows)
    data = await manager.word_cloud(GROUP)
    assert data["sampled_messages"] == 1000
    assert data["words"] == [{"text": "新词", "count": 1000}]
    assert len(frequent_words([" ".join(f"word{n}" for n in range(100))])) == 40


async def test_robot_messages_excluded_from_api_even_when_chatbot_is_disabled(tmp_path):
    from wxhook_api.core.chatbot_config import ChatbotOptions
    from wxhook_api.services.chatbot import MentionChatService

    app = create_app(douyin_service=make_service(tmp_path),
                     chat_service=MentionChatService(ChatbotOptions(False, "wxid_robot")))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        manager = app.state.groups
        await manager.import_history(GROUP, [
            {"msgid": 1, "timestamp": 1, "type": 1, "content": "苹果 苹果", "sender": "wxid_member"},
            {"msgid": 2, "timestamp": 2, "type": 1, "content": "香蕉 香蕉", "sender": "wxid_robot"},
        ])
        await manager.receive(CallbackEvent.model_validate(event("西瓜", msgid=3, sender="wxid_robot")))
        await manager.receive(CallbackEvent.model_validate(event("葡萄", msgid=4, sender="", is_self=True)))
        result = (await client.get(f"/api/manage/groups/{GROUP}/word-cloud")).json()
        assert result["sampled_messages"] == 1
        assert result["words"] == [{"text": "苹果", "count": 2}]
        assert (await manager.messages(GROUP))["total"] == 4, "Robot messages remain in chat history"


async def test_identity_discovery_invalidates_word_cloud_and_persisted_account_survives_restart(tmp_path):
    path = tmp_path / "cloud.sqlite3"
    manager = GroupManager(path)
    await manager.start([])
    await manager.import_history(GROUP, [
        {"msgid": 1, "timestamp": 1, "type": 1, "content": "苹果", "sender": "wxid_member"},
        {"msgid": 2, "timestamp": 2, "type": 1, "content": "香蕉", "sender": "wxid_robot"},
    ])
    assert (await manager.word_cloud(GROUP))["sampled_messages"] == 2
    await manager.claim_account("wxid_robot")
    result = await manager.word_cloud(GROUP)
    assert result["sampled_messages"] == 1 and result["words"] == [{"text": "苹果", "count": 1}]
    restarted = GroupManager(path)
    await restarted.start([])
    assert await restarted.word_cloud(GROUP) == result


async def test_robot_messages_do_not_consume_the_thousand_member_message_limit(tmp_path):
    manager = GroupManager(tmp_path / "limit.sqlite3")
    await manager.start([])
    await manager.claim_account("wxid_robot")
    await manager.import_history(GROUP, [
        {"msgid": i, "timestamp": i, "type": 1, "sender": "wxid_member", "content": "苹果"}
        for i in range(1, 1001)
    ] + [
        {"msgid": i, "timestamp": i, "type": 1, "sender": "wxid_robot", "content": "香蕉"}
        for i in range(1001, 2101)
    ])
    # Older callbacks can identify self messages only through a sender prefix.
    await manager.receive(CallbackEvent.model_validate(event("wxid_robot:\n西瓜", msgid=2200, sender="")))
    result = await manager.word_cloud(GROUP)
    assert result["sampled_messages"] == 1000
    assert result["words"] == [{"text": "苹果", "count": 1000}]


def test_app_import_and_creation_do_not_require_jieba():
    result = subprocess.run([sys.executable, "-c",
        "import sys; sys.modules['jieba'] = None; from wxhook_api.main import create_app; create_app()"],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


async def test_missing_jieba_uses_offline_fallback_and_keeps_management_available(tmp_path, monkeypatch):
    def no_network(request):
        raise AssertionError("No network needed for management or word cloud")

    with monkeypatch.context() as missing:
        missing.setitem(sys.modules, "jieba", None)
        app = create_app(douyin_service=make_service(tmp_path), hook_transport=httpx.MockTransport(no_network))
        async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await app.state.groups.receive(CallbackEvent.model_validate(event("苹果 苹果", msgid=1)))
            cloud = await client.get(f"/api/manage/groups/{GROUP}/word-cloud")
            assert cloud.status_code == 200 and cloud.headers["cache-control"] == "no-store"
            assert cloud.json()["words"] == [{"text": "苹果", "count": 2}]
            for url in ("/", "/api/manage/state", "/api/manage/chat-model", f"/api/manage/groups/{GROUP}/messages"):
                assert (await client.get(url)).status_code == 200
    result = await app.state.groups.word_cloud(GROUP)
    assert result["words"] == [{"text": "苹果", "count": 2}]
