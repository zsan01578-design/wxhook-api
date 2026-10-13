from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class HookTransportError(RuntimeError):
    """The Hook returned an unusable transport response."""


class HookTimeoutError(HookTransportError):
    """The Hook did not respond before the configured timeout."""


class InvalidJsonError(ValueError):
    """A request body is not valid JSON."""


class BodyTooLargeError(ValueError):
    """A request body exceeds the configured limit."""


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def invalid_json_response() -> JSONResponse:
    return JSONResponse(status_code=400, content={"ret": -1, "msg": "invalid json"})


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(InvalidJsonError)
    async def invalid_json_handler(request: Request, exc: InvalidJsonError):
        return invalid_json_response()

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        if request.method == "POST" and any(error.get("type") == "json_invalid" for error in exc.errors()):
            return invalid_json_response()
        return error_response(422, "validation_error", "request validation failed")

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        if request.method == "POST" and exc.status_code == 400 and "parsing the body" in str(exc.detail):
            return invalid_json_response()
        return error_response(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(HookTimeoutError)
    async def timeout_handler(request: Request, exc: HookTimeoutError):
        return error_response(504, "hook_timeout", str(exc))

    @app.exception_handler(HookTransportError)
    async def transport_handler(request: Request, exc: HookTransportError):
        return error_response(502, "hook_transport_error", str(exc))
