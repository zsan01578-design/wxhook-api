# media-parser 接入实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用本地 `ucmao/media-parser` 服务替换 dudunas 去水印兜底，并让它同时可作为解析来源与失败兜底。

**Architecture:** media-parser 作为独立 Flask 进程跑在 `127.0.0.1:8051`（`API_ONLY=true`），wxhook 通过 `httpx` 调 `POST /api/v1/parse` 并适配成 `ParsedMedia`；解析链路增加 `media_parser` 来源与失败自动兜底，删除 dudunas 全部代码与配置；重启脚本负责拉起本地服务。

**Tech Stack:** Python 3.11–3.14（Windows）、FastAPI/httpx（wxhook）、Flask/requests/waitress（media-parser）、pytest、PowerShell。

**Spec:** `docs/superpowers/specs/2026-10-28-media-parser-integration-design.md`

## Global Constraints

- 支持平台保持抖音 / 小红书 / B站，不放开其余平台。
- media-parser 地址只允许回环主机（`127.0.0.1`、`localhost`、`::1`）且必须显式端口。
- 所有媒体 URL 必须通过 `checked_url(..., media=True)`；响应上限 2 MB、图集上限 100 张。
- 测试不得访问真实网络或本机 8051；一律使用 `httpx.MockTransport`。
- 默认不执行 `git commit` / `git push`；本仓库约定提交需用户明确授权，每个任务结束只汇报变更。
- 每个任务结束前运行对应测试；最终运行 `.venv/Scripts/python.exe -m pytest -q`、`ruff check src tests`、`python -m compileall -q src`、`python -m pip check`。
- 运行测试统一用 `.venv/Scripts/python.exe -m pytest ...`（Windows）。

## File Structure

**新增**

- `src/wxhook_api/services/media_parser.py` — media-parser HTTP 客户端与响应映射。
- `tests/test_media_parser.py` — 客户端映射、错误、限制测试。
- `tests/test_media_parser_config.py` — 地址与来源组合校验测试。
- `tests/test_media_parser_launcher.py` — 启动器纯函数测试。
- `scripts/media-parser-server.py` — 本地服务启动器。
- `scripts/start-media-parser.bat` — 前台手动启动入口。
- `docs/superpowers/plans/2026-10-28-media-parser-integration.md` — 本计划。

**修改**

- `src/wxhook_api/core/douyin_config.py` — 配置字段、校验、读写。
- `src/wxhook_api/routes/parse.py` — `/api/parse` 兜底。
- `src/wxhook_api/routes/douyin.py` — `supported_parsers`。
- `src/wxhook_api/services/douyin.py` — 群聊解析链路与平台限制。
- `src/wxhook_api/services/douyin_parser.py` — 移除 dudunas 域名。
- `src/wxhook_api/services/media_cache.py` — 缓存版本。
- `src/wxhook_api/web/douyin.html`、`web/douyin.js`、`web/manage.js` — 页面。
- `requirements-runtime.lock` — 新增运行时依赖。
- `scripts/restart-project.ps1`、`tests/restart_project.Tests.ps1` — 生命周期。
- `tests/conftest.py`、`tests/test_douyin.py`、`tests/test_manage.py`、`tests/test_douyin_retry.py`、`tests/test_douyin_fallback.py`、`tests/test_parse_api.py`、`tests/test_parse_video.py`、`tests/test_media_cache.py` — 测试。
- `.env.example`、`parse-video.example.yml`、`docs/GUIDE.md`、`THIRD_PARTY_NOTICES.md` — 配置与文档。

**删除**

- `src/wxhook_api/services/parse_fallback.py`
- `tests/test_parse_fallback.py`

---

### Task 1: media-parser 客户端与响应映射

**Files:**
- Create: `src/wxhook_api/services/media_parser.py`
- Test: `tests/test_media_parser.py`

**Interfaces:**
- Consumes: `DouyinError`、`ParsedMedia`、`checked_url`、`read_response_bytes`（`services/douyin_parser.py`），`source_platform`（`services/parse_video.py`）。
- Produces:
  - `MediaParserClient(base_url: str, *, transport: httpx.AsyncBaseTransport | None = None, max_bytes: int = 2 * 1024 * 1024)`
  - `async MediaParserClient.parse(source: str) -> ParsedMedia`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_media_parser.py`：

```python
import json

import httpx
import pytest

from wxhook_api.services.douyin_parser import DouyinError
from wxhook_api.services.media_parser import MediaParserClient


BASE = "http://127.0.0.1:8051"
SHORT = "https://v.douyin.com/mediaparser/"
XHS = "https://www.xiaohongshu.com/explore/abcdef1234567890"
VIDEO = "https://v3.douyinvod.com/media.mp4"
IMAGE = "https://p3.douyinpic.com/media.png"
XHS_IMAGE = "https://sns-img.xhscdn.com/media.png"


def success(**data):
    return httpx.Response(200, json={"retcode": 200, "retdesc": "成功", "succ": True, "data": data})


def client(handler, **kwargs):
    return MediaParserClient(BASE, transport=httpx.MockTransport(handler), **kwargs)


async def test_video_payload_maps_to_parsed_media():
    media = await client(lambda request: success(
        video_id="123456", title="标题", desc="完整文案", video_url=VIDEO,
        platform="抖音", author={"nickname": "作者"},
    )).parse(SHORT)
    assert media.kind == "video"
    assert media.item_id == "123456"
    assert media.title == "标题"
    assert media.author == "作者"
    assert media.platform == "douyin"
    assert media.assets == [{"kind": "video", "urls": [VIDEO]}]


async def test_request_uses_post_json_to_v1_parse():
    calls = []

    def respond(request):
        calls.append(request)
        return success(video_url=VIDEO)

    await client(respond).parse(SHORT)
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{BASE}/api/v1/parse"
    assert json.loads(calls[0].content) == {"url": SHORT}


async def test_image_payload_ignores_live_photo_video_and_uses_source_platform():
    media = await client(lambda request: success(
        title="图文", platform="抖音",
        author={"nickname": "作者"},
        image_list=[IMAGE, {"url": XHS_IMAGE, "live_photo_url": "https://sns-video.xhscdn.com/live.mp4"}],
    )).parse(XHS)
    assert media.kind == "images"
    assert media.platform == "xiaohongshu"
    assert [asset["urls"] for asset in media.assets] == [[IMAGE], [XHS_IMAGE]]


async def test_title_falls_back_to_desc_then_default():
    media = await client(lambda request: success(desc="只有文案", video_url=VIDEO)).parse(SHORT)
    assert media.title == "只有文案"
    media = await client(lambda request: success(video_url=VIDEO)).parse(SHORT)
    assert media.title == "未命名作品"


@pytest.mark.parametrize("payload, expected", [
    ({"retcode": 400, "retdesc": "需要登录", "succ": False,
      "error_code": "XIAOHONGSHU_COOKIE_REQUIRED", "data": None}, "parse-video.local.yml"),
    ({"retcode": 400, "retdesc": "提取媒体内容失败", "succ": False,
      "error_code": "MEDIA_NOT_FOUND", "data": None}, "MEDIA_NOT_FOUND"),
])
async def test_business_errors_are_reported_without_http_status(payload, expected):
    with pytest.raises(DouyinError) as error:
        await client(lambda request: httpx.Response(400, json=payload)).parse(XHS)
    assert expected in str(error.value)


async def test_non_json_error_uses_http_status():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: httpx.Response(500, text="<html>oops</html>")).parse(SHORT)
    assert "HTTP 500" in str(error.value)


async def test_invalid_json_success_is_rejected():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: httpx.Response(200, text="not-json")).parse(SHORT)
    assert "不是有效 JSON" in str(error.value)


async def test_connection_failure_is_explained():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DouyinError) as error:
        await client(refuse).parse(SHORT)
    assert "本地服务连接失败" in str(error.value)


async def test_oversized_response_is_rejected():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: httpx.Response(200, content=b" " * 2048), max_bytes=1024).parse(SHORT)
    assert "响应过大" in str(error.value)


async def test_untrusted_media_url_is_rejected():
    with pytest.raises(DouyinError):
        await client(lambda request: success(image_list=["http://127.0.0.1/private.png"])).parse(SHORT)


async def test_more_than_100_images_is_rejected():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: success(image_list=[IMAGE] * 101)).parse(SHORT)
    assert "超过 100" in str(error.value)


async def test_missing_media_is_rejected():
    with pytest.raises(DouyinError) as error:
        await client(lambda request: success(title="空作品")).parse(SHORT)
    assert "没有返回视频或图片地址" in str(error.value)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_media_parser.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'wxhook_api.services.media_parser'`

- [ ] **Step 3: 实现客户端**

创建 `src/wxhook_api/services/media_parser.py`：

```python
"""Adapter for the local ucmao/media-parser HTTP service."""

from __future__ import annotations

import json

import httpx

from wxhook_api.services.douyin_parser import DouyinError, ParsedMedia, checked_url, read_response_bytes
from wxhook_api.services.parse_video import source_platform


DEFAULT_MAX_BYTES = 2 * 1024 * 1024
MAX_IMAGES = 100
CONNECT_MESSAGE = "media-parser 本地服务连接失败，请确认 127.0.0.1:8051 已启动"
COOKIE_MESSAGE = "media-parser 提示小红书需要登录 Cookie，请检查 parse-video.local.yml 的 xiaohongshu-cookie"


class MediaParserClient:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self.endpoint = base_url.rstrip("/") + "/api/v1/parse"
        self.transport = transport
        self.max_bytes = max(1024, int(max_bytes))

    async def parse(self, source: str) -> ParsedMedia:
        source = checked_url(source, multi_platform=True)
        status, raw = await self._request(source)
        return _parse_payload(raw, source, status)

    async def _request(self, source: str) -> tuple[int, bytes]:
        try:
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(30, connect=2),
            ) as client:
                async with client.stream(
                    "POST", self.endpoint, json={"url": source},
                    headers={"Accept": "application/json"},
                ) as response:
                    status = response.status_code
                    raw = bytearray()
                    async for chunk in read_response_bytes(
                        response, self.max_bytes, size_error="media-parser 响应过大，已停止处理"):
                        raw.extend(chunk)
        except httpx.ConnectError as exc:
            raise DouyinError(CONNECT_MESSAGE) from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise DouyinError("media-parser 解析超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise DouyinError(CONNECT_MESSAGE) from exc
        return status, bytes(raw)


def _parse_payload(raw: bytes, source: str, status: int) -> ParsedMedia:
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError, UnicodeError):
        payload = None
    if not isinstance(payload, dict):
        if status != 200:
            raise DouyinError(f"media-parser 返回 HTTP {status}")
        raise DouyinError("media-parser 返回的不是有效 JSON")

    retcode = payload.get("retcode")
    if retcode != 200 or payload.get("succ") is not True:
        code = str(payload.get("error_code") or "")[:50]
        if code == "XIAOHONGSHU_COOKIE_REQUIRED":
            raise DouyinError(COOKIE_MESSAGE)
        desc = str(payload.get("retdesc") or "解析失败")[:200]
        raise DouyinError(f"media-parser 解析失败：{desc}" + (f"（{code}）" if code else ""))

    data = payload.get("data")
    if not isinstance(data, dict):
        raise DouyinError("media-parser 成功响应缺少 data 对象")

    images = data.get("image_list") or []
    if not isinstance(images, list):
        raise DouyinError("media-parser 返回的图片列表格式无效")
    if len(images) > MAX_IMAGES:
        raise DouyinError("media-parser 返回的图集超过 100 张，已停止处理")

    assets = []
    for item in images:
        value = item if isinstance(item, str) else item.get("url") if isinstance(item, dict) else None
        if not isinstance(value, str) or not value:
            raise DouyinError("media-parser 返回了无效图片地址")
        assets.append({"kind": "image", "urls": [checked_url(value, media=True)]})

    if assets:
        kind = "images"
    else:
        video = data.get("video_url")
        if not isinstance(video, str) or not video:
            raise DouyinError("media-parser 没有返回视频或图片地址")
        assets = [{"kind": "video", "urls": [checked_url(video, media=True)]}]
        kind = "video"

    raw_title = data.get("title") or data.get("desc") or ""
    title = (raw_title if isinstance(raw_title, str) else "")[:2000] or "未命名作品"
    author = data.get("author")
    if isinstance(author, dict):
        author = author.get("nickname")
    author = (author if isinstance(author, str) else "")[:200]
    item_id = str(data.get("video_id") or "")[:100]
    return ParsedMedia(item_id, title, author, kind, assets, platform=source_platform(source))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_media_parser.py -q`
Expected: PASS（全部用例）

---

### Task 2: 配置字段、校验与持久化

**Files:**
- Modify: `src/wxhook_api/core/douyin_config.py`（`GroupSettings`、`load_douyin_options`、`save_group_settings`）
- Modify: `src/wxhook_api/routes/douyin.py:35-39`（`supported_parsers`）
- Modify: `tests/conftest.py:37`
- Modify: `tests/test_douyin.py:35-36`（`options()` 默认值）
- Test: `tests/test_media_parser_config.py`

**Interfaces:**
- Produces: `GroupSettings.media_parser_url: str`；`parser_provider` 支持 `"media_parser"`。
- 后续任务通过 `service.options.settings.media_parser_url` 读取地址。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_media_parser_config.py`：

```python
import pytest
from pydantic import ValidationError

from wxhook_api.core.douyin_config import GroupSettings, load_douyin_options, save_group_settings


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8051",
    "http://localhost:9000",
    "http://[::1]:8051",
    "",
])
def test_media_parser_url_accepts_loopback_and_empty(url):
    assert GroupSettings(media_parser_url=url).media_parser_url == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "http://192.168.1.10:8051",
    "http://127.0.0.1",
    "http://127.0.0.1:8051/api",
    "http://user:pass@127.0.0.1:8051",
    "ftp://127.0.0.1:8051",
    "http://127.0.0.1:8051?x=1",
])
def test_media_parser_url_rejects_non_loopback_or_paths(url):
    with pytest.raises(ValidationError):
        GroupSettings(media_parser_url=url)


def test_media_parser_provider_requires_an_address():
    with pytest.raises(ValidationError):
        GroupSettings(parser_provider="media_parser", media_parser_url="")
    settings = GroupSettings(parser_provider="media_parser", media_parser_url="http://127.0.0.1:8051")
    assert settings.parser_provider == "media_parser"


def test_environment_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "douyin.local.yml"
    path.write_text("media-parser-url: http://127.0.0.1:8051\n", encoding="utf-8")
    monkeypatch.setenv("WXHOOK_DOUYIN_CONFIG", str(path))
    monkeypatch.setenv("WXHOOK_MEDIA_PARSER_URL", "http://localhost:9001")
    assert load_douyin_options().settings.media_parser_url == "http://localhost:9001"


def test_media_parser_url_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "douyin.local.yml"
    monkeypatch.setenv("WXHOOK_DOUYIN_CONFIG", str(path))
    monkeypatch.delenv("WXHOOK_MEDIA_PARSER_URL", raising=False)
    options = load_douyin_options()
    assert options.settings.media_parser_url == "http://127.0.0.1:8051"
    updated = save_group_settings(
        options, options.settings.model_copy(update={"media_parser_url": "http://localhost:9001"}))
    assert updated.settings.media_parser_url == "http://localhost:9001"
    assert load_douyin_options().settings.media_parser_url == "http://localhost:9001"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_media_parser_config.py -q`
Expected: FAIL，`ValidationError`/`AttributeError`（字段不存在）

- [ ] **Step 3: 修改配置模型**

在 `src/wxhook_api/core/douyin_config.py`：

1. 导入改为：

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
```

2. `GroupSettings` 的 `parser_provider` 与新增字段改为：

```python
    parser_provider: Literal["parse_video", "api", "builtin", "media_parser"] = "parse_video"
    parser_api_url: str = Field(default="", max_length=4096)
    media_parser_url: str = Field(default="http://127.0.0.1:8051", max_length=2048)
```

3. 在 `validate_parser_api_url` 之后新增：

```python
    @field_validator("media_parser_url")
    @classmethod
    def validate_media_parser_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.port is not None
                and 1 <= parsed.port <= 65535
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and parsed.path in {"", "/"}
                and not any(ord(char) < 33 for char in value)
            )
            if not valid:
                raise ValueError
        except ValueError as exc:
            raise ValueError("media-parser 地址必须是本机地址，例如 http://127.0.0.1:8051") from exc
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_media_parser_selection(self):
        if self.parser_provider == "media_parser" and not self.media_parser_url:
            raise ValueError("选择 media-parser 时必须填写本机服务地址")
        return self
```

4. `load_douyin_options` 的 `GroupSettings(...)` 末尾新增：

```python
        media_parser_url=os.getenv("WXHOOK_MEDIA_PARSER_URL", values.get("media-parser-url", "http://127.0.0.1:8051")),
```

5. `save_group_settings` 的 `values` 字典新增：

```python
        "media-parser-url": settings.media_parser_url,
```

6. `src/wxhook_api/routes/douyin.py` 的 `supported_parsers` 改为：

```python
"supported_parsers": ["parse_video", "api", "builtin", "media_parser"]
```

- [ ] **Step 4: 隔离测试环境默认值**

`tests/conftest.py`：删除

```python
    monkeypatch.delenv("WXHOOK_DUDUNAS_SECRET", raising=False)
```

替换为

```python
    monkeypatch.setenv("WXHOOK_MEDIA_PARSER_URL", "")
```

`tests/test_douyin.py` 的 `options()`：

```python
def options(tmp_path, **settings):
    return DouyinOptions(GroupSettings(**{"enabled": True, "groups": [GROUP], "parser_provider": "builtin", "media_parser_url": "", **settings}), tmp_path / "douyin.local.yml", tmp_path / "downloads")
```

`tests/test_manage.py` 的 `make_service()`：

```python
def make_service(tmp_path, transport=None, **values):
    settings = GroupSettings(**{"enabled": True, "groups": [GROUP, OTHER], "parser_provider": "builtin", "media_parser_url": "", **values})
    return DouyinService(DouyinOptions(settings, tmp_path / "douyin.yml", tmp_path / "downloads"), transport=transport)
```

- [ ] **Step 5: 更新受影响的既有断言**

`tests/test_parse_video.py:317`：

```python
            assert (await context.client.get("/api/douyin/config")).json()["supported_parsers"] == ["parse_video", "api", "builtin", "media_parser"]
```

- [ ] **Step 6: 运行相关测试确认通过**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_media_parser_config.py tests/test_douyin.py tests/test_manage.py tests/test_parse_video.py tests/test_douyin_fallback.py tests/test_douyin_api.py -q
```
Expected: PASS

---

### Task 3: `/api/parse` 失败兜底到 media-parser

**Files:**
- Modify: `src/wxhook_api/routes/parse.py:8,48-62`
- Test: `tests/test_parse_api.py`（追加用例）

**Interfaces:**
- Consumes: `MediaParserClient`（Task 1）、`settings.media_parser_url`（Task 2）。
- Produces: `/api/parse` 主解析失败且地址非空时调用 media-parser；地址为空时保持原错误。

- [ ] **Step 1: 写失败测试**

在 `tests/test_parse_api.py` 追加：

```python
from collections import deque
from types import SimpleNamespace

from fastapi import HTTPException, Response

from wxhook_api.core.douyin_config import GroupSettings
from wxhook_api.routes import parse as parse_routes
from wxhook_api.schemas.requests import ParseLinkRequest
from wxhook_api.services.douyin_parser import DouyinError, ParsedMedia


MEDIA_BASE = "http://127.0.0.1:8051"
FALLBACK_VIDEO = "https://v3.douyinvod.com/fallback.mp4"


def media_payload(request):
    if request.url.host == "127.0.0.1" and request.url.path == "/api/v1/parse":
        return httpx.Response(200, json={
            "retcode": 200, "retdesc": "成功", "succ": True,
            "data": {"title": "兜底标题", "author": {"nickname": "作者"}, "video_url": FALLBACK_VIDEO},
        })
    return httpx.Response(404)


class FailingPrimary:
    transport = httpx.MockTransport(media_payload)

    async def parse(self, source):
        raise DouyinError("primary failed")


class VideoPrimary:
    transport = httpx.MockTransport(lambda request: pytest.fail("fallback must not run after success"))

    async def parse(self, source):
        return ParsedMedia("id", "主解析", "作者", "video",
                           [{"kind": "video", "urls": ["https://v3.douyinvod.com/primary.mp4"]}], "douyin")


def parse_state(parser, media_url):
    settings = GroupSettings(media_parser_url=media_url)
    return SimpleNamespace(state=SimpleNamespace(
        parse_video_parser=parser,
        parse_slots=asyncio.Semaphore(2),
        parse_rate=deque(),
        douyin=SimpleNamespace(options=SimpleNamespace(settings=settings)),
    ))


async def test_api_parse_falls_back_to_media_parser():
    request = SimpleNamespace(app=parse_state(FailingPrimary(), MEDIA_BASE))
    result = await parse_routes.parse_link(ParseLinkRequest(url="https://v.douyin.com/fallback/"), request, Response())
    assert result["kind"] == "video"
    assert result["title"] == "兜底标题"
    assert result["assets"] == [{"kind": "video", "urls": [FALLBACK_VIDEO]}]


async def test_api_parse_does_not_call_fallback_after_success():
    request = SimpleNamespace(app=parse_state(VideoPrimary(), MEDIA_BASE))
    result = await parse_routes.parse_link(ParseLinkRequest(url="https://v.douyin.com/fallback/"), request, Response())
    assert result["title"] == "主解析"


async def test_api_parse_without_media_url_keeps_primary_error():
    request = SimpleNamespace(app=parse_state(FailingPrimary(), ""))
    with pytest.raises(HTTPException) as error:
        await parse_routes.parse_link(ParseLinkRequest(url="https://v.douyin.com/fallback/"), request, Response())
    assert error.value.status_code == 400
    assert "primary failed" in error.value.detail


async def test_api_parse_combines_primary_and_fallback_errors():
    def fail_media(request):
        return httpx.Response(400, json={
            "retcode": 400, "retdesc": "提取失败", "succ": False,
            "error_code": "MEDIA_NOT_FOUND", "data": None,
        })

    class BrokenFallback:
        transport = httpx.MockTransport(fail_media)

        async def parse(self, source):
            raise DouyinError("primary failed")

    request = SimpleNamespace(app=parse_state(BrokenFallback(), MEDIA_BASE))
    with pytest.raises(HTTPException) as error:
        await parse_routes.parse_link(ParseLinkRequest(url="https://v.douyin.com/fallback/"), request, Response())
    assert "primary failed" in error.value.detail
    assert "media-parser 兜底也失败" in error.value.detail
    assert "MEDIA_NOT_FOUND" in error.value.detail
```

同时在该文件顶部补充导入（若已有则合并）：

```python
from fastapi import HTTPException, Response
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parse_api.py -q -k "media_parser or fallback"`
Expected: FAIL（兜底未实现，`AttributeError: douyin` 或结果仍是主解析错误）

- [ ] **Step 3: 修改路由**

`src/wxhook_api/routes/parse.py`：

1. 导入改为：

```python
from wxhook_api.services.media_parser import MediaParserClient
from wxhook_api.services.parse_video import ParseVideoParser
```

2. `parse_link` 函数体改为：

```python
@router.post("/api/parse", tags=["分享解析"])
async def parse_link(body: ParseLinkRequest, request: Request, response: Response):
    _check_rate(request.app)
    slots = await _acquire_slot(request.app)
    try:
        parser = request.app.state.parse_video_parser
        settings = request.app.state.douyin.options.settings
        try:
            media = await parser.parse(body.url)
        except DouyinError as primary_error:
            if not settings.media_parser_url:
                raise
            try:
                media = await MediaParserClient(
                    settings.media_parser_url, transport=getattr(parser, "transport", None)).parse(body.url)
            except DouyinError as fallback_error:
                raise DouyinError(
                    f"{str(primary_error)[:300]}；media-parser 兜底也失败：{str(fallback_error)[:200]}") from fallback_error
    except DouyinError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        slots.release()
    response.headers["Cache-Control"] = "no-store"
    return {"platform": media.platform, "kind": media.kind, "title": media.title, "author": media.author, "assets": media.assets}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parse_api.py -q`
Expected: PASS

---

### Task 4: 群聊解析链路与平台限制

**Files:**
- Modify: `src/wxhook_api/services/douyin.py:23,357-393,415-416`
- Test: `tests/test_douyin_retry.py`（重写 dudunas 用例）
- Test: `tests/test_douyin_fallback.py`（追加 media 兜底用例）

**Interfaces:**
- Consumes: `MediaParserClient`、`settings.media_parser_url`。
- Produces: `_resolve_media` 的兜底顺序与 `job["parser_provider"]` / `job["parser_fallback"]` 语义。

- [ ] **Step 1: 写失败测试**

将 `tests/test_douyin_retry.py` 中 `FailingParser`、`FakeFallback`、`TogglingParser`、`test_retry_restores_primary_provider_after_a_fallback_attempt`、`VideoParser`、`ImageParser`、`test_video_primary_is_replaced_by_the_watermark_fallback`、`test_image_primary_does_not_call_the_watermark_fallback`、`test_dudunas_fallback_is_used_when_primary_parser_fails` 替换为：

```python
MEDIA_BASE = "http://127.0.0.1:8051"
MEDIA_VIDEO = "https://v3.douyinvod.com/media-fallback.mp4"


def media_success(request):
    if request.url.host == "127.0.0.1" and request.url.path == "/api/v1/parse":
        return httpx.Response(200, json={
            "retcode": 200, "retdesc": "成功", "succ": True,
            "data": {"title": "兜底标题", "author": {"nickname": "作者"}, "video_url": MEDIA_VIDEO},
        })
    return httpx.Response(404)


class FailingParser:
    async def parse(self, source):
        raise DouyinError("primary parser failed")


class VideoParser:
    async def parse(self, source):
        return ParsedMedia("id", "主解析标题", "作者", "video",
                           [{"kind": "video", "urls": ["https://v3.douyinvod.com/primary.mp4"]}], "douyin")


async def test_primary_failure_uses_media_parser_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: FailingParser())
    service = DouyinService(
        options(tmp_path, parser_provider="parse_video", parser_api_url="", media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(media_success))
    job = {"source_url": SHORT, "platform": "douyin"}
    parsed = await service._resolve_media(job, service.options.settings)
    assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is True
    assert parsed.assets == [{"kind": "video", "urls": [MEDIA_VIDEO]}]


async def test_primary_success_never_calls_media_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: VideoParser())
    service = DouyinService(
        options(tmp_path, parser_provider="parse_video", media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(lambda request: pytest.fail("media-parser must not be called")))
    job = {"source_url": SHORT, "platform": "douyin"}
    parsed = await service._resolve_media(job, service.options.settings)
    assert job["parser_provider"] == "parse_video" and job["parser_fallback"] is False
    assert parsed.assets == [{"kind": "video", "urls": ["https://v3.douyinvod.com/primary.mp4"]}]


async def test_media_parser_primary_supports_xiaohongshu(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: pytest.fail("primary must not run"))
    service = DouyinService(
        options(tmp_path, parser_provider="media_parser", parser_api_url="", media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(media_success))
    job = {"source_url": "https://www.xiaohongshu.com/explore/abcdef123456", "platform": "xiaohongshu"}
    parsed = await service._resolve_media(job, service.options.settings)
    assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is False
    assert parsed.platform == "xiaohongshu"


async def test_media_parser_primary_failure_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: pytest.fail("reverse fallback is forbidden"))

    def broken(request):
        return httpx.Response(400, json={"retcode": 400, "retdesc": "失败", "succ": False,
                                         "error_code": "MEDIA_NOT_FOUND", "data": None})

    service = DouyinService(
        options(tmp_path, parser_provider="media_parser", media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(broken))
    job = {"source_url": SHORT, "platform": "douyin"}
    with pytest.raises(DouyinError) as error:
        await service._resolve_media(job, service.options.settings)
    assert "MEDIA_NOT_FOUND" in str(error.value)


async def test_api_provider_failure_uses_media_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "DouyinAPIParser", lambda *args, **kwargs: FailingParser())
    service = DouyinService(
        options(tmp_path, parser_provider="api", parser_api_url="https://jk.y-l.cc/api/dsp/demo/user/",
                media_parser_url=MEDIA_BASE),
        transport=httpx.MockTransport(media_success))
    job = {"source_url": SHORT, "platform": "douyin"}
    await service._resolve_media(job, service.options.settings)
    assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is True


async def test_empty_media_url_keeps_primary_error(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: FailingParser())
    service = DouyinService(
        options(tmp_path, parser_provider="parse_video", media_parser_url=""),
        transport=httpx.MockTransport(lambda request: pytest.fail("fallback must stay disabled")))
    job = {"source_url": SHORT, "platform": "douyin"}
    with pytest.raises(DouyinError) as error:
        await service._resolve_media(job, service.options.settings)
    assert str(error.value) == "primary parser failed"


async def test_media_parser_fallback_downloads_and_reports_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(douyin_module, "ParseVideoParser", lambda **kwargs: FailingParser())

    def respond(request):
        if request.url.host == "127.0.0.1":
            return media_success(request)
        if request.url.host == "v3.douyinvod.com":
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    service = DouyinService(
        options(tmp_path, parser_provider="parse_video", media_parser_url=MEDIA_BASE, send_results=False),
        transport=httpx.MockTransport(respond))
    await service.start()
    try:
        await service.receive(CallbackEvent.model_validate({**EVENT, "roomid": GROUP, "content": SHORT}))
        await asyncio.wait_for(service._queue.join(), 5)
        job = service.snapshot()["jobs"][0]
        assert job["status"] == "completed", job
        assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is True
        assert len(job["files"]) == 1
    finally:
        await service.stop()
```

保留文件顶部的 `test_download_failure_reparses_with_a_fresh_url` 不变，并在文件顶部补上：

```python
import pytest
```

（`asyncio`、`httpx`、`ParsedMedia`、`DouyinError` 原文件已有。）

在 `tests/test_douyin_fallback.py` 追加：

```python
async def test_api_failure_falls_back_to_media_parser(tmp_path):
    def respond(request):
        if request.url.host == "jk.y-l.cc":
            return httpx.Response(200, json={"code": 108, "msg": "会员接口不存在"})
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, json={
                "retcode": 200, "retdesc": "成功", "succ": True,
                "data": {"title": "mp", "author": {"nickname": "a"}, "video_url": "https://v3.douyinvod.com/mp.mp4"},
            })
        return note_failure(request)

    job, _ = await run_job(
        tmp_path, respond, parser_provider="parse_video", parser_api_url=API,
        media_parser_url="http://127.0.0.1:8051")
    assert job["status"] == "parsed", job["error"]
    assert job["parser_provider"] == "media_parser" and job["parser_fallback"] is True


async def test_non_douyin_api_provider_still_rejects_before_network(tmp_path):
    job, calls = await run_job(
        tmp_path, note_failure, content=XHS, parser_provider="api", parser_api_url=API,
        media_parser_url="http://127.0.0.1:8051")
    assert job["status"] == "failed"
    assert "parse-video-py" in job["error"]
    assert not any("127.0.0.1" in url for url in calls)
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_douyin_retry.py tests/test_douyin_fallback.py -q
```
Expected: FAIL（仍在使用 dudunas / 没有 media 兜底）

- [ ] **Step 3: 修改服务**

`src/wxhook_api/services/douyin.py`：

1. 导入：

```python
from wxhook_api.services.media_parser import MediaParserClient
```

删除：

```python
from wxhook_api.services.parse_fallback import load_dudunas_parser, prefer_watermark_free_video
```

2. 整个 `_resolve_media` 替换为：

```python
    async def _resolve_media(self, job: dict, config) -> Any:
        provider = config.parser_provider
        fallback_used = False
        fallback_url = config.media_parser_url

        async def media_parser():
            if not fallback_url:
                raise DouyinError("未配置 media-parser 本地服务地址")
            return await MediaParserClient(fallback_url, transport=self._transport).parse(job["source_url"])

        if provider == "media_parser":
            parsed = await media_parser()
        elif provider == "parse_video":
            parser = ParseVideoParser(transport=self._transport)
            try:
                parsed = await parser.parse(job["source_url"])
            except DouyinError as primary_error:
                parsed = None
                if job["platform"] == "douyin" and config.parser_api_url:
                    try:
                        parsed = await DouyinAPIParser(self._client, config.parser_api_url).parse(job["source_url"])
                        provider, fallback_used = "api", True
                    except DouyinError as api_error:
                        primary_error = DouyinError(
                            f"parse-video-py 解析失败：{str(primary_error)[:200]}；备用接口也失败：{str(api_error)[:200]}")
                if parsed is None:
                    if not fallback_url:
                        raise primary_error
                    try:
                        parsed = await media_parser()
                        provider, fallback_used = "media_parser", True
                    except DouyinError as fallback_error:
                        raise DouyinError(
                            f"{str(primary_error)[:300]}；media-parser 兜底也失败：{str(fallback_error)[:200]}") from fallback_error
        else:
            parser = DouyinAPIParser(self._client, config.parser_api_url) if provider == "api" else DouyinParser(self._client)
            try:
                parsed = await parser.parse(job["source_url"])
            except DouyinError as primary_error:
                if not fallback_url:
                    raise
                try:
                    parsed = await media_parser()
                    provider, fallback_used = "media_parser", True
                except DouyinError as fallback_error:
                    raise DouyinError(
                        f"{str(primary_error)[:300]}；media-parser 兜底也失败：{str(fallback_error)[:200]}") from fallback_error
        job.update(item_id=parsed.item_id, title=parsed.title, author=parsed.author, kind=parsed.kind,
                   assets=parsed.assets, platform=parsed.platform, parser_provider=provider, parser_fallback=fallback_used)
        return parsed
```

3. `_run()` 中的平台限制改为：

```python
                    if config.parser_provider in {"api", "builtin"} and job["platform"] != "douyin":
                        raise DouyinError("当前解析来源仅支持抖音，请在资源页选择 parse-video-py 或 media-parser 并保存后重新发送链接")
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/test_douyin_retry.py tests/test_douyin_fallback.py tests/test_parse_video.py tests/test_douyin_api.py -q
```
Expected: PASS

---

### Task 5: 删除 dudunas、提升缓存版本、清理测试

**Files:**
- Delete: `src/wxhook_api/services/parse_fallback.py`、`tests/test_parse_fallback.py`
- Modify: `src/wxhook_api/services/douyin_parser.py:18`、`src/wxhook_api/services/media_cache.py:18`
- Modify: `.env.example:3-4`、`parse-video.example.yml:5-7`
- Modify: `tests/test_media_cache.py:30`

**Interfaces:**
- Produces: 仓库内不再有 dudunas 运行代码；`MediaCache.CACHE_VERSION == 3`。

- [ ] **Step 1: 删除代码与测试文件**

```bash
rm src/wxhook_api/services/parse_fallback.py tests/test_parse_fallback.py
```

- [ ] **Step 2: 清理域名与配置示例**

`src/wxhook_api/services/douyin_parser.py` 的 `MEDIA_DOMAINS` 删除末尾 `"api.dudunas.top"`（保留前面的 `"hdslb.com"`）。

`.env.example` 删除：

```
# Optional watermark-removal fallback secret; leave empty to disable. Keep it local.
# WXHOOK_DUDUNAS_SECRET=
```

替换为：

```
# Optional: media-parser local service URL (also used as parse fallback).
# WXHOOK_MEDIA_PARSER_URL=http://127.0.0.1:8051
```

`parse-video.example.yml` 删除：

```yaml
# 可选：去水印兜底接口密钥。留空=不启用；仅在自研解析失败时调用。
# 该密钥只保存在本机 parse-video.local.yml（或 WXHOOK_DUDUNAS_SECRET），不要提交 Git。
dudunas-secret: ''
```

- [ ] **Step 3: 提升缓存版本并更新测试值**

`src/wxhook_api/services/media_cache.py`：

```python
CACHE_VERSION = 3
```

`tests/test_media_cache.py:30`：

```python
        "parser_provider": "media_parser", "parser_fallback": True,
```

- [ ] **Step 4: 全量搜索确认无残留**

Run:
```bash
grep -rn "dudunas\|parse_fallback\|prefer_watermark_free\|WXHOOK_DUDUNAS" src tests --include=*.py --include=*.js --include=*.yml --include=*.html
grep -n "dudunas\|DUDUNAS" .env.example parse-video.example.yml
```
Expected: 两条命令均无输出

- [ ] **Step 5: 运行全量测试**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS

---

### Task 6: 配置页面

**Files:**
- Modify: `src/wxhook_api/web/douyin.html:32-39`
- Modify: `src/wxhook_api/web/douyin.js:10-12,49,60-62,84-86,170-175`
- Modify: `src/wxhook_api/web/manage.js:258`

**Interfaces:**
- Consumes: `settings.media_parser_url`、`supported_parsers` 含 `media_parser`。

- [ ] **Step 1: 修改 HTML**

`src/wxhook_api/web/douyin.html` 的 `select` 改为：

```html
          <select id="parser-provider"><option value="parse_video">parse-video-py（抖音 / 小红书 / B站）</option><option value="media_parser">media-parser（本地服务）</option><option value="api">指定解析接口（仅抖音）</option><option value="builtin">原公开分享页解析（仅抖音）</option></select>
```

在 `api-settings` 结束标签 `</div>` 之后新增：

```html
          <div id="media-parser-settings">
          <label for="media-parser-url">media-parser 本地服务地址</label>
          <input id="media-parser-url" type="url" placeholder="http://127.0.0.1:8051" autocomplete="off" spellcheck="false">
          <p class="field-note">选择 media-parser 时作为主来源；选择其他来源时作为失败兜底。只填写本机回环地址，留空表示禁用。</p>
          </div>
```

- [ ] **Step 2: 修改 douyin.js**

1. `loadConfig()` 中，在 `element("parser-api-url").value = ...` 后新增：

```javascript
    element("media-parser-url").value = data.settings.media_parser_url || "";
```

2. `loadConfig()` 中，在现有 `if (element("parser-provider").value === "api" ...)` 后新增：

```javascript
    if (element("parser-provider").value === "media_parser" && !element("media-parser-url").value.trim()) message("config-status", "选择 media-parser 时请填写本机服务地址，例如 http://127.0.0.1:8051。", true);
```

3. 表单提交中，在 `const apiUrl = ...` 后新增：

```javascript
    const mediaUrl = element("media-parser-url").value.trim();
```

4. 保存请求体新增字段：

```javascript
    await request("/api/douyin/config", { enabled, groups, auto_download: element("auto-download").checked, notify_group: element("notify-group").checked, send_results: element("send-results").checked, parser_provider: provider, parser_api_url: apiUrl, media_parser_url: mediaUrl });
```

5. 保存成功后新增：

```javascript
    if (provider === "media_parser" && !mediaUrl) message("config-status", "配置已保存，但 media-parser 地址为空，解析会失败。", true);
```

6. 解析记录来源文案改为：

```javascript
    if (job.parser_provider) info.append(node("p", "message-meta", `解析来源：${{parse_video: "parse-video-py", api: "指定接口", builtin: "公开分享页", media_parser: "media-parser"}[job.parser_provider] || job.parser_provider}${job.parser_fallback ? "（失败后回退）" : ""}${job.cache_hit ? " · 缓存命中" : ""}${job.platform ? ` · ${{douyin: "抖音", xiaohongshu: "小红书", bilibili: "B站"}[job.platform] || job.platform}` : ""}`));
```

- [ ] **Step 3: 修改 manage.js**

来源文案映射改为：

```javascript
    const provider = {parse_video: "parse-video-py（抖音 / 小红书 / B站）", media_parser: "media-parser（本地服务）", api: "指定接口（仅抖音）", builtin: "公开分享页（仅抖音）"}[media.parser_provider] || media.parser_provider;
```

- [ ] **Step 4: 浏览器手工验证**

1. 启动 wxhook（无需 Hook 也可打开配置页）。
2. 打开 `/douyin`，确认：
   - 下拉出现 `media-parser（本地服务）`；
   - 地址输入框显示 `http://127.0.0.1:8051`；
   - 选择 media-parser 并清空地址后保存，页面显示错误提示；
   - 恢复地址后保存成功，刷新后值保留；
   - 解析记录中 media-parser 来源显示为 `media-parser`，兜底时显示 `（失败后回退）`。
3. 浏览器控制台无报错。

---

### Task 7: 本地服务启动器

**Files:**
- Create: `scripts/media-parser-server.py`
- Create: `scripts/start-media-parser.bat`
- Test: `tests/test_media_parser_launcher.py`

**Interfaces:**
- Produces:
  - `resolve_root(value: str | None) -> pathlib.Path`
  - `bridge_xhs_cookie(project_root: pathlib.Path) -> None`
  - `install_fcntl_stub() -> None`
  - CLI：`python scripts/media-parser-server.py [--root PATH] [--port 8051]`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_media_parser_launcher.py`：

```python
import importlib.util
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "scripts" / "media-parser-server.py"


def load_launcher():
    spec = importlib.util.spec_from_file_location("media_parser_server", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_precedence(tmp_path, monkeypatch):
    module = load_launcher()
    explicit = tmp_path / "explicit"
    configured = tmp_path / "configured"
    monkeypatch.setenv("WXHOOK_MEDIA_PARSER_ROOT", str(configured))
    assert module.resolve_root(str(explicit)) == explicit.resolve()
    assert module.resolve_root(None) == configured.resolve()
    monkeypatch.delenv("WXHOOK_MEDIA_PARSER_ROOT")
    assert module.resolve_root(None) == (PROJECT_ROOT.parent / "media-parser").resolve()


def test_xhs_cookie_is_bridged_without_overwriting_env(tmp_path, monkeypatch):
    module = load_launcher()
    config = tmp_path / "parse-video.local.yml"
    config.write_text("xiaohongshu-cookie: 'a1=private; web_session=test'\n", encoding="utf-8")
    monkeypatch.setenv("WXHOOK_PARSE_VIDEO_CONFIG", str(config))
    monkeypatch.delenv("XHS_COOKIE", raising=False)
    module.bridge_xhs_cookie(PROJECT_ROOT)
    assert __import__("os").environ["XHS_COOKIE"] == "a1=private; web_session=test"

    monkeypatch.setenv("XHS_COOKIE", "existing")
    module.bridge_xhs_cookie(PROJECT_ROOT)
    assert __import__("os").environ["XHS_COOKIE"] == "existing"


def test_missing_root_exits_with_message(tmp_path, capsys):
    module = load_launcher()
    assert module.main(["--root", str(tmp_path / "missing")]) == 2
    assert "not found" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="fcntl is unavailable on Windows only")
def test_fcntl_stub_enables_app_import():
    module = load_launcher()
    sys.modules.pop("fcntl", None)
    module.install_fcntl_stub()
    import fcntl
    assert callable(fcntl.flock)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_media_parser_launcher.py -q`
Expected: FAIL，文件不存在

- [ ] **Step 3: 实现启动器**

创建 `scripts/media-parser-server.py`：

```python
"""Run ucmao/media-parser as a loopback-only service for wxhook."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import types


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    configured = os.getenv("WXHOOK_MEDIA_PARSER_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (PROJECT_ROOT.parent / "media-parser").resolve()


def parse_video_config_path() -> Path:
    return Path(os.getenv("WXHOOK_PARSE_VIDEO_CONFIG") or PROJECT_ROOT / "parse-video.local.yml").expanduser()


def bridge_xhs_cookie(project_root: Path) -> None:
    if os.getenv("XHS_COOKIE", "").strip():
        return
    path = parse_video_config_path()
    try:
        if not path.is_file() or path.stat().st_size > 32 * 1024:
            return
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        value = data.get("xiaohongshu-cookie", "")
    except Exception:
        return
    if isinstance(value, str):
        value = value.strip()
    else:
        return
    if value and all(32 <= ord(char) <= 126 for char in value):
        os.environ["XHS_COOKIE"] = value


def install_fcntl_stub() -> None:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        module = types.ModuleType("fcntl")
        module.LOCK_EX = 2

        def flock(descriptor, operation):
            return None

        module.flock = flock
        sys.modules["fcntl"] = module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root")
    parser.add_argument("--port", type=int, default=int(os.getenv("WXHOOK_MEDIA_PARSER_PORT", "8051")))
    args = parser.parse_args(argv)
    root = resolve_root(args.root)
    if not (root / "app.py").is_file() or not (root / "src" / "parsers").is_dir():
        print(f"media-parser source not found at {root}", file=sys.stderr)
        return 2
    bridge_xhs_cookie(PROJECT_ROOT)
    os.environ["API_ONLY"] = "true"
    os.environ.setdefault("TZ", "Asia/Shanghai")
    install_fcntl_stub()
    sys.path.insert(0, str(root))
    os.chdir(root)
    from app import app as application  # noqa: E402
    from waitress import serve  # noqa: E402

    serve(application, host="127.0.0.1", port=args.port, threads=4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

创建 `scripts/start-media-parser.bat`：

```bat
@echo off
setlocal
set "PROJECT=%~dp0.."
"%PROJECT%\.venv\Scripts\python.exe" "%PROJECT%\scripts\media-parser-server.py" %*
if errorlevel 1 pause
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_media_parser_launcher.py -q`
Expected: PASS

---

### Task 8: 运行时依赖

**Files:**
- Modify: `requirements-runtime.lock`

**Interfaces:**
- Produces: wxhook `.venv` 可直接运行 media-parser（Flask/requests/waitress 等）。

- [ ] **Step 1: 加入锁文件**

在 `requirements-runtime.lock` 中按字母序插入以下行（版本来自本次解析结果）：

```
beautifulsoup4==4.12.3
blinker==1.9.0
charset-normalizer==3.5.2
Flask==3.1.3
Flask-Cors==6.0.5
itsdangerous==2.2.0
Jinja2==3.1.6
MarkupSafe==3.0.4
mini-racer==0.12.4
pycryptodome==3.20.0
requests==2.33.0
soupsieve==3.0.4
urllib3==2.8.0
waitress==3.0.2
Werkzeug==3.1.9
```

保留现有 `certifi==2026.7.22`、`click==8.5.0`、`idna==3.20`、`lxml==6.1.3`、`python-dotenv==1.2.4`。

- [ ] **Step 2: 安装并验证**

Run:
```bash
.venv/Scripts/python.exe -m pip install -r requirements-runtime.lock
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -c "import flask, flask_cors, requests, bs4, lxml, waitress, py_mini_racer, Crypto; print('deps ok')"
```
Expected: `pip check` 输出 `No broken requirements found.`，最后一行为 `deps ok`

- [ ] **Step 3: 回归测试**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS

---

### Task 9: 重启脚本生命周期与 PowerShell 回归

**Files:**
- Modify: `scripts/restart-project.ps1`（新增函数、调整顺序与步骤编号）
- Modify: `tests/restart_project.Tests.ps1`

**Interfaces:**
- Produces:
  - `Get-MediaParserRoot -Root <path>`
  - `Get-MediaParserProcesses -Root <path> -Snapshot <array>`
  - `Stop-MediaParserService -Root <path>`
  - `Start-MediaParserService -Root <path> -Python <path>`

- [ ] **Step 1: 更新 PowerShell 测试**

在 `tests/restart_project.Tests.ps1` 中：

1. 在 `Write-Output 'PID reuse protection: passed'` 之后新增：

```powershell
$previousMediaRoot = $env:WXHOOK_MEDIA_PARSER_ROOT
try {
    $env:WXHOOK_MEDIA_PARSER_ROOT = 'E:\Media Parser'
    Assert-True ((Get-MediaParserRoot $root) -eq 'E:\Media Parser') 'Configured media-parser root was ignored.'
    Remove-Item Env:WXHOOK_MEDIA_PARSER_ROOT
    Assert-True ((Get-MediaParserRoot $root) -eq 'D:\Project With Spaces\media-parser') 'Default sibling media-parser root is incorrect.'
} finally {
    if ($null -ne $previousMediaRoot) { $env:WXHOOK_MEDIA_PARSER_ROOT = $previousMediaRoot }
    else { Remove-Item Env:WXHOOK_MEDIA_PARSER_ROOT -ErrorAction SilentlyContinue }
}
$mediaLauncher = Join-Path $root 'scripts\media-parser-server.py'
$mediaSnapshot = @(
    (Process-Record 201 $venv ('"' + $venv + '" "' + $mediaLauncher + '"')),
    (Process-Record 202 'C:\Python312\python.exe' 'python.exe scripts\media-parser-server.py'),
    (Process-Record 203 $venv 'python.exe unrelated.py')
)
$mediaIds = @(Get-MediaParserProcesses $root $mediaSnapshot | ForEach-Object { $_.ProcessId })
Assert-True (($mediaIds -join ',') -eq '201') 'media-parser ownership included unrelated Python.'
Write-Output 'media-parser root and ownership: passed'
```

2. 在“simulated failed pull”测试中，将三个 stub 函数替换为：

```powershell
function Update-ProjectCode { param($Root) throw 'simulated failed pull' }
function Stop-ProjectService { param($Root) throw 'BUG: stopped service after failed pull' }
function Start-MediaParserService { param($Root, $Python) throw 'BUG: started media-parser after failed pull' }
function Start-ProjectService { param($Root, $Python, $Configuration) throw 'BUG: started service after failed pull' }
```

3. 在 stub 区块中，将 `Start-ProjectService` 之前新增：

```powershell
function Start-MediaParserService { param($Root, $Python) $script:events += 'start-media' }
```

4. 三处顺序断言改为：

```powershell
Assert-True (($script:events -join ',') -eq 'pull,dependencies,stop,start-media,start') 'Restart order is incorrect.'
Assert-True (($script:events -join ',') -eq 'dependencies,stop,start-media,start') 'Local mode unexpectedly pulled code.'
Assert-True (($script:events -join ',') -eq 'pull,dependencies') 'Dependency failure stopped the old service.'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/restart_project.Tests.ps1`
Expected: FAIL，`Get-MediaParserRoot` 未定义

- [ ] **Step 3: 修改重启脚本函数**

在 `scripts/restart-project.ps1` 的 `Stop-ProjectService` 之后、`Test-ServicePort` 之前插入：

```powershell
function Get-MediaParserRoot {
    param([string]$Root)
    $configured = $env:WXHOOK_MEDIA_PARSER_ROOT
    if ($configured) { return [System.IO.Path]::GetFullPath($configured) }
    return [System.IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $Root) 'media-parser'))
}

function Get-MediaParserProcesses {
    param([string]$Root, [object[]]$Snapshot)
    $launcher = Join-Path $Root 'scripts\media-parser-server.py'
    $interpreters = @((Join-Path $Root '.venv\Scripts\python.exe'), (Join-Path $Root '.venv\Scripts\pythonw.exe'))
    foreach ($item in $Snapshot) {
        if ($item.Name -notin @('python.exe', 'pythonw.exe')) { continue }
        if ($item.ExecutablePath -notin $interpreters) { continue }
        if (-not ([string]$item.CommandLine).Contains($launcher)) { continue }
        $item
    }
}

function Stop-MediaParserService {
    param([string]$Root)
    $targets = @(Get-MediaParserProcesses $Root (Get-ProcessSnapshot))
    foreach ($target in $targets) {
        if (Test-SameProcess $target) {
            Write-Host "Stopping media-parser PID $($target.ProcessId)..."
            Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        if (-not @(Get-MediaParserProcesses $Root (Get-ProcessSnapshot)).Count) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The old media-parser process has not stopped.'
}

function Start-MediaParserService {
    param([string]$Root, [string]$Python)
    $mediaRoot = Get-MediaParserRoot $Root
    Stop-MediaParserService $Root
    if (-not (Test-Path -LiteralPath (Join-Path $mediaRoot 'app.py'))) {
        Write-Host "media-parser not installed at $mediaRoot; parsing fallback stays disabled."
        return
    }
    $launcher = Join-Path $Root 'scripts\media-parser-server.py'
    if (-not (Test-Path -LiteralPath $launcher)) {
        Write-Warning "Missing $launcher; parsing fallback stays disabled."
        return
    }
    $port = 8051
    if ($env:WXHOOK_MEDIA_PARSER_PORT) {
        try { $port = [int]$env:WXHOOK_MEDIA_PARSER_PORT } catch { throw 'WXHOOK_MEDIA_PARSER_PORT must be an integer.' }
    }
    if ($port -lt 1 -or $port -gt 65535) { throw 'WXHOOK_MEDIA_PARSER_PORT is out of range.' }
    $health = "http://127.0.0.1:$port/api/health"
    if (Test-ServicePort $health) {
        Write-Warning "Port $port is already in use; media-parser was not started."
        return
    }
    $outLog = Join-Path $Root 'data\media-parser.stdout.log'
    $errLog = Join-Path $Root 'data\media-parser.stderr.log'
    $process = Start-Process -FilePath $Python -ArgumentList @('"' + $launcher + '"', '--root', ('"' + $mediaRoot + '"'), '--port', "$port") -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        try {
            $status = Invoke-RestMethod -Uri $health -TimeoutSec 2 -UseBasicParsing
            if ($status.status -eq 'ok') {
                $statePath = Join-Path $Root 'data\runtime\media-parser.json'
                New-Item -ItemType Directory -Path (Split-Path -Parent $statePath) -Force | Out-Null
                [IO.File]::WriteAllText($statePath, (@{pid=$process.Id; root=$mediaRoot; port=$port; started_at=(Get-Date).ToString('o')} | ConvertTo-Json))
                Write-Host "media-parser ready: $health (PID $($process.Id))"
                return
            }
        } catch { }
        $process.Refresh()
        if ($process.HasExited) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    Write-Warning "media-parser did not become ready. Check $outLog and $errLog; wxhook will still start."
}
```

- [ ] **Step 4: 调整重启顺序**

`Restart-Project` 中：

```powershell
    Write-Host '[1/5] Updating code...'
```

```powershell
    Write-Host '[2/5] Preparing dependencies; the old service is still running...'
```

```powershell
    Write-Host '[3/5] Stopping the old project service...'
    Stop-ProjectService $Root
    Write-Host '[4/5] Starting media-parser local service...'
    Start-MediaParserService $Root $python
    Write-Host '[5/5] Starting the updated service...'
    Start-ProjectService $Root $python $configuration
```

- [ ] **Step 5: 运行 PowerShell 测试**

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/restart_project.Tests.ps1`
Expected: 输出包含 `media-parser root and ownership: passed` 与 `restart ordering, local mode, and dependency failure: passed`

---

### Task 10: 文档与最终验收

**Files:**
- Modify: `docs/GUIDE.md:485-495` 及 jk 接口段落
- Modify: `THIRD_PARTY_NOTICES.md`
- Test: 全量后端测试 + 浏览器 + 手工 E2E

**Interfaces:**
- 无新增代码接口。

- [ ] **Step 1: 重写 GUIDE 段落**

将 `docs/GUIDE.md` 中：

```
### 可选：去水印兜底、下载重试与媒体缓存
...
这里的去水印是优先选择平台无水印媒体源，或使用兜底接口返回的无水印文件；不做像素擦除，作者嵌入画面的水印仍会保留。
```

替换为：

```markdown
### media-parser 本地解析服务（可选来源与失败兜底）

[media-parser](https://github.com/ucmao/media-parser)（MIT）是独立的本地解析服务。
抖音使用无水印播放端点、小红书优先取原画、B站返回含音轨的 MP4，因此不再依赖第三方去水印接口。

安装只需一次：把 media-parser 克隆到 wxhook 项目目录的上一级（测试机 `D:\media-parser`，开发机 `F:\media-parser`）。
之后双击 `update-and-start.bat` 会自动安装依赖、停止旧进程、启动 media-parser，再启动 wxhook。
media-parser 启动失败或健康检查超时只会告警，不影响 wxhook 的其他功能。

在 `/douyin` 页面可以：

- 把“解析来源”选为 `media-parser（本地服务）`，直接用本地服务解析；
- 保持 `parse-video-py`、`指定解析接口` 或 `公开分享页` 时，主解析失败后自动再尝试 media-parser 兜底；
- 修改“media-parser 本地服务地址”（默认 `http://127.0.0.1:8051`）。地址只允许本机回环地址，留空表示禁用。

选择 media-parser 作为主来源时，它失败后不会再反向回退到 parse-video-py。
小红书 Cookie 仍然只填写在 `parse-video.local.yml` 的 `xiaohongshu-cookie`，启动器会自动传给 media-parser，不需要复制到别处。

更新 media-parser：`cd D:\media-parser && git pull`，然后重启 wxhook。
日志在 `data/media-parser.stdout.log` 与 `data/media-parser.stderr.log`。
media-parser 的数据库与会话密钥保存在其源码目录，不进入 wxhook 的备份和更新。
如果修改端口，需要同时设置 `WXHOOK_MEDIA_PARSER_PORT` 和页面里的服务地址。

下载失败仍会清理未完成文件、重新解析并最多重试 3 次。
同一平台和源链接的媒体缓存 7 天；本次升级提升了缓存版本，旧缓存不会命中，第一次解析会重新获取媒体。
media-parser 只处理抖音、小红书和 B站；媒体回传仍走原有的群权限、大小限制和自动保存规则。
这里的“无水印”指选择平台的无水印媒体源，不做像素擦除，作者嵌入画面的水印仍会保留。
```

并把 jk 接口段落中的：

```
API 模式失败时不会自动改用其他解析来源；如需继续使用原解析方式，可在页面明确选择“原公开分享页解析”并保存。
```

替换为：

```
API 模式失败时，如果配置了 media-parser 地址，会自动再尝试本地 media-parser 兜底；没有配置地址时保持原错误。
```

- [ ] **Step 2: 补第三方说明**

在 `THIRD_PARTY_NOTICES.md` 的 `## jieba (optional)` 之前新增：

```markdown
## media-parser

- Project: https://github.com/ucmao/media-parser
- Tested revision: `19496f8c253ee0c18a2e2e27e23267c76aecaa63`
- License: MIT

Installed and run as a separate local service; source code is not vendored
into this repository. The launcher in `scripts/media-parser-server.py` only
sets up a loopback-bound process and does not modify upstream files. Users
clone and update the upstream repository themselves.
```

- [ ] **Step 3: 手工端到端验证**

```bash
# 1) 一次性克隆（开发机）
git clone https://github.com/ucmao/media-parser.git F:/media-parser

# 2) 前台启动，确认健康检查
.venv/Scripts/python.exe scripts/media-parser-server.py --root F:/media-parser
curl http://127.0.0.1:8051/api/health
```

另开终端启动 wxhook，打开 `/douyin`，按以下顺序验证：

1. 选择 media-parser，依次解析抖音视频、抖音图文、小红书视频、小红书图文、B站视频；
2. 确认直链可下载、抖音/小红书为无水印源、回传群消息正常；
3. 切回 parse-video-py，人为让主解析失败，确认自动兜底且来源显示 `media-parser（失败后回退）`；
4. 停掉 media-parser，确认主解析成功时不受影响、失败时错误提示可读；
5. 双击 `update-and-start.bat`，确认旧 media-parser 被停止、新进程就绪、wxhook 正常。

若开发机无法直连平台，以上步骤在测试机执行，并在汇报中注明哪些项未验证。

- [ ] **Step 4: 最终检查**

Run:
```bash
.venv/Scripts/python.exe -m pytest -q
ruff check src tests
.venv/Scripts/python.exe -m compileall -q src
.venv/Scripts/python.exe -m pip check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/restart_project.Tests.ps1
```
Expected: 全部通过；无 dudunas 残留（设计/计划文档除外）

---

## Self-Review

- **Spec coverage:** 客户端映射与限制（Task 1）、配置与回环校验（Task 2）、单次解析兜底（Task 3）、群聊链路与平台限制（Task 4）、删除 dudunas 与缓存迁移（Task 5）、页面（Task 6）、启动器（Task 7）、依赖（Task 8）、重启生命周期（Task 9）、文档与验收（Task 10）——spec 各节均有对应任务。
- **Placeholder scan:** 无 TBD/TODO；所有代码、命令与期望输出均已写明。
- **Type consistency:** `MediaParserClient.parse`、`settings.media_parser_url`、`Get-MediaParserRoot` 等名称在各任务中一致；`parser_provider="media_parser"`、`parser_fallback` 语义与现有字段一致。
