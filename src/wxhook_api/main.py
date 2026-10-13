from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.core.body_limit import BodyLimitMiddleware
from wxhook_api.core.config import Settings, load_settings
from wxhook_api.core.errors import install_exception_handlers
from wxhook_api.routes import account, callbacks, contacts, health, history, media, messages, querydb
from wxhook_api.services.events import EventDispatcher


def create_app(settings: Settings | None = None, *, hook_transport: httpx.AsyncBaseTransport | None = None, hook_client: HookClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    dispatcher = EventDispatcher(settings.callback_queue_size)
    owned_client: HookClient | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal owned_client
        if hook_client is None:
            owned_client = HookClient(settings.hook_base_url, connect_timeout=settings.connect_timeout, read_timeout=settings.read_timeout, transport=hook_transport)
            app.state.hook_client = owned_client
        await dispatcher.start()
        try:
            yield
        finally:
            try:
                await dispatcher.stop(settings.shutdown_timeout)
            finally:
                if owned_client is not None:
                    await owned_client.aclose()
                    owned_client = None

    app = FastAPI(title="wxhook-api", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.hook_client = hook_client
    app.state.event_dispatcher = dispatcher
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    install_exception_handlers(app)
    for router in (health.router, account.router, messages.router, media.router, querydb.router, contacts.router, history.router, callbacks.router):
        app.include_router(router)
    return app


app = create_app()
