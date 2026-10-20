from datetime import datetime

import pytest

from tests.test_chatbot import MEMBER, context
from tests.test_douyin import GROUP
from wxhook_api.services.chat_summary import CHINA


@pytest.mark.parametrize("existing_status", ["open", "done", "cancelled"])
async def test_editing_ai_task_to_duplicate_title_returns_conflict_without_changes(tmp_path, existing_status):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.groups.import_history(GROUP, [dict(
            msgid=1, timestamp=int(datetime.now(CHINA).timestamp()), sender=MEMBER,
            type=1, content="准备报告并安排会议",
        )])
        source = await tools.db(lambda db: db.execute(
            "SELECT id FROM chat_messages WHERE group_id=?", (GROUP,)
        ).fetchone()[0])
        first = await tools.save_task(GROUP, dict(title="准备报告", owner="小王", source_id=source))
        second = await tools.save_task(GROUP, dict(title="安排会议", owner="小李", source_id=source))
        await tools.task_status(GROUP, first, existing_status)
        before = (await tools.snapshot(GROUP))["tasks"]
        response = await state.client.put(
            f"/api/manage/groups/{GROUP}/tools/tasks/{second}",
            json=dict(title="准备报告", owner="新负责人", due_at="2027-01-01T12:00:00+08:00"),
        )
        assert response.status_code == 409
        assert "相同标题" in response.json()["error"]["message"]
        assert (await tools.snapshot(GROUP))["tasks"] == before
        # A distinct title remains editable, preserving the original source.
        response = await state.client.put(
            f"/api/manage/groups/{GROUP}/tools/tasks/{second}",
            json=dict(title="安排项目会议", owner="小李"),
        )
        assert response.status_code == 200
        updated = next(task for task in (await tools.snapshot(GROUP))["tasks"] if task["id"] == second)
        assert updated["title"] == "安排项目会议" and updated["source_id"] == source
