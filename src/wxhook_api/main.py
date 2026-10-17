from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import asynccontextmanager
import sqlite3

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.body_limit import BodyLimitMiddleware
from wxhook_api.core.config import Settings, load_settings
from wxhook_api.core.douyin_config import load_douyin_options
from wxhook_api.core.errors import install_exception_handlers
from wxhook_api.routes import account, callbacks, contacts, douyin, health, history, manage, media, messages, querydb, ui
from wxhook_api.services.douyin import DouyinService
from wxhook_api.services.events import EventDispatcher
from wxhook_api.services.groups import GroupManager
from wxhook_api.services.inbox import MessageInbox
from wxhook_api.services.wechat_sync import SyncOptions, WeChatSync


def create_app(settings: Settings | None = None, *, hook_transport: httpx.AsyncBaseTransport | None = None, hook_client: HookClient | None = None, douyin_service: DouyinService | None = None, group_manager: GroupManager | None = None, sync_options: SyncOptions | None = None) -> FastAPI:
    settings = settings or load_settings()
    dispatcher = EventDispatcher(settings.callback_queue_size)
    groups = group_manager or GroupManager()
    synchronizer = WeChatSync(groups, sync_options or SyncOptions.from_environment())
    dispatcher.register(synchronizer.receive if synchronizer.options.enabled else groups.receive)
    inbox = MessageInbox()
    dispatcher.register(inbox.receive)
    douyin_worker = douyin_service or DouyinService(load_douyin_options())
    douyin_worker.group_manager = groups
    dispatcher.register(douyin_worker.receive)
    owned_client: HookClient | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal owned_client
        if hook_client is None:
            owned_client = HookClient(settings.hook_base_url, connect_timeout=settings.connect_timeout, read_timeout=settings.read_timeout, transport=hook_transport)
            app.state.hook_client = owned_client
        try:
            await groups.start(douyin_worker.options.settings.groups)
            await douyin_worker.start()
            await dispatcher.start()
            synchronizer.start(app.state.hook_client)
            yield
        finally:
            await synchronizer.stop()
            try:
                await dispatcher.stop(settings.shutdown_timeout)
            finally:
                try:
                    await douyin_worker.stop()
                finally:
                    if owned_client is not None:
                        await owned_client.aclose()
                        owned_client = None

    app = FastAPI(title="wxhook-api", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.hook_client = hook_client
    app.state.event_dispatcher = dispatcher
    app.state.message_inbox = inbox
    app.state.launcher_status = None
    app.state.douyin = douyin_worker
    app.state.groups = groups
    app.state.wechat_sync = synchronizer
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    install_exception_handlers(app)
    app.add_exception_handler(sqlite3.Error, manage.storage_error)
    for router in (health.router, account.router, messages.router, media.router, querydb.router, contacts.router, history.router, callbacks.router, ui.router, douyin.router, manage.router):
        app.include_router(router)
    app.mount("/ui/assets", StaticFiles(directory=ui.WEB_ROOT), name="message-assets")
    return app


app = create_app()


if __name__ == "__main__":
    from wxhook_api.launcher import main

    main()
