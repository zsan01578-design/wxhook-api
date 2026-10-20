import asyncio

import pytest

from tests.test_chatbot import MEMBER, context
from tests.test_douyin import GROUP
from tests.test_review_regressions import keyword_rule


@pytest.mark.parametrize("fill_capacity", [False, True])
async def test_deleted_rule_cannot_be_recreated_by_inflight_update(tmp_path, monkeypatch, fill_capacity):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        await tools.stop()
        rule_id = await tools.save_rule(GROUP, keyword_rule())
        original = tools.save_rule
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(group, body, rule_id=None):
            entered.set()
            await release.wait()
            return await original(group, body, rule_id)

        monkeypatch.setattr(tools, "save_rule", delayed)
        pending = asyncio.create_task(state.client.put(
            f"/api/manage/groups/{GROUP}/tools/rules/{rule_id}", json=keyword_rule("new reply"),
        ))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert (await state.client.delete(
                f"/api/manage/groups/{GROUP}/tools/rules/{rule_id}",
            )).status_code == 200
            if fill_capacity:
                for index in range(50):
                    await original(GROUP, {**keyword_rule(), "name": f"replacement {index}"})
            release.set()
            response = await asyncio.wait_for(pending, 2)
            assert response.status_code == 404
            rules = (await tools.snapshot(GROUP))["rules"]
            assert len(rules) == (50 if fill_capacity else 0)
            assert not any(rule["id"] == rule_id for rule in rules)
            # A queued callback for the deleted rule must remain unable to reply.
            await tools._process_rule((GROUP, rule_id, "stale-callback", MEMBER, "weather"))
            assert not state.sends
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_rule_updates_are_scoped_to_existing_group_and_preserve_cooldown(tmp_path):
    async with context(tmp_path) as state:
        tools = state.app.state.group_tools
        rule_id = await tools.save_rule(GROUP, keyword_rule())
        await tools.db(lambda db: db.execute(
            "UPDATE keyword_rules SET last_triggered='2026-10-20T10:00:00+08:00' WHERE id=?", (rule_id,),
        ))
        assert await tools.save_rule("other@chatroom", keyword_rule("wrong group"), rule_id) is None
        assert (await tools.snapshot(GROUP))["rules"][0]["reply"] == "sunny"
        assert await tools.save_rule(GROUP, keyword_rule("updated"), rule_id) == rule_id
        rule = (await tools.snapshot(GROUP))["rules"][0]
        assert rule["reply"] == "updated" and rule["last_triggered"] == "2026-10-20T10:00:00+08:00"
