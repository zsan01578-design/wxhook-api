import asyncio
import httpx
from asgi_lifespan import LifespanManager
import pytest

from tests.test_callbacks import EVENT
from tests.test_douyin import GROUP, ITEM, PNG, SHORT, image_item, page, profile_transport
from wxhook_api.core.douyin_config import DouyinOptions, GroupSettings
from wxhook_api.main import create_app
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.groups import GroupManager


OTHER = "other@chatroom"


def make_service(tmp_path, transport=None, **values):
    settings = GroupSettings(**{"enabled": True, "groups": [GROUP, OTHER], "parser_provider": "builtin", **values})
    return DouyinService(DouyinOptions(settings, tmp_path / "douyin.yml", tmp_path / "downloads"), transport=transport)


def event(group=GROUP, sender="wxid_member", msgid=1):
    return CallbackEvent.model_validate({**EVENT, "roomid": group, "sender": sender, "content": SHORT, "msgid": msgid})


async def test_management_pages_and_state_reuse_legacy_groups_without_hook_calls(tmp_path):
    def no_network(request):
        pytest.fail("state and pages must work offline")

    app = create_app(douyin_service=make_service(tmp_path), hook_transport=httpx.MockTransport(no_network))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for url in ("/", "/manage", "/messages", "/ui/assets/manage.css", "/ui/assets/manage.js"):
            response = await client.get(url)
            assert response.status_code == 200
        response = await client.get("/api/manage/state")
        assert response.headers["cache-control"] == "no-store"
        assert {group["id"] for group in response.json()["groups"]} == {GROUP, OTHER}
        assert all(group["douyin_enabled"] for group in response.json()["groups"])


async def test_per_group_overrides_master_switch_and_legacy_defaults(tmp_path):
    service = make_service(tmp_path, enabled=False)
    app = create_app(douyin_service=service, hook_transport=profile_transport())
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert not service.allowed(GROUP)
        assert (await client.patch(f"/api/manage/groups/{GROUP}", json={"douyin_enabled": True, "auto_download": False})).status_code == 200
        assert service.allowed(GROUP)
        assert not service.allowed(OTHER)
        assert not service.auto_download(GROUP)
        assert service.auto_download(OTHER)
        await client.patch(f"/api/manage/groups/{GROUP}", json={"enabled": False})
        assert not service.allowed(GROUP)
        await client.patch(f"/api/manage/groups/{GROUP}", json={"enabled": True})
        assert service.allowed(GROUP)
        await service.configure(GroupSettings(enabled=False, groups=[]))
        assert service.allowed(GROUP), "explicit group settings take precedence over legacy defaults"


async def test_member_blacklist_scoped_to_group_and_duplicate_add_preserves_policy(tmp_path):
    service = make_service(tmp_path)
    app = create_app(douyin_service=service, hook_transport=profile_transport())
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for group in (GROUP, OTHER):
            await app.state.groups.receive(event(group))
        path = f"/api/manage/groups/{GROUP}/members/wxid_member"
        await client.patch(path, json={"blocked": True, "name": "成员备注"})
        assert not service.allowed(GROUP, "wxid_member")
        assert service.allowed(OTHER, "wxid_member")
        assert service.allowed(GROUP, "wxid_someone_else")
        await client.post(f"/api/manage/groups/{GROUP}/members", json={"id": "wxid_member"})
        row = (await client.get(f"/api/manage/groups/{GROUP}/members")).json()["members"][0]
        assert row["blocked"] and row["name"] == "成员备注"
        state = (await client.get("/api/manage/state")).json()
        assert len(state["blocked_members"]) == 1
        await client.patch(path, json={"blocked": False})
        assert service.allowed(GROUP, "wxid_member")


async def test_group_and_member_policies_and_audit_survive_restart(tmp_path):
    database = tmp_path / "persistent.sqlite3"
    app = create_app(group_manager=GroupManager(database), douyin_service=make_service(tmp_path))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.patch(f"/api/manage/groups/{GROUP}", json={"name": "产品交流", "note": "工作群", "pinned": True, "blocked": True})
        await client.post(f"/api/manage/groups/{GROUP}/members", json={"id": "wxid_member", "blocked": True})
    second = create_app(group_manager=GroupManager(database), douyin_service=make_service(tmp_path))
    async with LifespanManager(second), httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://test") as client:
        data = (await client.get("/api/manage/state")).json()
        row = data["groups"][0]
        assert row["id"] == GROUP and row["name"] == "产品交流" and row["note"] == "工作群"
        assert row["pinned"] and row["blocked"] and not row["active"]
        assert len(data["blocked_members"]) == 1
        assert not second.state.douyin.allowed(GROUP, "wxid_member")
        events = (await client.get(f"/api/manage/audit?group={GROUP}")).json()["events"]
        assert any(row["action"] == "拉黑群成员" for row in events)


@pytest.mark.parametrize("path,body,method,status", [
    ("/groups", {"id": "群昵称"}, "post", 422),
    ("/groups", {"id": "abc@chatroom", "unknown": True}, "post", 422),
    (f"/groups/{GROUP}", {"name": "x" * 81}, "patch", 422),
    (f"/groups/{GROUP}", {"blocked": None}, "patch", 422),
    (f"/groups/{GROUP}/members", {"id": OTHER}, "post", 422),
    ("/groups/missing@chatroom", {"blocked": True}, "patch", 404),
])
async def test_invalid_writes_are_rejected_without_changing_policy(tmp_path, path, body, method, status):
    service = make_service(tmp_path)
    app = create_app(douyin_service=service, hook_transport=profile_transport())
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await getattr(client, method)("/api/manage" + path, json=body)
        assert result.status_code == status
        assert service.allowed(GROUP)
        assert (await client.get("/api/manage/audit")).json()["events"] == []


async def test_callbacks_discover_groups_and_members_and_preserve_local_names(tmp_path):
    app = create_app(douyin_service=make_service(tmp_path, enabled=False))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = {**EVENT, "roomid": "new@chatroom", "sender": "wxid_new", "content": "<script>alert(1)</script>"}
        await client.post("/receiveChatBotMsg", json=payload)
        await app.state.event_dispatcher._queue.join()
        await client.patch("/api/manage/groups/new@chatroom", json={"name": "本地备注"})
        await client.post("/receiveChatBotMsg", json=payload)
        await app.state.event_dispatcher._queue.join()
        group = next(row for row in (await client.get("/api/manage/state")).json()["groups"] if row["id"] == "new@chatroom")
        assert group["member_count"] == 1 and group["message_count"] == 1
        assert group["name"] == "本地备注" and group["last_message"] == payload["content"]
        assert not group["douyin_enabled"]


async def test_blacklisting_stops_inflight_parse_and_queued_member_jobs(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request.url.host)
        if request.url.host == "v.douyin.com":
            started.set()
            await release.wait()
            return httpx.Response(302, headers={"location": f"https://www.douyin.com/note/{ITEM}"})
        if request.url.host == "www.douyin.com":
            return httpx.Response(200, text=page(image_item(1)))
        pytest.fail("blacklisted member must not download any media")

    service = make_service(tmp_path, httpx.MockTransport(respond))
    manager = GroupManager(tmp_path / "policies.sqlite3")
    await manager.start([GROUP])
    service.group_manager = manager
    await manager.save_member(GROUP, "wxid_member", {}, create=True)
    await service.start()
    try:
        await service.receive(event(msgid=1))
        await asyncio.wait_for(started.wait(), 1)
        await service.receive(event(msgid=2))
        await manager.save_member(GROUP, "wxid_member", {"blocked": True})
        release.set()
        await asyncio.wait_for(service._queue.join(), 2)
        assert len(service.snapshot()["jobs"]) == 2
        assert all(job["status"] == "failed" and not job["files"] for job in service.snapshot()["jobs"])
        assert calls.count("v.douyin.com") == 1
        await service.receive(event(msgid=3))
        assert len(service.snapshot()["jobs"]) == 2
    finally:
        release.set()
        await service.stop()


async def test_different_groups_really_use_independent_download_settings(tmp_path):
    def respond(request):
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": f"https://www.douyin.com/note/{ITEM}"})
        if request.url.host == "www.douyin.com":
            return httpx.Response(200, text=page(image_item(1)))
        return httpx.Response(200, content=PNG)

    service = make_service(tmp_path, httpx.MockTransport(respond))
    app = create_app(douyin_service=service, hook_transport=profile_transport())
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.patch(f"/api/manage/groups/{GROUP}", json={"auto_download": False})
        await service.receive(event(GROUP))
        await service.receive(event(OTHER))
        await asyncio.wait_for(service._queue.join(), 2)
        jobs = {job["group_id"]: job for job in service.snapshot()["jobs"]}
        assert jobs[GROUP]["status"] == "parsed" and not jobs[GROUP]["files"]
        assert jobs[OTHER]["status"] == "completed" and len(jobs[OTHER]["files"]) == 1


async def test_blocking_member_mid_download_removes_partial_and_skips_mirrors(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    media_calls = []

    class SlowMedia(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield PNG + b"\0" * (65536 - len(PNG))
            started.set()
            await release.wait()
            yield b"\0" * 65536

    def respond(request):
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": f"https://www.douyin.com/note/{ITEM}"})
        if request.url.host == "www.douyin.com":
            item = image_item(1)
            item["images"][0]["url_list"].append("https://p3.douyinpic.com/mirror.png")
            return httpx.Response(200, text=page(item))
        media_calls.append(str(request.url))
        return httpx.Response(200, stream=SlowMedia())

    service = make_service(tmp_path, httpx.MockTransport(respond))
    app = create_app(douyin_service=service, hook_transport=profile_transport())
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await app.state.groups.receive(event())
        await service.receive(event())
        await asyncio.wait_for(started.wait(), 1)
        await client.patch(f"/api/manage/groups/{GROUP}/members/wxid_member", json={"blocked": True})
        release.set()
        await asyncio.wait_for(service._queue.join(), 2)
        job = service.snapshot()["jobs"][0]
        assert job["status"] == "failed" and not job["files"]
        assert len(media_calls) == 1
        assert not list(service.options.output_dir.glob("*/*.part"))


async def test_contact_search_import_uses_documented_hook_response(tmp_path):
    def respond(request):
        assert request.url.path == "/Contact/search"
        return httpx.Response(200, json={"status": 0, "data": [{"username": GROUP, "nick_name": "群名称", "remark": "本地群"}, {"username": "wxid_person"}]})

    app = create_app(douyin_service=make_service(tmp_path), hook_transport=httpx.MockTransport(respond))
    async with LifespanManager(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        data = (await client.get("/api/manage/contacts", params={"keyword": "产品"})).json()
        assert data == {"groups": [{"id": GROUP, "name": "本地群"}]}
