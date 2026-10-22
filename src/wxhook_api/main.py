from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import AsyncExitStack, asynccontextmanager
import os
import sqlite3

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.body_limit import BodyLimitMiddleware
from wxhook_api.core.access import AccessMiddleware
from wxhook_api.core.config import Settings, load_settings
from wxhook_api.core.chatbot_config import load_chatbot_options
from wxhook_api.core.douyin_config import load_douyin_options
from wxhook_api.core.errors import install_exception_handlers
from wxhook_api.core.persistence import finish_before_cancel
from wxhook_api.core.runtime import source_revision
from wxhook_api.routes import account, callbacks, contacts, douyin, group_tools, health, history, manage, media, messages, querydb, ui
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.chatbot import MentionChatService
from wxhook_api.services.chat_model import ChatModelConfiguration
from wxhook_api.services.chat_summary import ChatSummaryService
from wxhook_api.services.douyin_delivery import DouyinMessenger
from wxhook_api.services.events import EventDispatcher
from wxhook_api.services.groups import GroupManager, event_group
from wxhook_api.services.group_tools import GroupToolsService
from wxhook_api.services.inbox import MessageInbox
from wxhook_api.services.wechat_sync import SyncOptions, WeChatSync
from wxhook_api.services.web_search import WebSearchConfiguration


def create_app(settings: Settings | None = None, *, hook_transport: httpx.AsyncBaseTransport | None = None, hook_client: HookClient | None = None, douyin_service: DouyinService | None = None, group_manager: GroupManager | None = None, sync_options: SyncOptions | None = None, chat_service: MentionChatService | None = None) -> FastAPI:
    settings = settings or load_settings()
    dispatcher = EventDispatcher(settings.callback_queue_size)
    groups = group_manager or GroupManager()
    synchronizer = WeChatSync(groups, sync_options or SyncOptions.from_environment())
    inbox = MessageInbox()
    douyin_worker = douyin_service or DouyinService(load_douyin_options())
    douyin_worker.group_manager = groups
    chatbot = chat_service or MentionChatService(load_chatbot_options())
    chatbot.groups = groups
    chatbot.default_group_enabled = douyin_worker.allowed
    douyin_worker.account_check = chatbot.verify_account
    douyin_worker.own_message_check = chatbot.own_message
    model_configuration = ChatModelConfiguration(chatbot)
    search_configuration = WebSearchConfiguration(chatbot=chatbot)
    summary_service = ChatSummaryService(groups, model_configuration, chatbot.identity)
    chatbot.summary_service = summary_service
    tools_service = GroupToolsService(groups, model_configuration, chatbot)
    chatbot.tools_service = tools_service

    async def archive_event(event):
        if synchronizer.options.enabled:
            await synchronizer.receive(event)
            return
        group = event_group(event)
        if group and (chatbot.options.enabled or douyin_worker.allowed(group, event.sender)
                      or group in await tools_service.enabled_groups()):
            if not await chatbot.verify_account():
                raise ValueError("无法核验微信账号，已停止处理此回调")
        await groups.receive(event)

    dispatcher.register(archive_event, critical=True)
    dispatcher.register(inbox.receive)

    async def automatic_message(event):
        if await chatbot.receive(event):
            return
        if await tools_service.receive(event):
            return
        await douyin_worker.receive(event)

    dispatcher.register(automatic_message)
    owned_client: HookClient | None = None

    async def shutdown():
        async def close_owned_client():
            nonlocal owned_client
            if owned_client is not None:
                await owned_client.aclose()
                owned_client = None

        # Callbacks run in reverse order and each still runs if an earlier
        # service fails to stop. Keep the shared Hook open until all workers exit.
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(close_owned_client)
            cleanup.callback(setattr, douyin_worker, "messenger", None)
            cleanup.push_async_callback(douyin_worker.stop)
            cleanup.push_async_callback(chatbot.stop)
            cleanup.push_async_callback(summary_service.stop)
            cleanup.push_async_callback(tools_service.stop)
            cleanup.push_async_callback(dispatcher.stop, settings.shutdown_timeout)
            cleanup.push_async_callback(synchronizer.stop)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal owned_client
        if hook_client is None:
            owned_client = HookClient(settings.hook_base_url, connect_timeout=settings.connect_timeout, read_timeout=settings.read_timeout, transport=hook_transport)
            app.state.hook_client = owned_client
        try:
            if os.getenv("WXHOOK_DOUYIN_FEEDBACK_ENABLED", "true").lower() not in {"false", "0", "off"}:
                douyin_worker.messenger = DouyinMessenger(app.state.hook_client, account_check=chatbot.verify_account)
            await groups.start(douyin_worker.options.settings.groups)
            await douyin_worker.start()
            await chatbot.start(app.state.hook_client)
            await tools_service.start()
            summary_service.bot_wxid = chatbot.identity
            await summary_service.start()
            await dispatcher.start()
            synchronizer.start(app.state.hook_client)
            yield
        finally:
            await finish_before_cancel(shutdown())

    app = FastAPI(title="wxhook-api", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.hook_client = hook_client
    app.state.event_dispatcher = dispatcher
    app.state.message_inbox = inbox
    app.state.callback_status = {"accepted": 0, "rejected": 0, "last_accepted_at": "", "last_rejection": None}
    app.state.launcher_status = None
    app.state.douyin = douyin_worker
    app.state.groups = groups
    app.state.wechat_sync = synchronizer
    app.state.chatbot = chatbot
    app.state.chat_model = model_configuration
    app.state.chat_search = search_configuration
    app.state.chat_summary = summary_service
    app.state.group_tools = tools_service
    app.state.runtime_revision = source_revision()
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(AccessMiddleware)
    install_exception_handlers(app)
    app.add_exception_handler(sqlite3.Error, manage.storage_error)
    for router in (health.router, account.router, messages.router, media.router, querydb.router, contacts.router, history.router, callbacks.router, ui.router, douyin.router, manage.router, group_tools.router):
        app.include_router(router)
    app.mount("/ui/assets", StaticFiles(directory=ui.WEB_ROOT), name="message-assets")
    return app


app = create_app()


if __name__ == "__main__":
    from wxhook_api.launcher import main

    main()
