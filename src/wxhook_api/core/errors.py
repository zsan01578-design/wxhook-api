from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from wxhook_api.core.callback_diagnostics import rejected_callback


class HookTransportError(RuntimeError):
    """The Hook returned an unusable transport response."""


class HookTimeoutError(HookTransportError):
    """The Hook did not respond before the configured timeout."""


class AccountMismatchError(ValueError):
    """A different WeChat account must not reuse this service's archive or policies."""


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
        invalid_json = request.method == "POST" and any(error.get("type") == "json_invalid" for error in exc.errors())
        if request.url.path.rstrip("/") == "/receiveChatBotMsg":
            result = rejected_callback(request.app, exc, 400 if invalid_json else 422)
            if not invalid_json:
                return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": "Hook callback fields are incompatible", "fields": result["fields"]}})
        if invalid_json:
            return invalid_json_response()
        if request.url.path in {"/api/manage/chat-model", "/api/manage/chat-search"} or request.url.path.startswith("/api/manage/chat-models"):
            return error_response(422, "validation_error", "请检查名称、Base URL、模型名称、额度及 API Key 格式；远程地址须为 HTTPS，HTTP 仅允许本机，不能包含账号或查询参数。")
        if request.url.path == "/api/manage/chatbot/mentions":
            return error_response(422, "validation_error", "机器人显示名最多 20 个，每个 1–80 字，请每行填写一个。")
        if request.url.path == "/api/douyin/config":
            # Explain configuration errors without returning rejected values,
            # which may include an account-specific parsing endpoint.
            hints = {
                "groups": "请填写真实群 ID（例如 123456789@chatroom），每行一个，不能填写群昵称",
                "parser_provider": "后台不接受此解析来源，请更新并停止旧服务后重新运行 main.py",
                "parser_api_url": "接口地址须以 https://jk.y-l.cc/api/dsp/ 开头，不要附加 ?url= 参数",
            }
            fields = list(dict.fromkeys(str(error["loc"][1]) for error in exc.errors() if len(error.get("loc", ())) > 1))
            descriptions = list(dict.fromkeys(hints.get(field, "请检查配置字段 " + field) for field in fields))
            return error_response(422, "validation_error", "配置校验失败：" + ("；".join(descriptions[:4]) or "请使用页面表单重新保存配置"))
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
