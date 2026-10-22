import httpx
import pytest

from tests.test_chatbot import context
from tests.test_douyin import GROUP


@pytest.mark.parametrize("page,size", [(2**63, 1), (2**63 // 200 + 2, 200), (10**100, 50)])
async def test_large_message_page_returns_empty_page_without_database_overflow(tmp_path, page, size):
    async with context(tmp_path) as state:
        await state.app.state.groups.import_history(GROUP, [dict(
            msgid=1, timestamp=1700000000, sender="wxid_member", type=1, content="已归档消息",
        )])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=state.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/manage/groups/{GROUP}/messages", params={"page": page, "size": size},
            )
        assert response.status_code == 200
        assert response.json() == dict(messages=[], total=1, page=page, size=size, has_more=False)
        first = await state.client.get(f"/api/manage/groups/{GROUP}/messages")
        assert len(first.json()["messages"]) == 1


@pytest.mark.parametrize("source_id", [2**63, 10**100])
@pytest.mark.parametrize("method", ["POST", "PUT"])
async def test_task_source_outside_sqlite_integer_range_is_rejected(tmp_path, source_id, method):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        task_id = await tools.save_task(GROUP, dict(title="保留的待办", owner=""))
        before = (await tools.snapshot(GROUP))["tasks"]
        url = f"/api/manage/groups/{GROUP}/tools/tasks"
        if method == "PUT":
            url += "/" + task_id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=state.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.request(method, url, json=dict(title="新的标题", source_id=source_id))
        assert response.status_code == 422
        assert (await tools.snapshot(GROUP))["tasks"] == before


async def test_largest_sqlite_source_id_keeps_missing_source_validation(tmp_path):
    async with context(tmp_path) as state:
        response = await state.client.post(
            f"/api/manage/groups/{GROUP}/tools/tasks", json=dict(title="来源不存在", source_id=2**63 - 1),
        )
        assert response.status_code == 409
        assert not (await state.app.state.group_tools.snapshot(GROUP))["tasks"]
