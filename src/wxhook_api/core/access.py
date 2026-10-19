"""Local desktop trust boundary and optional authenticated remote access."""
import base64
import hmac
import ipaddress
import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


def loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class AccessMiddleware:
    def __init__(self, app):
        self.app = app
        self.token = os.getenv("WXHOOK_ADMIN_TOKEN", "")
        if self.token and (len(self.token) < 24 or not self.token.isascii() or not self.token.isprintable()):
            raise ValueError("WXHOOK_ADMIN_TOKEN must contain at least 24 printable ASCII characters")
        self.hosts = {"localhost", "127.0.0.1", "::1"}
        self.hosts.update(x.strip().lower() for x in os.getenv("WXHOOK_ALLOWED_HOSTS", "").split(",") if x.strip())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        try:
            target = urlsplit("http://" + headers.get("host", ""))
            valid_host = target.hostname in self.hosts and not target.username and not target.password
            target.port
            origin = headers.get("origin")
            parsed = urlsplit(origin) if origin else None
            same_origin = not parsed or (parsed.scheme == scope.get("scheme", "http") and parsed.netloc.lower() == target.netloc.lower())
        except ValueError:
            valid_host = same_origin = False
        if not valid_host or not same_origin or headers.get("sec-fetch-site") == "cross-site":
            await self.reject(scope, receive, send, 403, "请求来源不受信任。")
            return
        local = loopback((scope.get("client") or ("", 0))[0])
        if not local and scope.get("scheme") != "https":
            await self.reject(scope, receive, send, 403, "远程访问必须使用 HTTPS。")
            return
        # The local Hook cannot supply administrator credentials. It may only
        # submit callbacks, never read records or mutate model configuration.
        callback = scope["path"] == "/receiveChatBotMsg" and scope["method"] == "POST"
        if local and callback and not origin and not headers.get("sec-fetch-site"):
            await self.app(scope, receive, send)
            return
        supplied = headers.get("authorization", "")
        secret = ""
        if supplied.lower().startswith("bearer "):
            secret = supplied[7:]
        elif supplied.lower().startswith("basic "):
            try:
                user, secret = base64.b64decode(supplied[6:], validate=True).decode("utf-8").split(":", 1)
                if user != "admin":
                    secret = ""
            except (ValueError, UnicodeError):
                pass
        authenticated = bool(self.token) and hmac.compare_digest(secret.encode(), self.token.encode())
        if not authenticated and (self.token or not local):
            await self.reject(scope, receive, send, 401, "需要管理员认证；远程访问必须配置管理口令。")
            return
        await self.app(scope, receive, send)

    async def reject(self, scope, receive, send, status, message):
        response = JSONResponse({"error": {"code": "access_denied", "message": message}}, status_code=status,
                                headers={"Cache-Control": "no-store", "WWW-Authenticate": 'Basic realm="wxhook", charset="UTF-8"'})
        await response(scope, receive, send)
