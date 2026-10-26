import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from tests.test_chatbot import MEMBER, context, event
from tests.test_douyin import GROUP, ITEM, PNG, SHORT, image_item, page
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.favorites import (FavoritesService, bookmark_url, is_favorite_command, query_terms, share_links)


OTHER = "other@chatroom"
ARTICLE = "https://example.com/recipes/braised-pork?x=1&y=2"


def endpoint(group=GROUP):
    return f"/api/manage/groups/{group}/favorites"


def media_response(request):
    if request.url.host == "v.douyin.com":
        return httpx.Response(302, headers={"location":f"https://www.douyin.com/note/{ITEM}"})
    if request.url.host == "www.douyin.com":
        item = image_item(2)
        item["desc"] = "红烧肉家常菜教程 #美食"
        return httpx.Response(200, text=page(item))
    return httpx.Response(200, content=PNG, headers={"content-type":"image/png"})


def configure_media(state, respond=media_response):
    state.media._transport = httpx.MockTransport(respond)


@pytest.mark.parametrize("text", ["收藏", f"收藏 {SHORT}", "收藏夹", "搜索收藏：红烧肉", "上次那个红烧肉教程在哪", "之前收藏的Python教程在哪里"])
def test_commands_are_recognized(text):
    assert is_favorite_command(text)


@pytest.mark.parametrize("text", ["我不想收藏这个", "收藏家喜欢什么", "帮我画一只猫", "今天天气如何"])
def test_unrelated_chat_is_not_intercepted(text):
    assert not is_favorite_command(text)


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "http://192.168.1.2/a", "https://user:secret@example.com/a", "https://example.com:9000/a", "javascript:alert(1)"])
def test_bookmark_validation_rejects_local_and_credential_urls(url):
    with pytest.raises(ValueError):
        bookmark_url(url)


def test_share_text_preserves_query_and_extracts_natural_subject():
    assert share_links(f"收藏 <url><![CDATA[{ARTICLE.replace('&','&amp;')}]]></url>") == [ARTICLE]
    assert "红烧肉" in query_terms("上次那个红烧肉教程在哪")


async def test_collect_gallery_download_and_persistence_without_ephemeral_media(tmp_path):
    async with context(tmp_path) as state:
        configure_media(state)
        worker = state.app.state.favorites
        result = await worker.collect(GROUP, SHORT, sender=MEMBER)
        item = result["item"]
        assert item["status"] == "saved" and item["kind"] == "images"
        assert item["title"] == "红烧肉家常菜教程 #美食"
        assert "美食" in item["tags"]
        assert [file["name"] for file in item["files"]] == ["001.png","002.png"]
        assert worker.root != state.media.options.output_dir
        assert (worker.root / item["id"] / "001.png").read_bytes() == PNG
        assert "红烧肉家常菜教程" in (await worker.get(GROUP,item["id"]))["source_text"]
        assert not list(state.media.options.output_dir.glob("*/result.json"))
        reloaded = FavoritesService(state.app.state.group_tools, state.media)
        await reloaded.start()
        assert (await reloaded.get(GROUP,item["id"]))["files"] == item["files"]
        response = await state.client.get(f"{endpoint()}/{item['id']}/files/001.png")
        assert response.status_code == 200 and response.content == PNG
        assert not worker._leases
        await state.app.state.groups.add_group(OTHER,"另一个群")
        assert (await state.client.get(f"{endpoint(OTHER)}/{item['id']}/files/001.png")).status_code == 404
        assert not (await worker.search(OTHER))["items"]


async def test_duplicate_bookmark_edit_search_and_group_isolation(tmp_path):
    async with context(tmp_path) as state:
        worker = state.app.state.favorites
        def unexpected(request):
            pytest.fail("Generic article bookmarks must not make network requests")
        configure_media(state, unexpected)
        item = (await worker.collect(GROUP, ARTICLE, title="私房红烧肉", notes="下次聚餐试试", tags=["菜谱"]))["item"]
        duplicate = await worker.collect(GROUP, ARTICLE, title="不应覆盖标题")
        assert duplicate["duplicate"] and duplicate["item"]["id"] == item["id"]
        assert duplicate["item"]["title"] == "私房红烧肉"
        assert (await worker.search(GROUP,"上次那个红烧肉教程在哪"))["items"][0]["id"] == item["id"]
        assert await worker.update(GROUP,item["id"],"生日聚餐菜谱",["聚餐"],"红烧肉")
        assert (await worker.search(GROUP,"红烧肉"))["total"] == 1
        assert not (await worker.search(OTHER,"红烧肉"))["items"]
        assert not await worker.update(OTHER,item["id"],"不能跨群",[],"")
        assert not await worker.delete(OTHER,item["id"])


async def test_chat_save_without_mention_and_deduplicated_reply(tmp_path):
    async with context(tmp_path) as state:
        configure_media(state)
        payload = event(f"收藏 {SHORT} a@a.aN")
        await state.submit(payload)
        await state.submit(payload)
        assert len(state.sends) == 1 and "已加入本群收藏" in state.sends[0]["msg"]
        assert (await state.app.state.favorites.search(GROUP))["total"] == 1
        await state.submit(event("上次那个红烧肉教程在哪",2))
        assert len(state.sends) == 2
        assert "红烧肉" in state.sends[1]["msg"] and SHORT in state.sends[1]["msg"]
        assert state.media.snapshot()["jobs"] == []


async def test_save_previous_own_share_not_another_members(tmp_path):
    async with context(tmp_path) as state:
        await state.submit(event(ARTICLE,1,sender="other_member"))
        await state.submit(event("收藏",2))
        assert "请先发送分享链接" in state.sends[-1]["msg"]
        await state.submit(event(ARTICLE,3))
        await state.submit(event("收藏",4))
        assert "已加入本群收藏" in state.sends[-1]["msg"]
        assert (await state.app.state.favorites.search(GROUP))["total"] == 1


async def test_recent_share_cannot_be_taken_from_after_the_command(tmp_path):
    async with context(tmp_path) as state:
        await state.app.state.groups.receive(CallbackEvent.model_validate(event("收藏",1)))
        await state.app.state.groups.receive(CallbackEvent.model_validate(event(ARTICLE,2)))
        assert not await state.app.state.favorites.recent_source(GROUP,MEMBER,"msg:1")


async def test_failed_download_is_honest_and_retry_then_delete(tmp_path):
    async with context(tmp_path) as state:
        def bad(request):
            if request.url.host == "p3.douyinpic.com":
                return httpx.Response(200,text="<html>not an image</html>")
            return media_response(request)
        configure_media(state,bad)
        worker = state.app.state.favorites
        item = (await worker.collect(GROUP,SHORT))["item"]
        assert item["status"] == "partial" and item["error"] and not item["files"]
        await worker.update(GROUP,item["id"],"我的私房菜谱",["周末"],"已经编辑的备注")
        assert not list(worker.root.glob("*/*.part"))
        configure_media(state)
        response = await state.client.post(f"{endpoint()}/{item['id']}/retry",json={})
        assert response.status_code == 200, response.text
        assert response.json()["item"]["status"] == "saved"
        assert len(response.json()["item"]["files"]) == 2
        assert response.json()["item"]["title"] == "我的私房菜谱"
        assert response.json()["item"]["notes"] == "已经编辑的备注"
        response = await state.client.delete(f"{endpoint()}/{item['id']}")
        assert response.status_code == 200
        assert not (worker.root/item["id"]).exists()
        assert await worker.get(GROUP,item["id"]) is None


async def test_size_limit_and_untrusted_redirect_keep_link_only(tmp_path):
    async with context(tmp_path) as state:
        configure_media(state)
        state.media.options = replace(state.media.options,max_file_bytes=10)
        worker = state.app.state.favorites
        item = (await worker.collect(GROUP,SHORT))["item"]
        assert item["status"] == "partial" and not item["files"]
        calls = []
        def redirect(request):
            calls.append(request.url.host)
            if request.url.host == "p3.douyinpic.com":
                return httpx.Response(302,headers={"location":"http://127.0.0.1:30001/GetDecodeKey"})
            return media_response(request)
        configure_media(state,redirect)
        state.media.options = replace(state.media.options,max_file_bytes=1024)
        await worker.collect(GROUP,SHORT,retry_id=item["id"])
        assert "127.0.0.1" not in calls


async def test_collection_api_validation_pagination_and_cross_group_delete(tmp_path):
    async with context(tmp_path) as state:
        response = await state.client.post(endpoint(),json={"text":ARTICLE,"title":"红烧肉","tags":["美食"]})
        assert response.status_code == 200, response.text
        item_id = response.json()["item"]["id"]
        assert (await state.client.get(endpoint()+"?page=0")).status_code == 422
        assert (await state.client.get(endpoint(OTHER))).status_code == 404
        assert (await state.client.post(endpoint(),json={"text":"http://127.0.0.1/a"})).status_code == 409
        assert (await state.client.put(f"{endpoint()}/{item_id}",json={"title":"新标题","tags":["a"*25]})).status_code == 422
        assert (await state.client.get(endpoint()+"?q=红烧肉")).json()["total"] == 1
        await state.app.state.groups.update_group(GROUP,{"blocked":True})
        assert (await state.client.delete(f"{endpoint()}/{item_id}")).status_code == 409


async def test_ai_query_expansion_only_receives_question_and_falls_back_on_error(tmp_path):
    async with context(tmp_path) as state:
        worker = state.app.state.favorites
        await worker.collect(GROUP,ARTICLE,title="红烧肉")
        class Models:
            settings = True
            calls = []
            async def answer(self, question, **kwargs):
                self.calls.append((question,kwargs))
                return json.dumps(["红烧肉"])
        worker.models = Models()
        result = await worker.search(GROUP,"找一下做菜的方法",semantic=True,sender=MEMBER)
        assert result["total"] == 1 and result["search_mode"] == "semantic"
        assert worker.models.calls[0][0] == "找一下做菜的方法"
        assert ARTICLE not in str(worker.models.calls)
        worker.models.answer = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad model"))
        assert (await worker.search(GROUP,"红烧肉",semantic=True))["total"] == 1


async def test_cancelled_download_preserves_partial_record_and_releases_slots(tmp_path):
    async with context(tmp_path) as state:
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(request):
            if request.url.host == "p3.douyinpic.com":
                entered.set()
                await release.wait()
            return media_response(request)
        configure_media(state,delayed)
        worker = state.app.state.favorites
        task = asyncio.create_task(worker.collect(GROUP,SHORT))
        await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await worker.search(GROUP)
        assert result["items"][0]["status"] == "partial"
        assert not worker._active and worker._slots._value == 2


async def test_file_lease_blocks_delete_and_survives_other_group_lookup(tmp_path):
    async with context(tmp_path) as state:
        configure_media(state)
        worker = state.app.state.favorites
        item = (await worker.collect(GROUP,SHORT))["item"]
        assert await worker.acquire_file(GROUP,item["id"],"001.png")
        with pytest.raises(ValueError):
            await worker.delete(GROUP,item["id"])
        worker.release_file(item["id"])
        assert await worker.delete(GROUP,item["id"])


async def test_custom_media_title_and_complete_caption_are_both_saved(tmp_path):
    async with context(tmp_path) as state:
        configure_media(state)
        worker = state.app.state.favorites
        item = (await worker.collect(GROUP,SHORT,title="周末做这道菜"))["item"]
        stored = await worker.get(GROUP,item["id"])
        assert stored["title"] == "周末做这道菜"
        assert "红烧肉家常菜教程" in stored["source_text"]


async def test_cancellation_at_reservation_does_not_leave_saving_card(tmp_path,monkeypatch):
    async with context(tmp_path) as state:
        worker = state.app.state.favorites
        committed, hold = asyncio.Event(), asyncio.Event()
        original = worker.tools.db
        async def delayed(action):
            result = await original(action)
            if action.__name__ == "reserve":
                committed.set()
                await hold.wait()
            return result
        monkeypatch.setattr(worker.tools,"db",delayed)
        task = asyncio.create_task(worker.collect(GROUP,ARTICLE))
        await asyncio.wait_for(committed.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await worker.search(GROUP))["items"][0]["status"] == "partial"
        assert not worker._active
