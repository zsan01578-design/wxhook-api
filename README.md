# wxhook-api

## 微信风格的群聊管理系统

在 PyCharm 运行根目录 `main.py`，完成微信登录后，打开 **`http://127.0.0.1:8000/`**（或 `/manage`）。
首页现为群聊管理工作台，原来的消息收发工具移到 `/messages`，抖音资源页面仍在 `/douyin`。
不需要安装前端工具或额外的数据库服务。

- 微信登录后自动通过只读数据库查询同步全部可读取群聊，不受联系人搜索 20 条上限影响；左侧支持搜索、运行状态筛选、置顶和手动添加。
- 自动同步可读取的完整群成员关系和微信名称；不兼容的成员表会标记为“部分发现”，不会假装完整。历史成员和手动添加记录会保留。
- 每个群可分别开启“抖音分享解析”和“自动保存视频与图片”；群功能总开关可暂停该群全部自动功能并保留设置。
- 群聊可加入或移出黑名单；成员黑名单仅在对应群生效，不影响其在其他群的功能使用。
- 群名称和成员名称默认跟随微信同步；自定义名称、备注、功能开关和黑名单不会被同步覆盖。清空本地名称可恢复跟随微信。
- “群消息”可查看持久化聊天记录，支持全文搜索与翻页；左下角可检查连接、启用消息接收，顶部可查看进度并立即同步。

**黑名单停止的是本系统自动处理，不会退出微信群、踢出成员或屏蔽微信消息。**
拉黑或暂停后，新消息不再触发解析；正在解析的任务会在解析返回后检查限制，下载在下一个数据块检查并停止，未完成文件会清理。
群消息仍可以记录并在“群消息”查看。已保存文件不受影响。

管理配置、群与成员、完整聊天记录、历史导入断点及最近 500 条操作记录保存在项目 `data/management.sqlite3`，重启保留，已忽略 Git。
可用 `WXHOOK_MANAGEMENT_DB` 指定数据库路径。备份时停止服务并复制该文件。
原 `/messages` 收发工具仍只展示进程内最近 100 条回调；管理系统的“群消息”使用持久化记录，不受此上限影响。

### 自动同步与聊天记录

自动同步默认开启，直接运行 `main.py` 即可。微信未登录或 Hook 暂时断开时，服务等待并自动重试，已保存记录仍可离线查看。
首次登录后读取群聊与成员名单，再分批调用 `/Message/history` 导入当前电脑微信本地可读取的全部群历史；每批每群最多 5 页、每页 200 条，未完成的群自动续传，没有总记录条数截断。
完成首次导入后默认每 60 秒刷新名单和增量消息；导入未完成或连接中断时每 5 秒继续尝试。
顶部状态条显示当前群与进度，“立即同步”只安排同一个后台任务，不会重复启动导入。

实时群回调同时写入资料库；历史与回调有相同非零消息 ID 时合并，ID 以字符串保存，避免大整数精度丢失。
没有服务端 ID 的历史记录按来源数据库、本地 ID 等信息识别；没有有效 ID 的实时回调独立保存，可能与无有效 ID 的历史记录同时存在。
历史导入只归档，不进入自动功能处理队列，因此不会重新解析旧抖音链接或重复下载旧媒体。
黑名单只控制自动功能，不影响聊天记录归档。

同步范围是**当前电脑的微信本地可读数据**，无法取回微信本机已删除、未同步到本机或仅在其他设备上的消息。
图片、语音、文件等消息保存接口返回的文字/XML 与原始字段，不批量下载聊天附件。
群名册使用表和字段探测，支持 `contact / chat_room / chatroom_member` 关联关系与旧版 `ChatRoom.MemberList`；不支持的成员二进制格式会明确提示。
适配时参考了[公开数据库结构说明](https://github.com/bojieli/greenbubbles/blob/main/docs/WECHAT_DATABASE_FORMAT.md)，实际是否可读取仍以测试机 Hook 返回的表结构为准。

旧管理资料库会自动增加所需表和字段，保留已有配置。同步只添加或更新资料，不删除本地群档案或已保存历史。
首次确认登录账号后会绑定当前微信账号；如果切换账号，自动同步和群回调归档会拒绝写入同一资料库。
新账号请设置独立 `WXHOOK_MANAGEMENT_DB` 后重启，避免混合两个微信账号的聊天记录。

可选环境变量：`WXHOOK_SYNC_ENABLED=false` 关闭后台同步；`WXHOOK_SYNC_INTERVAL=60` 设置增量同步间隔（秒，最小 5）。
系统中没有运行微信 Hook 时可使用模拟接口进行自动化测试；真实群名、完整成员关系和本机历史覆盖范围仍需在登录微信的测试机验收。

已有 `douyin.yml` / `douyin.local.yml` 中的群名单会自动加入管理列表。
尚未单独修改的群沿用旧解析设置；在管理系统明确设置过的功能开关优先于 `/douyin` 的默认配置。
新发现或手动添加的群默认不开启解析，需要自行打开，已拉黑的成员和群始终受限。

管理接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/manage/state` | 群列表、运行状态与成员黑名单 |
| POST | `/api/manage/sync` | 安排一次后台同步，返回 202 |
| GET | `/api/manage/groups/{群ID}/messages?page=1&size=50&q={关键词}` | 查询已归档聊天记录，分页大小最大 200，关键词为可选字面文本 |
| POST | `/api/manage/groups` | 添加本地管理群 |
| PATCH | `/api/manage/groups/{群ID}` | 名称、备注、置顶、功能开关、群黑名单 |
| GET / POST | `/api/manage/groups/{群ID}/members` | 查询或手动添加成员 |
| PATCH | `/api/manage/groups/{群ID}/members/{成员wxid}` | 成员备注与黑名单 |
| GET | `/api/manage/audit?group={群ID}` | 查询最近 100 条操作记录 |
| GET | `/api/manage/contacts?keyword={关键词}` | 从微信联系人中搜索群聊（最多 20 条联系人中的群） |

## 指定群聊的抖音视频与图文解析

### 指定解析接口

默认解析来源已切换为 `jk.y-l.cc` 的 GET 接口。在测试电脑打开 `/douyin`，选择“指定解析接口”，
将接口方提供的**完整地址**填入“完整接口地址”（以 `https://jk.y-l.cc/api/dsp/` 开头，含接口标识和用户 ID），
不要附加 `?url=` 参数，保存后立即生效。请求会自动以 `url` 查询参数传入群内分享链接。
接口地址含账号相关标识，只保存到本机 `douyin.local.yml`，仓库不包含你的实际地址；
也可用 `WXHOOK_DOUYIN_API_URL` 环境变量提供，环境变量在启动时优先读取。

该接口以 **`code=200`** 判断成功，成功示例中的 `status=101` 不作为失败处理。
视频读取 `data.url`，`data.down` 作为候选备用地址；图集优先读取 `data.pics`，为空时兼容 `data.download_image`。
封面 `data.cover` 不会被当作图集回传。图片数组支持 URL 字符串或含 `url`、`src`、`image_url`、`down` 的对象。
成功响应里没有可用媒体地址时仍会报错，不会显示虚假的解析成功。

接口连接失败、返回非 JSON、业务失败码或无效媒体地址都会进入原来的群内失败提示流程。
例如 `code=108`、`msg=会员接口不存在` 表示接口账号/地址当前不可用，需要接口提供方处理开通或更新地址。
API 模式失败时不会自动改用其他解析来源；如需继续使用原解析方式，可在页面明确选择“原公开分享页解析”并保存。
实际保存与回传的是接口返回的媒体文件，去水印和内容质量取决于接口提供方。

### 群内提示与自动回传

启用群聊解析后，默认会在**分享来源群**发送“开始解析”提示；解析或下载失败时发送失败原因；
下载完成后把媒体回传同一个群，再发送完成提示。图片按图集顺序发送，全部 Hook 调用返回明确成功后才显示“已回传”。
开始提示失败不会阻止继续解析，所有提示的发送状态可在资源页面查看。

图片使用 `/SendImgMsg`；当前 Hook 文档没有独立的视频发送接口，因此视频通过 `/send_file_msg` 作为 **MP4 文件**发送，
不是原生视频消息气泡。WebP、AVIF 等静态图片生成 PNG 副本用于图片消息，原下载文件保留；动态图以原文件回传以保留动画。
图片转换使用 Pillow，更新这版后在测试电脑的项目虚拟环境执行一次 `python -m pip install -e .`，以后仍只运行 `main.py`。

在 `/douyin` 的默认设置中，可分别关闭“在来源群提示开始、失败和完成”和“将视频或图片发回来源群”，保存后立即生效。
这些提示/回传设置应用于所有实际开启了解析的群；群的总开关、解析开关、自动保存开关和群/成员黑名单仍独立生效。
回传必须同时开启该群的自动保存。关闭自动保存时，会提示未回传，不会把没有发送的媒体标为成功。

回传前、每个文件发送前以及图片转换后会检查群与成员限制。发送中遇到失败或超时即停止后续文件，
页面区分“解析失败”“部分回传”“回传失败”和“回传待确认”，并保留已经下载的文件。
发送超时可能已到达微信群，不自动重试；请先查看群聊结果。同一非零消息 ID 在当前运行和最近已持久化的任务记录中去重，
服务重启不会自动重发历史任务。自动提示不附带源分享链接，避免自己的回调再次触发解析。
可设置 `WXHOOK_DOUYIN_FEEDBACK_ENABLED=false` 并重启，临时关闭所有群内自动发送，保留解析下载功能。

运行 `main.py` 后，在测试电脑打开 **`http://127.0.0.1:8000/douyin`**，也可从群管理侧栏点击“资源”。
此功能与 Python 服务一起运行，不需要启动额外程序。

1. 先让目标群收到一条消息；配置页面的“最近收到消息的群”会显示该群 ID，点击可添加。
   也可以手动填写 `123456789@chatroom` 形式的真实群 ID，每行一个，不能使用群昵称。
2. 选择解析来源并配置接口地址，勾选“启用群聊抖音解析”，保留“解析成功后自动保存视频和图片”，点击“保存群配置”。
   默认名单保存后立即生效；空名单或未启用时不处理默认群，管理系统中已独立开启的群以单独配置为准。
3. 让群内成员发送抖音视频或图文分享文案，也支持卡片 XML 中包含的抖音链接。
4. 页面会依次显示“排队中 → 解析中 → 保存中 → 回传中 → 已回传”，展开记录可查看、播放、下载文件和查看每个文件发送状态。
   如果关闭自动保存，则只展示解析出的媒体链接。

默认文件位置为项目下 `downloads/douyin/<任务ID>/`：视频保存为 `001.mp4`，图集按顺序保存为
`001.webp`、`002.webp` 等（以实际文件格式为准）。`result.json` 记录作品标题、作者、来源群、发送人和文件列表。
页面显示最近 100 条记录，已保存文件和结果记录重启后可继续查看；未完成的任务需要重新发送链接。
同一条回调重复推送不会重复创建任务，同一个链接在另一条新群消息中发送则会重新解析。

页面配置保存在被 Git 忽略的 `douyin.local.yml`，下载文件同样不会提交 GitHub。
未通过页面保存配置时，读取仓库中的 `douyin.yml`；也可直接编辑该文件后重启：

```yaml
enabled: true
groups:
  - '123456789@chatroom'
  - '987654321@chatroom'
auto-download: true
notify-group: true
send-results: true
parser-provider: api
parser-api-url: '' # 请在资源页填写接口方提供的完整地址，保存到本机配置
output-dir: downloads/douyin
max-file-mb: 100
```

存在 `douyin.local.yml` 时优先读取本地配置；也可以用 `WXHOOK_DOUYIN_CONFIG` 指定其他配置文件。
默认单文件最大 100 MB（配置范围 1–500 MB），每个作品总量最大 500 MB，图集最多 100 张，后台队列最多 50 个任务。
群消息接收不等待网络解析或媒体下载完成。对于沿用默认配置的群，禁用默认功能或移除群后会停止未完成的下载；独立配置的群需在管理系统中停用。
接口不可用或媒体下载失败时会显示原因，不会将网页错误内容保存为视频。内置解析模式下，公开页面需要登录验证、作品不可用或结构变化同样会报错。

相关接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/api/douyin/config` | 查看或保存群名单、启用状态和自动保存设置 |
| GET | `/api/douyin/jobs` | 查看解析状态、媒体链接与本地文件列表 |
| GET | `/api/douyin/files/{任务ID}/{文件名}` | 播放或查看本地媒体，`?download=true` 下载 |

## 一键启动微信、Hook 和聊天页面

测试机已将 `version.dll`、`version.yml` 放在 `Weixin.exe` 同目录后，
只需在 PyCharm 中运行项目根目录的 **`main.py`**。
当前打开的 `src/wxhook_api/main.py` 也支持直接运行，`python -m wxhook_api` 同样使用一键入口。

首次使用请确认项目根目录 `launcher.yml` 中的微信路径：

```yaml
wechat-path: 'F:\app\Weixin\Weixin.exe'
open-browser: false
```

一键入口会启动 Python 服务并唤起原版微信窗口；如果 Hook 尚未响应，则启动微信（已运行的微信会复用）。
微信自身加载旁边的 Hook DLL。程序在后台等待 Hook 和账号登录，登录后自动设置消息回调。
同时会查找原版微信主窗口，从最小化或托盘隐藏状态恢复显示并请求切换到前台；已运行的微信同样会唤起。
如果 Windows 拒绝前台焦点切换，会通过闪烁微信任务栏图标提醒，点击该图标即可进入。
默认 `open-browser: false`，启动时显示原版微信。群管理可手动访问 `http://127.0.0.1:8000/`，消息工具为 `/messages`；
如需同时自动打开网页，将 `open-browser` 改为 `true`，原版微信窗口仍会自动唤起。
不再需要额外启动另一个 HTTP 服务或手动点击“启用消息接收”。Hook 断开、账号退出后会继续等待，恢复后重新设置回调。
扫码、确认登录仍需要在微信窗口中完成，等待期间聊天页面可以正常打开并显示启动进度。

停止 PyCharm 运行或在终端按 `Ctrl+C` 会关闭 Python 服务，微信保持运行。
如果 `8000` 已被旧版服务占用，请先停止旧服务后再运行一次。
如果对应的一键服务已经运行，再次启动入口会复用服务并唤起原版微信窗口。
启动器直接交由 Uvicorn 绑定端口，不再用额外的试绑定判断端口占用。
如果启动失败，控制台会保留原始系统错误；`netstat` 中只有 `TIME_WAIT` 不表示该端口有服务正在监听，
排查占用时应查看本地端口对应的 `LISTENING` 记录。
微信路径也可以通过 `WXHOOK_WECHAT_PATH` 环境变量覆盖。
一键启动用于 Python 和微信在同一台测试机的场景。

## 本地消息页面（收发文本）

启动 Python 服务后，在运行微信和 Hook 的测试电脑上打开 `http://127.0.0.1:8000/messages`。
页面提供微信连接检查、启用消息接收、发送文本和收到消息的回复入口，不需要安装前端工具。

测试电脑更新步骤：

1. 在原 Python 服务终端按 `Ctrl+C` 停止服务。
2. 将开发电脑项目里的 `main.py`、`launcher.yml`、`src`、`tests`、`pyproject.toml`、`README.md` 同步到测试电脑的原项目目录。
   保留测试机自己的 `.venv`、`version.yml` 和微信安装目录里的 Hook 文件。
3. 更新后首次在 PyCharm 的项目终端执行（下面假设测试机项目在 `F:\wxhook`）：

   ```powershell
   cd F:\wxhook
   .\.venv\Scripts\python.exe -m pip install -e .
   ```

4. 在 PyCharm 右键运行项目根目录 `main.py`。原版微信窗口会自动打开，在微信完成登录。
   如需使用网页收发工具，手动访问 `http://127.0.0.1:8000/`（或将 `open-browser` 改为 `true`）。
   页面顶部应显示“微信已登录”，回调状态应变为“消息接收已自动启用”。若显示旧页面，按 `Ctrl+F5` 刷新。
5. 下次启动只需再次运行 `main.py`，无需重复安装依赖或手动启动其他服务。
   “启用消息接收”按钮仍可用于手动重设回调。
6. 点击 `filehelper` 填入文件传输助手 ID，输入测试文本并发送，在微信中确认消息出现。
7. 让另一个微信账号给当前登录账号发送一条文本，页面右侧应在约 2 秒内出现消息。
8. 点击该消息的“回复”，输入内容并发送，在对方微信确认收到。群消息的回复对象为对应群聊。

验收标准：页面显示已登录、发出的文本能在微信中确认、外部账号的新消息出现在页面、回复能到达对方。
点击“启用消息接收”成功只表示 Hook 接受回调设置，收到真实消息才表示回调链路已验证。

如果发送失败，页面保留草稿并显示错误；若请求超时，请先确认微信里是否已经发送，避免重复发送。
如果接收不到，确认页面回调状态已显示启用，Hook 与 Python 都在同一台测试电脑运行，且 Python 服务未退出。
也可以直接访问 `http://127.0.0.1:8000/ui/messages` 查看是否有已接收的回调记录。

消息列表仅在当前进程内保留最近 100 条，重启后清空；单条内容最多显示 20,000 个字符，超出会标记截断。
第一版发送功能仅支持文本，非文本回调显示原始内容，不自动下载媒体或自动回复。

可选自动测试（使用模拟 Hook，不会发送真实微信消息）：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

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

FastAPI 层通过 `HookClient` 调用上游；一键入口负责打开已安装的 `Weixin.exe`，由微信自行加载其目录中的 Hook DLL。
Python 不直接加载或注入 `version.dll`，也不执行微信安装器 `weixin_4.1.10.27.exe`。

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
- 已安装对应版本微信，并将 Hook DLL、配置放置在微信主程序同目录；一键入口会启动微信并等待 `30001` 就绪
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

与运行根目录 `main.py` 一样，会一键启动微信、打开聊天页面并自动设置回调。

```bash
WXHOOK_CONFIG=F:/wxhook/version.yml .venv/Scripts/python.exe -m wxhook_api
```

默认启动地址：

```text
http://127.0.0.1:8000
```

### Uvicorn 启动

此方式只运行 API 服务，适合自行管理微信和回调的场景。

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

测试机提供的 Hook 中，`/QueryDB/GetAllDBName` 实际返回 `{"ret":0,"retmsg":"success","data":[{"dbName":"contact.db","dbHandle":123}]}`；其他版本也可能直接返回数据库数组，或使用 `status: 0` 包装。
自动同步同时兼容这些成功响应，以及 `ret: 0` 外层包装的 SQL `status: 0` 结果；出现任一非零错误码时仍按失败处理。
`/QueryDB/status` 返回 `IsLogin` 和 `hWeixin` 等字段。桥接原始接口保持响应透传，兼容处理只发生在管理系统内部。

仓库自带 Hook 的历史记录使用 `msgid / timestamp / type / content / sender / sender_nickname` 字段；同步器同时支持早期文档中的
`server_id / create_time / local_type / message_content / real_sender` 字段，保留消息 ID、原始时间、正文和发送人，并与实时回调去重。
同步失败时页面会指出失败阶段与 HTTP / Hook 状态码，便于区分数据库清单、SQL 查询和历史记录接口的问题。

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
