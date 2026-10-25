# wxhook-api

基于 FastAPI 的微信 Hook 本地增强服务：在已有 Hook 之上提供群聊/成员同步、聊天归档、@机器人问答、生图、群工具、关键词规则、提醒和分享解析。默认只监听本机回环地址，不注入微信进程。

目标微信版本为 **WeChat 4.1.10.27**；接口契约见 [`默认模块(1).md`](默认模块(1).md)。

## 快速开始

Windows 10/11，Python 3.11 或更高版本（已验证 3.12）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item version.example.yml version.yml
# 按本机情况修改 version.yml 中的 Hook 端口和回调配置
.\.venv\Scripts\python.exe main.py
```

`version.yml` 已加入 `.gitignore`，不要提交真实 wxid、群 ID 或白名单。未创建本机 `version.yml` 时，服务会临时使用 `version.example.yml`；正式运行前请生成。

也可双击 `update-and-start.bat` 更新并重启，或使用 `start-local.bat` 只重启当前版本。管理页默认打开 `http://127.0.0.1:8000/`。

## 架构

```text
WeChat Hook 30001
    │ POST /receiveChatBotMsg
    ▼
FastAPI 8000 ── Access/BodyLimit → 持久化收件 → 会话有序分发
    ├─ 归档：账号核验、消息入库、历史同步
    └─ 自动处理：问答/生图 → 群工具 → 分享解析
          │ 发送前再次检查账号、群和成员策略
          ▼
      Hook / 对话模型 / 生图模型 / 搜索
```

管理数据库启用 WAL；长查询使用独立只读连接。回调先落盘再确认，自动处理前写入不重放边界；模型调用有每日额度、有序备用和短时故障冷却。

## 主要能力

- 群聊/成员同步、聊天记录归档、黑名单与群开关
- @机器人问答：多轮上下文、成员隔离、联网搜索、token 用量统计
- 图片理解：@机器人发图或对最近图片追问（需在 AI 模型勾选“支持图片理解”）
- 生图：独立模型配置、语义识别、图片回传
- 群工具：周报/月报、关键词规则、待办与群提醒
- 抖音/小红书/B站分享解析、媒体回传与本地清理
- 数据保留、FTS5 消息搜索、管理库存储统计、在线备份、磁盘空间保护
- 管理库 WAL 读写解耦、模型 token 用量统计、故障配置短时冷却
- 本机回环信任边界、可选管理员口令、DPAPI 密钥保护

## 文档

- [完整使用与运维手册](docs/GUIDE.md)：配置、接口、AI、群工具、媒体、备份、故障恢复和安全边界
- [历史审查快照](docs/reviews/README.md)：仅供追溯，部分问题已修复
- [第三方声明](THIRD_PARTY_NOTICES.md)

常用手册入口：[一键启动](docs/GUIDE.md#一键启动微信hook和聊天页面)、[AI 问答](docs/GUIDE.md#群内-机器人问答一条提问最多一条回复)、[备用模型](docs/GUIDE.md#多个第三方配置按顺序切换)、[数据保留](docs/GUIDE.md#数据保留全文搜索与存储统计)、[安全边界](docs/GUIDE.md#安全边界与限制)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\ruff.exe check src
```

当前后端回归 **1138 项通过、1 项跳过（239.45 秒）**；跳过项是测试机无法创建 Windows 重解析点的文件系统测试。`tests/browser/` 另有 9 组 Playwright 脚本，需本机安装 Playwright CLI 和 Chrome。CI 配置见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

测试使用临时数据库、模拟 Hook 和模拟模型，不发送真实微信消息，也不调用真实模型/搜索服务。真实微信、IPv6 Hook、目标机长时间运行仍需实机验收。

## 安全与合规

默认绑定 `127.0.0.1`，并校验 Host、Origin、CSP 和跨站请求标志；远程访问需显式配置 HTTPS、`WXHOOK_ALLOWED_HOSTS` 和管理员口令。本项目不包含微信 DLL 注入、底层解密或多用户权限系统。使用前请自行确认 Hook、微信及相关接口的授权与合规性。

当前仓库未声明开源许可证，分发前请确认 `version.dll`、微信程序及第三方依赖的授权情况。
