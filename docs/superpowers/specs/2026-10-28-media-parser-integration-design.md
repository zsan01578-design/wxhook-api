# media-parser 接入设计

- 日期：2026-10-28
- 状态：设计已确认，待实现
- 目标仓库：F:\wxhook（wxhook-api）
- 外部项目：https://github.com/ucmao/media-parser （MIT，Flask 本地解析服务）

## 1. 背景与目标

现有去水印能力来自第三方固定接口 `api.dudunas.top`（`services/parse_fallback.py`），存在两个问题：

1. 密钥依赖外部服务，可用性和内容质量不可控；
2. 小红书等平台仍可能拿到带水印媒体。

`ucmao/media-parser` 是本地原生解析项目，抖音走 `watermark=0` 的播放端点、小红书优先取 `originVideoKey` 原画、B站返回已含音轨的 durl 文件，本身就是无水印解析。

本次改动的目标：

- 删除 dudunas 去水印兜底及其"视频优先无水印覆盖"逻辑；
- 以独立本地服务的形式接入 media-parser；
- media-parser 可作为解析来源直接选用，也可作为其他来源失败后的自动兜底；
- 平台范围保持不变：抖音、小红书、B站。

## 2. 非目标

- 不放开 media-parser 支持的其余 47 个平台；
- 不改动 jk.y-l.cc「指定解析接口」与「公开分享页」两条既有来源；
- 不自动更新 media-parser 源码（由使用者手动 `git pull`）；
- 不做像素级去水印，作者嵌入画面的水印仍会保留；
- 不引入 Docker/WSL 依赖。

## 3. 总体架构

```
微信回调
   │
   ▼
DouyinService._resolve_media  ──►  ParseVideoParser (parse-video-py)
   │                                  │ 失败
   │                                  ├─► DouyinAPIParser (jk.y-l.cc，仅抖音)
   │                                  └─► MediaParserClient ──HTTP──► media-parser
   │                                                                  127.0.0.1:8051
   ├─ provider=media_parser ─────────► MediaParserClient              (API_ONLY=true)
   ├─ provider=api / builtin ────────► 既有解析器，失败后同样兜底到 MediaParserClient
   │
   ▼
下载 / 缓存 / 回传（流程不变）

POST /api/parse（网页单次解析）
   └─ ParseVideoParser 失败 ─► MediaParserClient
```

media-parser 与 wxhook 是两个进程，只通过本机回环 HTTP 通信；wxhook 不 import 它的代码，避免 `src`/`configs`/`utils` 包名冲突和同步 `requests` 阻塞 FastAPI 事件循环。

## 4. 组件一：media-parser 本地服务（外部）

### 4.1 运行方式

- 源码位置：`<wxhook 项目目录的父目录>/media-parser`，默认即
  - 测试机：`D:\media-parser`
  - 开发机：`F:\media-parser`
- 可用环境变量 `WXHOOK_MEDIA_PARSER_ROOT` 覆盖。
- 使用 wxhook 的 `.venv` 运行，不单独建虚拟环境。
- 监听 `127.0.0.1:8051`，环境变量 `API_ONLY=true`，免鉴权、不写请求日志。
- 端口可用 `WXHOOK_MEDIA_PARSER_PORT` 覆盖（需与 wxhook 配置的 `media-parser-url` 保持一致）。

### 4.2 启动器 `scripts/media-parser-server.py`

职责：

1. 解析源码目录（参数 `--root` > 环境变量 > 项目父目录），校验存在 `app.py` 与 `src/parsers`，否则退出并打印明确原因；
2. Windows 下 `fcntl` 不存在：在导入 `app` 之前向 `sys.modules` 注入空实现（`LOCK_EX` 常量、`flock` 空函数），不修改上游源码；
3. 设置 `API_ONLY=true`、`TZ=Asia/Shanghai`；
4. 小红书 Cookie 桥接：读取 wxhook 的 `parse-video.local.yml`（路径规则与 `parse_video.parser_config_path()` 一致，支持 `WXHOOK_PARSE_VIDEO_CONFIG`），若其中 `xiaohongshu-cookie` 非空且环境变量 `XHS_COOKIE` 未设置，则注入 `XHS_COOKIE`。Cookie 不写日志、不回显；
5. `chdir` 到源码目录并把其加入 `sys.path`，导入 `app` 模块；
6. 用 waitress 以 4 线程启动 WSGI 应用，不启用 reloader。

启动器只服务于本机，不监听 `0.0.0.0`。

### 4.3 健康检查

- media-parser 自带 `GET /api/health`，返回 `{"status":"ok"}`。
- 重启脚本启动后用该接口确认就绪，超时 15 秒。

### 4.4 生命周期

`scripts/restart-project.ps1` 调整顺序：

1. 更新代码；
2. 准备依赖（现有逻辑，锁文件新增依赖后自动触发安装）；
3. 停止旧 wxhook 服务（现有逻辑）；
4. 停止旧 media-parser（PID 文件 `data/runtime/media-parser.json` + PID/可执行文件/命令行三重校验，只杀本项目启动器）；
5. 启动 media-parser（隐藏窗口，日志写 `data/media-parser.stdout.log` / `data/media-parser.stderr.log`），等待健康检查；
6. 启动 wxhook。

失败策略：

- media-parser 源码目录不存在：提示"未安装 media-parser，解析兜底不可用"，继续启动 wxhook；
- media-parser 启动失败或健康检查超时：打印告警和日志路径，继续启动 wxhook；
- 8051 已被非本项目进程占用：不杀进程，告警并跳过。

额外提供 `scripts/start-media-parser.bat`，用于前台调试或单独手动启动（可见窗口）。

## 5. 组件二：wxhook 客户端适配层

新增 `src/wxhook_api/services/media_parser.py`。

### 5.1 接口调用

- 地址：`{base}/api/v1/parse`
- 方法：`POST`，`Content-Type: application/json`，请求体 `{"url": <已校验的分享链接>}`
- 客户端：`httpx.AsyncClient`，`trust_env=False`，不跟随重定向，连接超时 2s、读取超时 30s、总超时 35s
- 响应读取：流式读取并限制 2 MB，超出即报错
- 单次调用，不自动重试

### 5.2 响应映射为 `ParsedMedia`

成功判定：HTTP 2xx 且 JSON 中 `succ is True` 且 `retcode == 200` 且 `data` 为对象。

| media-parser 字段 | ParsedMedia |
| --- | --- |
| `video_id` | `item_id`（截断 100） |
| `title`，为空时用 `desc` | `title`（截断 2000，仍为空则"未命名作品"） |
| `author.nickname` | `author`（截断 200） |
| `image_list` 非空 | `kind="images"`，逐项 `{"kind":"image","urls":[url]}` |
| `video_url` | `kind="video"`，`{"kind":"video","urls":[url]}` |
| 无图无视频 | 抛出解析错误 |
| — | `platform = source_platform(source)`（以请求链接判定，不信任响应里的中文平台名） |

`video_list`（多视频/合集）本次不展开，只取 `video_url` 主视频，与现有单视频语义保持一致。响应中的 `audio_url`、`cover_url`、`subtitles`、`_tip` 均忽略。

图片项规则：

- 字符串：直接作为 URL；
- 对象：取 `url` 字段；
- 实况图的 `live_photo_url` 本次忽略（与 parse-video-py 的图集语义保持一致）；
- 任一项无效 → 报错，不回传残缺图集；
- 超过 100 张 → 报错。

所有媒体 URL 必须通过 `checked_url(url, media=True)` 校验。若联调发现哪个平台的合法 CDN 不在 `MEDIA_DOMAINS`/`PLATFORM_HOSTS` 中，按最小化原则补域名，不放松整体校验。

### 5.3 错误映射

| 情况 | 对外错误信息 |
| --- | --- |
| 连接被拒 / 连接超时 | `media-parser 本地服务连接失败，请确认 127.0.0.1:8051 已启动` |
| 读取超时 | `media-parser 解析超时，请稍后重试` |
| 响应含 `retcode`/`error_code`（HTTP 400/429/503 等） | 按下列业务错误处理，不直接报 HTTP 状态 |
| `error_code=XIAOHONGSHU_COOKIE_REQUIRED` | `media-parser 提示小红书需要登录 Cookie，请检查 parse-video.local.yml 的 xiaohongshu-cookie` |
| 其他 `retcode != 200` | `media-parser 解析失败：{retdesc}（{error_code}）`（各自截断） |
| HTTP 非 2xx 且无业务 JSON | `media-parser 返回 HTTP {status}` |
| 响应无效 / 过大 | 对应的固定中文提示 |

上游业务失败时 HTTP 状态与 `retcode` 同值（如 400），客户端必须先解析 JSON 再判断，避免把可操作的 Cookie/权限提示降级成 HTTP 状态码。

错误信息不包含 Cookie、请求头或完整内部异常。

## 6. 组件三：配置与界面

### 6.1 配置模型 `GroupSettings`

- `parser_provider` 取值增加 `media_parser`：
  `Literal["parse_video", "api", "builtin", "media_parser"]`
- 新增 `media_parser_url: str = "http://127.0.0.1:8051"`
  - 校验：`http`/`https`；hostname 仅允许 `127.0.0.1`、`localhost`、`::1`；必须显式端口 1–65535；不允许用户名/密码/查询串/片段；path 必须为空或 `/`
  - 目的：配置页属于本机管理界面，但仍限制为回环地址，避免被填入内网地址形成 SSRF
  - 允许留空：表示禁用 media-parser（不兜底）；此时 `parser_provider` 不能为 `media_parser`，保存时返回明确校验错误
  - 空值与来源组合由 `GroupSettings` 的 `model_validator` 统一校验，避免只在页面上拦截
- 环境变量 `WXHOOK_MEDIA_PARSER_URL` 优先于配置文件
- 持久化键：`media-parser-url`
- `parser-config-version` 保持 2（新增字段有默认值，不需要重置旧配置）

### 6.2 配置接口

- `GET /api/douyin/config` 的 `supported_parsers` 增加 `"media_parser"`；
  前端用 `supported_parsers` 判断页面/后台版本是否一致，需同步更新。
- `POST /api/douyin/config` 接受新字段，由 `GroupSettings` 校验。

### 6.3 页面

`web/douyin.html`：

- 解析来源下拉新增 `<option value="media_parser">media-parser（本地服务）</option>`；
- 新增 `media-parser-url` 输入框与说明：选择 media-parser 时作为主来源，选择其他来源时作为失败兜底；只填写本机地址；留空表示禁用。

`web/douyin.js`：

- 加载/保存 `media_parser_url`；
- `updateProvider()` 维持既有 `api-settings` 显隐逻辑；
- 解析记录来源文案映射新增 `media_parser: "media-parser"`，删除 `dudunas`。

`web/manage.js`：

- 群管理里的来源文案映射同步新增 `media_parser`，删除 `dudunas`。

## 7. 组件四：解析链路

### 7.1 群聊解析 `services/douyin.py::_resolve_media`

删除 `load_dudunas_parser` / `prefer_watermark_free_video` 调用，新链路：

```
provider == "parse_video":
    ParseVideoParser
      └ 失败且平台为抖音且有 parser_api_url → DouyinAPIParser（标记 fallback）
      └ 仍失败 → MediaParserClient（provider=media_parser, fallback=true）
provider == "media_parser":
    MediaParserClient（fallback=false；失败直接报错，不反向回退）
provider == "api" / "builtin":
    既有解析器
      └ 失败 → MediaParserClient（provider=media_parser, fallback=true）
```

- 兜底失败时错误信息合并主解析与兜底原因：
  `"{主解析错误前 300 字}；media-parser 兜底也失败：{兜底错误前 200 字}"`
- 不再有"解析成功后优先替换为无水印视频"的第二步。
- `_run()` 中的平台限制改为：`api` 和 `builtin` 仅支持抖音；`parse_video` 和 `media_parser` 支持抖音/小红书/B站。

### 7.2 单次解析接口 `routes/parse.py`

- 主解析仍是 `ParseVideoParser`；
- 失败后读取 `request.app.state.douyin.options.settings.media_parser_url`，非空时调用 `MediaParserClient`；
- 兜底成功则正常返回；失败则合并错误返回 400；地址为空则直接返回主解析错误。

## 8. 组件五：删除 dudunas

| 文件 | 改动 |
| --- | --- |
| `services/parse_fallback.py` | 删除 |
| `tests/test_parse_fallback.py` | 删除 |
| `services/douyin.py` | 删除 import、`fallback` 变量、prefer 覆盖逻辑 |
| `routes/parse.py` | 删除 import 与 dudunas 分支 |
| `services/douyin_parser.py` | `MEDIA_DOMAINS` 移除 `api.dudunas.top` |
| `.env.example` | 移除 `WXHOOK_DUDUNAS_SECRET` |
| `parse-video.example.yml` | 移除 `dudunas-secret` 及注释 |
| `docs/GUIDE.md` | 重写"可选：去水印兜底、下载重试与媒体缓存"章节为 media-parser 说明 |
| `tests/conftest.py` | 移除 `WXHOOK_DUDUNAS_SECRET`，新增清理 `WXHOOK_MEDIA_PARSER_URL` |
| `tests/test_douyin_retry.py` | dudunas 兜底用例改为 media-parser 兜底 |
| `tests/test_media_cache.py` | provider 样例值 `dudunas` → `media_parser` |
| `web/douyin.js`、`web/manage.js` | 来源文案 |

测试机 `parse-video.local.yml` 中残留的 `dudunas-secret` 不会被读取，可直接删除，不强制。

## 9. 缓存迁移

`services/media_cache.py` 的 `CACHE_VERSION` 从 2 升到 3：

- 原因：旧缓存可能是带水印媒体，升级后若继续命中，用户会误以为接入无效；
- 效果：版本不匹配的旧目录不再被读取，7 天后按 TTL 自动清理，无需手工删文件。

## 10. 依赖与部署

### 10.1 运行时依赖

`requirements-runtime.lock` 新增（版本以 media-parser `requirements.txt` 和当前 Python 3.12 环境解析结果为准，安装后跑 `pip check`）：

- Flask 3.1.3
- Flask-Cors 6.0.5
- requests 2.33.0
- beautifulsoup4 4.12.3
- mini-racer 0.12.4（Windows `win_amd64` wheel 已验证可下载）
- pycryptodome 3.20.0
- waitress 3.0.2

`lxml`、`python-dotenv` 已存在，无需重复添加。Flask 的传递依赖（werkzeug、jinja2、itsdangerous、blinker、soupsieve 等）按实际解析结果补全并固定版本。

### 10.2 一次性安装步骤（开发机 / 测试机）

```bash
# 测试机
git clone https://github.com/ucmao/media-parser.git D:/media-parser
# 开发机
git clone https://github.com/ucmao/media-parser.git F:/media-parser
# 然后双击 update-and-start.bat，脚本会自动安装新依赖并拉起 media-parser
```

media-parser 的后续更新：

```bash
cd D:/media-parser && git pull
# 重启 wxhook 服务（restart-project.ps1 会顺带重启 media-parser）
```

### 10.3 与其他配置的关系

- `parse-video.local.yml` 仍是小红书 Cookie 的唯一来源；
- media-parser 自己的 `.env` 可选，仅在其需要额外平台 Cookie（抖音登录态等）时使用；
- media-parser 的 `data/`（SQLite、会话密钥）位于其源码目录，不进入 wxhook 的备份和更新；
- 去水印兜底不再使用任何外部付费接口，不需要密钥。

## 11. 安全与隐私

- media-parser 只监听回环地址；wxhook 配置只接受回环地址。
- 客户端不跟随重定向，媒体与页面地址统一走 `checked_url` 白名单与端口限制。
- 响应体 2 MB 上限、图集 100 张上限，防止异常响应拖垮进程。
- Cookie 仅通过进程环境变量传递，不写日志、不进 API 响应、不落盘到 wxhook。
- 错误信息只保留可操作的中文提示，不回显内部异常与请求头。

## 12. 测试计划

### 12.1 单元测试

新增 `tests/test_media_parser.py`：

- 视频响应映射（title/author/video_id/platform）；
- 图集响应映射（字符串项、对象项、忽略 live_photo_url）；
- `title` 为空时回退 `desc`；
- 超过 100 张、无效图片项、无媒体 → 报错；
- `retcode` 非 200、`XIAOHONGSHU_COOKIE_REQUIRED`、非 JSON、超大响应、连接失败 → 对应错误文案；
- 非白名单媒体地址 → 拒绝；
- 响应中平台名与请求链接不一致时以请求链接为准。

更新：

- `tests/test_douyin_retry.py`：parse_video 失败→api 失败→media-parser 成功；api 失败→media-parser 成功；media_parser 主选失败不回退；兜底失败错误合并；
- `tests/test_parse_routes` 相关用例（现有在 `test_parse_fallback.py` 中）迁移为 media-parser 兜底；
- `tests/test_media_cache.py` provider 文案。

### 12.2 配置测试

- `media_parser_url` 合法值（`http://127.0.0.1:8051`、`http://localhost:9000`、`http://[::1]:8051`）；
- 非法值（非回环主机、带路径、缺端口、带账号、https 以外的协议）；
- 空值允许保存，但此时 `parser_provider=media_parser` 必须被拒绝；
- `WXHOOK_MEDIA_PARSER_URL` 覆盖配置文件；
- 保存后重新加载保持一致。

### 12.3 手工验证

1. 本地启动 media-parser（前台可见日志），确认 `/api/health` 返回 ok；
2. 启动 wxhook，配置页切到 media-parser，分别用抖音视频、抖音图文、小红书视频/图文、B站视频验证：
   - 返回直链可下载；
   - 抖音/小红书链接为无水印源；
   - 回传群消息正常；
3. 切回 parse-video-py，人为让主解析失败（断网或换不支持链接），确认自动兜底到 media-parser 且解析记录来源显示"media-parser（失败后回退）"；
4. 停掉 media-parser，确认主解析成功时不受影响、主解析失败时错误提示可读；
5. 重启脚本验证：旧 media-parser 被正确停止，新进程启动，wxhook 正常。

若开发环境无法直连抖音/小红书，手工验证在测试机执行；开发机用 mock 测试覆盖映射与链路，并在交付说明中如实标注未验证项。

## 13. 回滚方案

- 代码回滚到本次提交之前即可恢复 dudunas 逻辑（配置中的 `media-parser-url` 会被旧代码忽略）。
- media-parser 进程可单独关闭，不影响 wxhook 既有来源。
- 缓存版本升级不可逆，但旧缓存仅是失效，不影响功能。

## 14. 验收标准

- [ ] 运行代码、配置模板、有效文档中不再引用 dudunas（设计文档和 Git 历史除外）；
- [ ] 配置页可选 media-parser，并可保存/回显回环地址；
- [ ] media-parser 主选可完成抖音/小红书/B站解析与下载；
- [ ] 其他来源失败能自动兜底到 media-parser，来源标记正确；
- [ ] media-parser 未启动时，主解析路径行为不变，错误提示可读；
- [ ] 重启脚本能自动拉起 media-parser，失败时不阻断 wxhook；
- [ ] 全量 `pytest` 通过，`pip check` 无冲突；
- [ ] GUIDE 文档更新，说明安装、更新、Cookie 桥接和排障。
