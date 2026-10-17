from datetime import datetime, timedelta, timezone

import httpx
from asgi_lifespan import LifespanManager

from tests.test_douyin import GROUP
from tests.test_manage import make_service
from wxhook_api.main import create_app
from wxhook_api.services.groups import GroupManager


async def test_radar_counts_china_days_members_types_and_filters_bot_and_other_groups(tmp_path):
    manager = GroupManager(tmp_path / "radar.sqlite3")
    await manager.start([])
    await manager.claim_account("wxid_robot")
    china = timezone(timedelta(hours=8))
    midnight = datetime.now(china).replace(hour=0, minute=0, second=0, microsecond=0)
    today = int(midnight.timestamp())
    await manager.import_history(GROUP, [
        {"msgid": 1, "timestamp": today, "sender": "wxid_a", "type": 1, "content": "苹果 苹果"},
        {"msgid": 2, "timestamp": today - 1, "sender": "wxid_a", "type": 3, "content": "图片"},
        {"msgid": 3, "timestamp": today - 14 * 86400, "sender": "wxid_old", "type": 1, "content": "旧词"},
        {"msgid": 4, "timestamp": today + 30, "sender": "wxid_robot", "type": 1, "content": "机器人"},
        {"msgid": 5, "timestamp": today + 40, "sender": "wxid_flagged", "type": 1, "content": "机器人", "is_self": True},
    ] + [
        {"msgid": 10 + i, "timestamp": today + 60, "sender": f"wxid_{i}", "type": 1, "content": "苹果"}
        for i in range(12)
    ])
    await manager.import_history("other@chatroom", [{"msgid": 1, "timestamp": today, "sender": "wxid_other", "type": 1, "content": "其他群"}])
    radar = await manager.radar(GROUP)
    assert radar["messages"] == 14 and radar["text_messages"] == 13
    assert radar["active_members"] == 13, "Count all members, not only the top ten"
    assert len(radar["top_members"]) == 10 and radar["top_members"][0]["id"] == "wxid_a"
    assert radar["daily"][-1]["count"] == 13 and radar["daily"][-2]["count"] == 1
    assert radar["hourly"][0]["count"] == 13 and radar["hourly"][23]["count"] == 1
    assert radar["peak_hour"] == {"hour": 0, "count": 13}
    assert radar["words"][0] == {"text": "苹果", "count": 14}
    assert len(radar["daily"]) == 14


async def test_radar_api_is_local_empty_and_validates_window(tmp_path):
    def no_network(request):
        raise AssertionError("Radar must stay local")

    app = create_app(douyin_service=make_service(tmp_path), hook_transport=httpx.MockTransport(no_network))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/manage/groups/{GROUP}/radar?days=7")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        data = response.json()
        assert data["messages"] == 0 and data["active_members"] == 0
        assert len(data["daily"]) == 7 and len(data["hourly"]) == 24
        assert data["top_members"] == [] and data["words"] == []
        for days in (0, 31):
            assert (await client.get(f"/api/manage/groups/{GROUP}/radar?days={days}")).status_code == 422
