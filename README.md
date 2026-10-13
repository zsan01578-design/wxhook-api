# wxhook-api

基于 FastAPI 的 WeChat Hook HTTP API 桥接服务。项目根据 `默认模块(1).md` 中的接口契约实现，将本地业务调用转发到已经运行的 WeChat Hook HTTP 服务，并接收 Hook 推送的消息回调。

> 当前目标版本：WeChat 4.1.10.27
>
> 本项目是 HTTP 桥接层，不是微信 Hook 注入实现。

## 目录

- [项目架构](#项目架构)
- [功能范围](#功能范围)
- [运行环境](#运行环境)
- [安装](#安装)
- [配置](#配置)
- [启动](#启动)
- [接口列表](#接口列表)
- [请求与响应约定](#请求与响应约定)
- [消息回调](#消息回调)
- [错误处理](#错误处理)
- [测试](#测试)
- [项目结构](#项目结构)
- [安全边界与限制](#安全边界与限制)
- [相关文件](#相关文件)

## 项目架构

服务由两个 HTTP 端点组成：

```text
业务调用方
    │
    ▼
FastAPI 桥接层  http://127.0.0.1:8000
    │
    │ HTTPX 转发
    ▼
现有 WeChat Hook 服务  http://127.0.0.1:30001
    │
    │ POST 消息事件
    ▼
FastAPI 回调入口  /receiveChatBotMsg
```

默认端口含义：

| 服务 | 默认地址 | 说明 |
|---|---|---|
| FastAPI 桥接层 | `127.0.0.1:8000` | 对外提供与文档一致的代理接口，并接收回调 |
| WeChat Hook 上游 | `127.0.0.1:30001` | 由现有 Hook 程序提供，FastAPI 不负责启动 |

FastAPI 层通过 `HookClient` 调用上游，不加载 `version.dll`，不启动 `weixin_4.1.10.27.exe`，不执行 DLL 注入，也不猜测微信进程内部实现。

## 功能范围

当前桥接层提供：

- 配置文件和环境变量加载
- HTTPX 异步上游客户端
- 微信账号、媒体、消息、数据库、联系人和历史记录接口代理
- 上游 JSON、数组、扩展字段和 HTTP 状态码透传
- 消息回调接收与有界内存队列
- 回调处理器注册和单 worker 顺序分发
- 回调 URL 白名单校验
- 请求体大小限制
- 非法 JSON、上游超时、连接失败和非 JSON 响应处理
- FastAPI OpenAPI 文档
- pytest 契约测试和边界回归测试

当前不包含：

- 微信进程 Hook 或 DLL 注入
- 图片、语音、数据库的底层解密算法
- 微信进程内 SQLite 实现
- 自动回复业务
- 持久化消息队列
- 图片自动下载和语音转文字执行器
- 用户认证系统和公网部署配置

## 运行环境

- Windows
- Python 3.11 或更高版本
- 已运行并监听 `30001` 端口的 WeChat Hook HTTP 服务
- Git Bash、PowerShell 或其他终端均可运行

检查 Python：

```bash
python --version
```

## 安装

在项目根目录执行：

```bash
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

如果系统没有 `py` 命令，也可以使用已安装的 Python 3.11：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

依赖包括：

- FastAPI
- Uvicorn
- HTTPX
- Pydantic 2
- pydantic-settings
- PyYAML
- pytest
- pytest-asyncio
- asgi-lifespan

## 配置

### `version.yml`

项目保留原始 Hook 配置文件 `version.yml`：

```yaml
# 提供接口的端口：现有 Hook 上游端口
start-port: 30001
# Hook 消息回调地址
callback-url: "http://localhost:8000/receiveChatBotMsg"
# 是否开启自动下载
image-auto-download: true
# 自动下载白名单，逗号分隔
image-download-white-list: "xxx"
# 语音转文字
voice-to-text-enable: true
# 语音转文字白名单，逗号分隔
voice-to-text-white-list: "wxid_9n2t6xh1huzm22,wxid_xkc126b78mea22,47668773796@chatroom"
```

`start-port` 是上游 Hook 端口，不是 FastAPI 桥接层端口。FastAPI 默认监听 `127.0.0.1:8000`。

### 环境变量

可以复制 `.env.example` 作为配置参考。应用启动时读取环境变量，环境变量优先于内置默认值；原始 `version.yml` 不会被应用改写。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WXHOOK_CONFIG` | 项目根目录 `version.yml` | YAML 配置文件路径 |
| `WXHOOK_HOST` | `127.0.0.1` | FastAPI 监听地址 |
| `WXHOOK_PORT` | `8000` | FastAPI 监听端口 |
| `WXHOOK_HOOK_BASE_URL` | `http://127.0.0.1:30001` | 上游 Hook 地址 |
| `WXHOOK_CONNECT_TIMEOUT` | `2` | 上游连接超时秒数 |
| `WXHOOK_READ_TIMEOUT` | `10` | 上游读取超时秒数 |
| `WXHOOK_MAX_BODY_BYTES` | `1048576` | HTTP 请求体最大字节数 |
| `WXHOOK_CALLBACK_QUEUE_SIZE` | `1000` | 回调内存队列容量 |
| `WXHOOK_SHUTDOWN_TIMEOUT` | `5` | 服务关闭时 worker 回收超时秒数 |
| `WXHOOK_LOG_LEVEL` | `INFO` | Uvicorn 日志级别 |
| `WXHOOK_ALLOWED_CALLBACK_URLS` | 本机回调地址 | 允许 `/set_callback` 使用的 URL，多个地址用逗号分隔 |

Git Bash 临时设置示例：

```bash
export WXHOOK_HOOK_BASE_URL=http://127.0.0.1:30001
export WXHOOK_PORT=8000
```

也可以使用 `.env.example` 中的变量作为本地环境配置参考，但不要把真实 Token、密码或私钥写入 `.env.example` 或 Git 仓库。

## 启动

### 模块方式启动

```bash
WXHOOK_CONFIG=F:/wxhook/version.yml .venv/Scripts/python.exe -m wxhook_api
```

默认启动地址：

```text
http://127.0.0.1:8000
```

### Uvicorn 启动

```bash
.venv/Scripts/python.exe -m uvicorn wxhook_api.main:app --host 127.0.0.1 --port 8000
```

当前默认使用单进程，避免回调配置、事件队列和 handler 注册状态在多进程之间不一致。

### 健康检查

```bash
curl http://127.0.0.1:8000/health
```

示例响应：

```json
{
  "status": "ok",
  "hook_base_url": "http://127.0.0.1:30001",
  "hook_port": 30001
}
```

健康检查只表示 FastAPI 进程正常运行，不代表微信已经登录或上游 Hook 一定可用。

### OpenAPI 文档

启动后可访问：

- `/docs`：Swagger UI
- `/redoc`：ReDoc
- `/openapi.json`：OpenAPI JSON

## 接口列表

以下接口路径和大小写与 `默认模块(1).md` 保持一致，FastAPI 会将请求转发到同名的上游 Hook 接口。

### 账号与基础信息

| 方法 | 路径 | 请求 |
|---|---|---|
| GET | `/GetFileSavePath` | 无请求体 |
| POST | `/GetDecodeKey` | 空请求体或 `{}` |
| POST | `/GetSelfProfile` | 空请求体或 `{}` |
| GET | `/QueryDB/status` | 无请求体 |

`/GetSelfProfile` 的返回字段保留文档中的原始拼写，包括 `proiv`。

### 消息与媒体

| 方法 | 路径 | 请求字段 |
|---|---|---|
| POST | `/SendVoiceMsg` | `wxid`, `path` |
| POST | `/SendTextMsg` | `wxidorgid`, `msg`, 可选 `atlist` |
| POST | `/SendImgMsg` | `wxidorgid`, `path` |
| POST | `/GetMessagePic` | `msgid` |
| POST | `/Decode_Pic` | `src_path`, `dst_path` |
| POST | `/send_file_msg` | `wxid`, `filepath`，可选 `stage` |

`atlist` 支持逗号分隔字符串或字符串数组，也可以传 `notify@all`。调用方负责在 `msg` 中拼接 @ 显示文本。

`stage` 支持：

```text
construct
fill
scene
send
```

未提供 `stage` 时默认使用 `send`。

### 数据库、联系人和历史记录

| 方法 | 路径 | 请求字段 |
|---|---|---|
| POST | `/QueryDB/execute` | `optDbName`, `SQL` |
| POST | `/QueryDB/GetAllDBName` | 空请求体或 `{}` |
| POST | `/Contact/search` | `keywords: string[]` |
| POST | `/Message/history` | `wxid`，可选 `size`, `page` |

联系人搜索的 OR 匹配、`username` 去重和最多 20 条结果由上游 Hook 负责。

聊天历史的消息分表定位、跨数据库合并和内容解码由上游 Hook 负责。桥接层保留 `wxid`、分页参数和上游返回数据。

### 回调配置

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/set_callback` | 校验白名单后转发上游回调配置 |
| POST | `/receiveChatBotMsg` | 接收 Hook 推送的消息事件 |

## 请求与响应约定

### 普通响应

消息和图片类接口通常使用：

```json
{
  "ret": 0,
  "retmsg": "success"
}
```

### PB 响应

文件消息等 PB 接口可能使用：

```json
{
  "ret": 0,
  "retmsg": "success",
  "data": {}
}
```

### QueryDB 响应

数据库接口通常使用：

```json
{
  "status": 0,
  "desc": "",
  "data": []
}
```

`/QueryDB/GetAllDBName` 也可能直接返回数据库数组，`/QueryDB/status` 返回 `IsLogin` 和 `hWeixin` 对象。桥接层不会强行把这些不同响应改成统一结构。

### 响应透传

桥接层会保留：

- 上游实际 HTTP 状态码
- JSON 对象或数组
- 文档未定义的扩展字段
- 上游业务错误字段和错误码
- 字符串形式的大数 ID

不会把上游业务错误自动伪装成成功，也不会用过窄的 Pydantic 响应模型裁剪数据。

### 非法 JSON

客户端发送非法 JSON 或非法 UTF-8 时返回：

```json
{
  "ret": -1,
  "msg": "invalid json"
}
```

合法 JSON 但字段缺失或类型错误时返回 HTTP 422。

## 消息回调

Hook 推送到 `/receiveChatBotMsg` 的事件至少包含：

```json
{
  "event_type": 1001,
  "msgid": 191284230691951358,
  "type": 1,
  "timestamp": 1710000000,
  "wxid": "wxid_xxx",
  "sender": "wxid_xxx",
  "roomid": "",
  "content": "hello"
}
```

事件模型允许保留未定义的扩展字段，`msgid` 支持整数或字符串，以避免大数精度丢失。

### 回调处理流程

1. FastAPI 校验回调字段。
2. 事件进入有界内存队列。
3. 成功入队后立即返回：

   ```json
   {"ret": 0, "retmsg": "accepted"}
   ```

4. 单个后台 worker 按注册顺序调用 handler。
5. handler 异常只记录错误类型，不记录消息正文或异常详情，worker 继续处理后续事件。

队列满时返回 HTTP 503。队列只存在于当前进程内，不提供持久化、崩溃恢复或恰好一次投递保证；重复事件不会仅因为 `msgid` 为 0 而被丢弃。

### 回调 URL 安全

`/set_callback` 只允许 `WXHOOK_ALLOWED_CALLBACK_URLS` 中的完整 URL：

- scheme 必须是 `http` 或 `https`
- 主机、端口和路径必须完全匹配
- 拒绝用户名、密码和 URL fragment
- 不允许任意外部 URL，避免 SSRF

## 错误处理

| 情况 | HTTP 状态 | 响应 |
|---|---:|---|
| 非法 JSON / UTF-8 | 400 | `{ "ret": -1, "msg": "invalid json" }` |
| 合法 JSON 但参数错误 | 422 | `validation_error` |
| 回调 URL 不在白名单 | 422 | `callback_url_not_allowed` |
| 回调队列已满 | 503 | `callback_queue_full` |
| 上游连接失败或响应不可用 | 502 | `hook_transport_error` |
| 上游请求超时 | 504 | `hook_timeout` |
| 上游返回合法 JSON 的非 2xx | 保留上游状态码 | 保留上游 JSON |

上游返回的 `ret`、`status`、`msg` 或 `desc` 属于业务协议字段，不会被桥接层擅自转换成 HTTP 错误。

## 测试

运行全部测试：

```bash
.venv/Scripts/python.exe -m pytest -q
```

运行编译检查：

```bash
.venv/Scripts/python.exe -m compileall -q src
```

测试覆盖：

- YAML 配置和环境变量覆盖
- FastAPI 端口与 Hook 端口分离
- 自代理地址拒绝
- HTTPX 请求方法、路径和请求体
- 上游 JSON、数组、扩展字段和 HTTP 状态码透传
- 连接失败、非 JSON 响应和超时
- 全部文档接口
- 非法 JSON 和非法 UTF-8
- 分块请求体大小限制
- 回调字段、大整数、扩展字段和队列满
- worker 关闭和 handler 异常隔离
- 应用生命周期不会启动时发送消息、执行 SQL 或设置回调

## 项目结构

```text
wxhook-api/
├── .env.example
├── .gitignore
├── pyproject.toml
├── version.yml
├── version.dll
├── 默认模块(1).md
├── src/
│   └── wxhook_api/
│       ├── __main__.py
│       ├── main.py
│       ├── clients/
│       │   └── hook_client.py
│       ├── core/
│       │   ├── body_limit.py
│       │   ├── config.py
│       │   └── errors.py
│       ├── routes/
│       │   ├── account.py
│       │   ├── callbacks.py
│       │   ├── contacts.py
│       │   ├── dependencies.py
│       │   ├── health.py
│       │   ├── history.py
│       │   ├── media.py
│       │   ├── messages.py
│       │   └── querydb.py
│       ├── schemas/
│       │   ├── events.py
│       │   ├── requests.py
│       │   └── responses.py
│       └── services/
│           └── events.py
└── tests/
    ├── test_callbacks.py
    ├── test_config.py
    ├── test_hook_client.py
    ├── test_lifecycle.py
    ├── test_regressions.py
    └── test_routes.py
```

## 安全边界与限制

- 默认绑定回环地址 `127.0.0.1`，不建议直接暴露到公网。
- 不在日志中记录消息正文、SQL、数据库密钥、AES 密钥、Token 或完整异常堆栈。
- 不把 `.env`、虚拟环境、缓存、`.tools` 和微信安装器上传到 Git。
- `weixin_4.1.10.27.exe` 约 239 MB，超过 GitHub 普通单文件限制，已加入 `.gitignore`。
- `version.dll` 作为现有运行时资产保留在项目中，但本桥接层不加载或执行它。
- 执行真实消息发送、SQL 查询、获取密钥或修改上游回调前，请确认上游 Hook 已正确运行并了解操作影响。
- 当前实现不提供身份认证；如需跨机器或公网使用，应在反向代理或受控网络层增加认证、TLS 和访问控制。

## 相关文件

- `默认模块(1).md`：原始 WeChat-Hook HTTP API 文档
- `version.yml`：Hook 端口和消息处理配置
- `.env.example`：FastAPI 桥接层环境变量示例
- `pyproject.toml`：Python 依赖和测试配置
- `tests/`：契约、集成和边界回归测试

## 许可证

当前仓库未声明开源许可证。使用或分发前请根据 `version.dll`、微信程序及相关接口的授权情况自行确认合规性。
