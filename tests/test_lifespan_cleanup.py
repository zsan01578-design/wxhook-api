import asyncio
from dataclasses import dataclass

import httpx
import pytest

from tests.test_chatbot import BOT
from tests.test_douyin import options
from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.chatbot_config import ChatbotOptions
from wxhook_api.main import create_app
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.wechat_sync import SyncOptions


STAGES = ["sync", "dispatcher", "tools", "summary", "chatbot", "media", "hook"]


def no_hook_calls(request):
    raise AssertionError("Lifecycle tests must not call the Hook")


def make_app(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("WXHOOK_DOUYIN_FEEDBACK_ENABLED", "true")
    media = DouyinService(options(tmp_path), transport=httpx.MockTransport(no_hook_calls))
    bot = MentionChatService(ChatbotOptions(True, BOT, ("机器人",)))
    return create_app(
        douyin_service=media, chat_service=bot,
        group_manager=GroupManager(tmp_path / "management.sqlite3"),
        sync_options=SyncOptions(enabled=False),
        hook_transport=httpx.MockTransport(no_hook_calls), **kwargs,
    )


def stages(app):
    return [
        ("sync", app.state.wechat_sync, "stop", ()),
        ("dispatcher", app.state.event_dispatcher, "stop", (app.state.settings.shutdown_timeout,)),
        ("tools", app.state.group_tools, "stop", ()),
        ("summary", app.state.chat_summary, "stop", ()),
        ("chatbot", app.state.chatbot, "stop", ()),
        ("media", app.state.douyin, "stop", ()),
        ("hook", app.state.hook_client, "aclose", ()),
    ]


def workers(app):
    return [task for task in [
        app.state.wechat_sync._task, app.state.event_dispatcher._worker,
        app.state.group_tools._worker, app.state.group_tools._scheduler,
        app.state.group_tools.reminders._worker, app.state.chat_summary._task,
        *app.state.chatbot._workers, app.state.douyin._worker,
        app.state.douyin._notice_worker, app.state.douyin._cleanup_worker,
    ] if task is not None]


@dataclass
class RunningApp:
    app: object
    lifespan: object
    tasks: list
    media_client: object
    hook_client: object

    def assert_closed(self):
        assert all(task.done() for task in self.tasks)
        assert self.media_client.is_closed
        assert self.hook_client._client.is_closed
        assert self.app.state.douyin.messenger is None


@pytest.fixture
async def running_app(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    originals = [(getattr(service, method), args) for _, service, method, args in stages(app)]
    state = RunningApp(app, lifespan, workers(app), app.state.douyin._client, app.state.hook_client)
    try:
        yield state
    finally:
        # Keep the pre-fix failure cases from leaking their real test workers.
        for stop, args in originals:
            await stop(*args)
        await lifespan.__aexit__(None, None, None)


@pytest.mark.parametrize("failure", STAGES)
async def test_shutdown_failure_still_closes_all_other_services(running_app, monkeypatch, failure):
    trace = []
    for name, service, method, _ in stages(running_app.app):
        original = getattr(service, method)

        async def stop(*args, name=name, original=original):
            trace.append(name)
            await original(*args)
            if name == failure:
                raise RuntimeError("synthetic stop failure: " + name)

        monkeypatch.setattr(service, method, stop)

    with pytest.raises(RuntimeError, match="synthetic stop failure: " + failure):
        await running_app.lifespan.__aexit__(None, None, None)
    assert trace == STAGES
    running_app.assert_closed()


@pytest.mark.parametrize("interrupted", STAGES)
async def test_repeated_shutdown_cancellation_waits_for_all_cleanup(running_app, monkeypatch, interrupted):
    trace = []
    entered, release = asyncio.Event(), asyncio.Event()
    for name, service, method, _ in stages(running_app.app):
        original = getattr(service, method)

        async def stop(*args, name=name, original=original):
            trace.append(name)
            if name == interrupted:
                entered.set()
                await release.wait()
            await original(*args)

        monkeypatch.setattr(service, method, stop)

    pending = asyncio.create_task(running_app.lifespan.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert trace == STAGES
        running_app.assert_closed()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_normal_shutdown_stops_workers_before_closing_owned_hook(running_app, monkeypatch):
    original = running_app.hook_client.aclose

    async def close():
        assert all(task.done() for task in running_app.tasks)
        assert running_app.media_client.is_closed
        await original()

    monkeypatch.setattr(running_app.hook_client, "aclose", close)
    await running_app.lifespan.__aexit__(None, None, None)
    running_app.assert_closed()


async def test_lifespan_preserves_external_hook_client_ownership(tmp_path, monkeypatch):
    async with HookClient("http://hook.test", transport=httpx.MockTransport(no_hook_calls)) as external:
        app = make_app(tmp_path, monkeypatch, hook_client=external)
        async with app.router.lifespan_context(app):
            active = workers(app)
            media_client = app.state.douyin._client
        assert all(task.done() for task in active) and media_client.is_closed
        assert not external._client.is_closed


async def test_cancelled_startup_finishes_cleanup_despite_repeated_cancellation(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    lifespan = app.router.lifespan_context(app)
    starting, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_start = app.state.group_tools.start
    original_summary_stop = app.state.chat_summary.stop
    active = []
    media_clients = []

    async def interrupted_start():
        await original_start()
        active.extend(workers(app))
        media_clients.append(app.state.douyin._client)
        starting.set()
        await asyncio.Event().wait()

    async def delayed_cleanup():
        cleaning.set()
        await release.wait()
        await original_summary_stop()

    monkeypatch.setattr(app.state.group_tools, "start", interrupted_start)
    monkeypatch.setattr(app.state.chat_summary, "stop", delayed_cleanup)
    pending = asyncio.create_task(lifespan.__aenter__())
    try:
        await asyncio.wait_for(starting.wait(), 2)
        pending.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert active and all(task.done() for task in active)
        assert media_clients[0].is_closed and app.state.hook_client._client.is_closed
        assert app.state.douyin.messenger is None
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        for _, service, method, args in stages(app):
            await getattr(service, method)(*args)


@pytest.mark.parametrize("stage", ["tools", "summary", "dispatcher"])
async def test_partial_startup_failure_closes_started_resources(tmp_path, monkeypatch, stage):
    app = make_app(tmp_path, monkeypatch)
    service = {"tools": app.state.group_tools, "summary": app.state.chat_summary,
               "dispatcher": app.state.event_dispatcher}[stage]
    original_start = service.start
    active = []
    media_clients = []

    async def start(*args):
        await original_start(*args)
        active.extend(workers(app))
        media_clients.append(app.state.douyin._client)
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(service, "start", start)
    try:
        with pytest.raises(RuntimeError, match="synthetic startup failure"):
            async with app.router.lifespan_context(app):
                pytest.fail("Startup must fail before accepting requests")
        assert active and all(task.done() for task in active)
        assert media_clients[0].is_closed and app.state.hook_client._client.is_closed
        assert app.state.douyin.messenger is None
    finally:
        for _, service, method, args in stages(app):
            await getattr(service, method)(*args)
