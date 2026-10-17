# wxhook-api

## 微信风格的群聊管理系统

在 PyCharm 运行根目录 `main.py`，完成微信登录后，打开 **`http://127.0.0.1:8000/`**（或 `/manage`）。
首页现为群聊管理工作台，原来的消息收发工具移到 `/messages`，分享解析资源页面仍在 `/douyin`。
不需要安装前端工具或额外的数据库服务。

- 微信登录后自动通过只读数据库查询同步全部可读取群聊，不受联系人搜索 20 条上限影响；左侧支持搜索、运行状态筛选、置顶和手动添加。
- 点击左侧任意群聊，中间直接打开消息窗口，按时间从上到下显示消息并定位到最新一条；自己的消息显示为右侧绿色气泡。
- 管理页消息窗口用于查看群消息，消息区占满中间空间。
- 左侧绿色群聊表示机器人已启用，灰色表示未启用，浅红表示已拉黑，并显示对应文字状态。判断依据为群功能总开关、分享解析或 @问答开关及黑名单的实际生效状态。
- 已启用机器人的群聊优先显示；同一状态内保留置顶、最近活跃的排序规则。可筛选已启用、未启用或已拉黑的群。
- 搜索与历史翻页位于消息窗口上方；阅读旧消息时收到新消息不会强制跳到底部，可点“回到最新消息”。功能配置、成员与黑名单管理入口仍保留。
- 自动同步可读取的完整群成员关系和微信名称；不兼容的成员表会标记为“部分发现”，不会假装完整。历史成员和手动添加记录会保留。
- 每个群可分别开启“@机器人问答”“分享解析”和“自动保存视频与图片”；群功能总开关可暂停该群全部自动功能并保留设置。
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
历史导入只归档，不进入自动功能处理队列，因此不会重新解析旧分享链接或重复下载旧媒体。
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
| GET / POST | `/api/manage/groups/{群ID}/summary` | 查看今日总结；POST 强制刷新并调用当前启用的 AI 模型 |
| POST | `/api/manage/groups` | 添加本地管理群 |
| PATCH | `/api/manage/groups/{群ID}` | 名称、备注、置顶、功能开关、群黑名单 |
| GET / POST | `/api/manage/groups/{群ID}/members` | 查询或手动添加成员 |
| PATCH | `/api/manage/groups/{群ID}/members/{成员wxid}` | 成员备注与黑名单 |
| GET | `/api/manage/audit?group={群ID}` | 查询最近 100 条操作记录 |
| GET | `/api/manage/contacts?keyword={关键词}` | 从微信联系人中搜索群聊（最多 20 条联系人中的群） |

右侧资料栏下方展示 **群聊词云**：根据当前群最近 1000 条已保存的成员文本消息（排除当前机器人账号发送的消息，包括历史记录），在本机分词统计高频词，
过滤链接、微信 ID、@称呼和常见语气词；字号越大表示出现越多。点击词语可搜索相关聊天记录，切换群聊或收到新消息后自动更新。
词云下方是 **今日群聊总结**，按北京时间每天 18:00 自动生成并保存；页面点击“刷新总结”可立即重新生成。
在群里 @机器人发送“总结”“今日总结”或“今天聊天总结”，机器人会返回当天已保存的总结；当天没有记录时会提示，没有配置模型时会返回配置提示。
成员管理仍在上方 **「群成员」** 标签中。词云优先使用 jieba 中文分词；未安装时自动使用内置的双字词分词，
不需要下载额外依赖，后台和词云都能正常启动。

## 群内 @机器人问答：一条提问最多一条回复

在已启用机器人的群里，只有 @当前机器人账号的文字消息才进入问答；普通聊天、@其他人、仅 @所有人、
私聊和机器人自己发送的消息不触发。原来开启分享解析的群默认继承开启 @问答，
可在群“功能配置”中单独关闭“@机器人问答”，不影响分享解析；新发现且未启用功能的群不会自动答话。

打开网页管理端左侧 **「AI 模型」**（或群聊「功能配置 → 配置 AI 模型」），可查看已保存的模型列表，点击 **「添加模型」** 或 **「查看 / 修改」**，填写：

- **配置名称**：用于区分服务商、账号或不同模型，可选填。
- **Base URL**：服务商提供的 OpenAI 兼容接口地址，例如 `https://api.siliconflow.cn/v1`。
- **模型名称**：服务商支持的完整模型 ID。
- **深度思考**：硅基流动支持切换思考模式的模型可选择“关闭（优先快速回复）”，减少等待；其他服务或不支持该选项的模型请选择“跟随模型默认”。
- **API Key**：服务商密钥；本地免密模型可留空。已有密钥时留空沿用，更换 Base URL 时需要重新填写。

点击 **「保存并测试」** 后，后台自动向模型发送一条简短连接测试消息，页面显示成功结果和模型回复。
测试不会发送到微信群。可保存多个模型，但同时只有一个启用项。第一个模型测试通过后自动启用，新增备用模型不会替换当前模型；
点击列表中的 **「启用」** 会再次测试，成功后切换后续群内 @问答，无须重启。修改当前启用模型测试通过后立即生效，
修改备用模型不影响当前问答。正在启用的模型需先切换到其他模型才能删除。连接失败、密钥无效、模型不可用、
额度不足或返回无效文本时显示原因，原有配置保持生效。接口须支持非流式 `POST /chat/completions`。
深度思考设置同时用于保存测试与实际群内回复；关闭时发送 `enable_thinking: false`，“跟随模型默认”不发送此参数。
参数说明见[硅基流动官方文档](https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions)。

模型配置保存在运行服务电脑的 `data/chat-model.local.json`，该目录被 Git 忽略；密钥不会返回给页面。
可通过 `WXHOOK_CHAT_MODEL_CONFIG` 指定其他本地配置路径。重启后自动恢复全部配置及启用项；旧版单模型配置会自动载入为启用项，
下次保存时迁移为模型列表。每个模型单独保存密钥，编辑时留空仅沿用该模型的密钥，更换地址须重新填写。请保护本机配置文件。
只发送当前被 @ 的问题，不读取整群历史。尚未配置模型时，只回复一条“问答接口尚未配置，请联系管理员。”。

使用消息扩展字段中的 `atlist / at_list / atuserlist / at_user_list / atUserList` 或 `source / msgsource / msg_source`
XML 的 `atuserlist` 匹配真实 wxid；存在明确的 @ID 时优先使用 ID，不会把 @其他人误认成机器人。
Hook 未提供 @ID 时，以当前微信昵称、本机配置的群内显示名或已同步的机器人群成员名称匹配带空格边界的 `@昵称` 文本。
微信的提及分隔空格 U+2005 同样支持。部分 Hook 的 `GetSelfProfile` 会返回有效 wxid 但昵称为空（`selfInfoMatched=false`），
此时无法只靠账号昵称识别 @。在群「功能配置 → 配置机器人群内显示名」填写 @时实际显示的名字，不含 @，每行一个。
保存后立即生效，无须重启；请使用另一个微信账号发送新的 @提问。页面同时显示识别到的称呼、
最近实时消息的处理状态及未触发原因。历史同步显示的消息不会触发问答。

显示名保存在被 Git 忽略的 `chatbot.local.yml`，也可以手动配置：

```yaml
enabled: true
bot-wxid: '' # 留空自动读取当前 GetSelfProfile
mention-names:
  - '群内机器人显示名' # 不含 @；可列出多个群使用的不同名字
max-reply-chars: 2000
```

手动修改文件后重启 `main.py`；网页保存显示名立即生效。`WXHOOK_CHATBOT_CONFIG` 可指定配置文件，`WXHOOK_CHATBOT_ENABLED=false` 可临时关闭全部问答。
不需要额外启动服务。可让群成员使用微信的 @ 功能发送 `@机器人 你好` 验证回复。

每个触发事件只生成一条最终回复，不发送“正在思考”等过程提示；超长回答截短为一条消息，不拆分发送。
接口失败也仅发送一条失败提示；发送失败或超时不会再补发第二条，以免已经送达的消息重复出现。
相同群、相同非零消息 ID 在队列和 `data/management.sqlite3` 的 `chat_reply_claims` 中去重，重启不重发已认领的消息。
没有有效消息 ID 的回调作为独立事件处理，不能可靠地区分重复推送与用户再次发送的相同内容。
暂停群功能、关闭问答或拉黑群/成员后会停止后续问答；答案生成后和实际发送前会再次检查规则。

一条 @提问只走问答流程，里面即使包含分享链接，也不会同时触发视频解析；未 @机器人的普通分享仍走原解析流程。
历史同步只归档，不会补答旧问题。后台队列最多 100 条，模型调用最多等待 60 秒。

## 指定群聊的抖音、小红书与 B站分享解析

默认使用 [parse-video-py](https://github.com/wujunwei928/parse-video-py)，直接调用 Python 库，与现有后台队列一起运行。
不需要 API Key、额外端口、Docker 或另开解析服务，仍只启动根目录 `main.py`。
首次更新此版，在测试电脑的项目终端执行：

```powershell
git pull origin main
.\.venv\Scripts\python.exe -m pip install -e .
```

如果旧版安装日志显示 `github.com/.../archive/...zip` 返回 HTTP 403，先拉取最新代码再重试。
当前使用同一提交的 GitHub 官方 `codeload.github.com` 下载地址并校验 SHA-256，仍固定同一解析库源码。
安装完成可用 `.\.venv\Scripts\python.exe -c "import parse_video_py; print('parse-video-py OK')"` 检查，再重新运行 `main.py`。

资源页面 `/douyin` 选择“parse-video-py（抖音 / 小红书 / B站）”。旧版配置会自动切换到该解析器，保留群名单、
群/成员黑名单、功能开关和下载目录；在新版页面明确选择并保存其他来源后，会尊重该选择。
数据库字段和接口路径保留原名 `douyin`，兼容已有配置和文件。

当前接入抖音视频/图集、小红书视频/图集、B站视频以及相应短链接、分享文案和卡片链接。
保留链接原查询参数，小红书签名参数不会被删除。图集只保存静态图片，封面、配乐和 Live Photo 附属视频不作为独立图片回传。
B站使用上游库返回的第一分 P、第一条可用视频地址，清晰度由平台实际返回决定；当前不合并多分 P、分段或 DASH 音视频。
解析器使用平台可访问的媒体地址，不额外拼接广告或片尾，也不提供像素擦水印或裁剪视频服务；作者嵌入画面的水印会保留。
平台登录要求、作品权限、链接过期和页面变化均可能导致失败，不能保证每条链接可解析。

小红书跳转登录页时，在**测试电脑**把 `parse-video.example.yml` 复制为 `parse-video.local.yml`。
使用本人已登录小红书网页的开发者工具，在 Network 中选择笔记页面请求，将 Request Headers 的 `Cookie` 值填入
`xiaohongshu-cookie`，保持单行；也可通过环境变量 `PARSE_VIDEO_XHS_COOKIE` 提供（优先于文件）。
这是可选配置，无需把 Cookie 发到聊天中。配置文件已忽略 Git，不会经配置 API 返回 Cookie，仅向 `www.xiaohongshu.com` 发出，
不会发送给短链、媒体 CDN 或微信 Hook。每次解析时读取，更新后可直接重发分享链接；登录过期时需更新本人 Cookie。
Cookie 不能使无权访问的作品变为可访问，必要时从 App 重新复制新分享链接。

依赖固定到上游提交 `5fcf87256edb5ffcdebf0e4aac2a5a41745da76e`，第三方来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
适配层为 SDK 请求添加超时、每次重定向的域名检查和 3 MB 页面读取上限；媒体探测不把完整视频载入内存。
网络默认直连，可用上游 `PARSE_VIDEO_PROXY` 为解析请求设置代理；媒体下载仍使用本机直连网络。

### 开启解析后没有提示或结果

更新代码后，先停止原来的 PyCharm 运行/调试任务或在原服务终端按 `Ctrl+C`，再运行根目录 `main.py`，然后刷新页面。
微信无需退出。新版启动器会检查正在运行的后台与本地 Python 源码是否一致，遇到旧进程会提示重启。
资源页会识别更新前的后台并提示重启，避免页面与后台版本不一致；校验失败时会提示群 ID、解析来源或接口地址等具体字段。

在群功能页确认当前来源是 `parse-video-py`，提示和自动回传已开启，同时开启该群的自动保存。
如果保留了旧的仅抖音来源，B站和小红书分享也会生成失败记录并提示更换来源，不再静默忽略。
“群消息”中的“微信历史”表示只归档，不能证明实时回调已经接通；只有实时回调的新分享会触发自动解析。
左下角“连接与消息接收”显示本次启动的回调数量与最近接收时间，可在此启用消息接收后重新发送一条新分享验证。
如果运行控制台出现 `POST /receiveChatBotMsg ... 422`，表示请求已到达，但回调字段未通过校验。
群回调允许省略 `roomid` 或提供 `null`，此时使用以 `@chatroom` 结尾的 `wxid` 识别群；不会把普通私聊当作群消息。
其余必填字段仍校验。控制台只记录拒收字段名与错误类型，不输出正文或字段值。
左下角“检查连接”以及 `GET /api/callback/status` 可查看本次启动的接收/拒收计数、最近拒收字段与类型；诊断只保存在内存，不保存聊天内容。

### 保留的指定解析接口（仅抖音，可选）

需要使用原 `jk.y-l.cc` GET 接口时，在测试电脑打开 `/douyin`，选择“指定解析接口”，
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

启用群聊解析后，默认会在**分享来源群**只发送文字“开始解析”，不附带任务编号或额外说明；解析或下载失败时发送失败原因。
下载完成后把媒体回传同一个群，成功结束不再发送文字提示。图片按图集顺序发送，全部 Hook 调用返回明确成功后资源页面才显示“已回传”。
开始提示失败不会阻止继续解析，所有提示的发送状态可在资源页面查看。

图片使用 `/SendImgMsg`；当前 Hook 文档没有独立的视频发送接口，因此视频通过 `/send_file_msg` 作为 **MP4 文件**发送，
不是原生视频消息气泡。WebP、AVIF 等静态图片生成 PNG 副本用于图片消息，原下载文件保留；动态图以原文件回传以保留动画。
图片转换使用 Pillow，更新这版后在测试电脑的项目虚拟环境执行一次 `python -m pip install -e .`，以后仍只运行 `main.py`。

在 `/douyin` 的默认设置中，可分别关闭“在来源群提示开始和失败”和“将视频或图片发回来源群”，保存后立即生效。
这些提示/回传设置应用于所有实际开启了解析的群；群的总开关、解析开关、自动保存开关和群/成员黑名单仍独立生效。
回传必须同时开启该群的自动保存。关闭自动保存或自动回传时，原因记录在资源页面，不另发结束提示，也不会把没有发送的媒体标为成功。

回传前、每个文件发送前以及图片转换后会检查群与成员限制。发送中遇到失败或超时即停止后续文件，
页面区分“解析失败”“部分回传”“回传失败”和“回传待确认”，并保留已经下载的文件。
发送超时可能已到达微信群，不自动重试；请先查看群聊结果。同一非零消息 ID 在当前运行和最近已持久化的任务记录中去重，
服务重启不会自动重发历史任务。自动提示不附带源分享链接，避免自己的回调再次触发解析。
可设置 `WXHOOK_DOUYIN_FEEDBACK_ENABLED=false` 并重启，临时关闭所有群内自动发送，保留解析下载功能。

运行 `main.py` 后，在测试电脑打开 **`http://127.0.0.1:8000/douyin`**，也可从群管理侧栏点击“资源”。
此功能与 Python 服务一起运行，不需要启动额外程序。

1. 先让目标群收到一条消息；配置页面的“最近收到消息的群”会显示该群 ID，点击可添加。
   也可以手动填写 `123456789@chatroom` 形式的真实群 ID，每行一个，不能使用群昵称。
2. 保留默认“parse-video-py”来源，勾选“启用群聊分享解析”，保留“解析成功后自动保存视频和图片”，点击“保存群配置”。
   默认名单保存后立即生效；空名单或未启用时不处理默认群，管理系统中已独立开启的群以单独配置为准。
3. 让群内成员发送抖音、小红书或 B站分享文案，也支持卡片 XML 中包含的链接。
4. 页面会依次显示“排队中 → 解析中 → 保存中 → 回传中 → 已回传”，展开记录可查看、播放、下载文件和查看每个文件发送状态。
   如果关闭自动保存，则只展示解析出的媒体链接。

默认文件位置为项目下 `downloads/douyin/<任务ID>/`：视频保存为 `001.mp4`，图集按顺序保存为
`001.webp`、`002.webp` 等（以实际文件格式为准）。`result.json` 记录作品标题、作者、来源群、发送人和文件列表。
页面显示最近 100 条记录，保留期限内的文件与结果记录重启后可继续查看；未完成的任务需要重新发送链接。
同一条回调重复推送不会重复创建任务，同一个链接在另一条新群消息中发送则会重新解析。

### 本地媒体自动清理

默认每天**北京时间凌晨 03:00**扫描整个媒体目录；服务启动时也会补清理已经过期的文件。
不需要额外启动定时程序，服务关闭期间不会执行删除，下次启动会补做。

| 类型 | 保留规则 |
|---|---|
| 全部媒体均已获 Hook 明确发送确认 | 任务结束后至少保留 24 小时 |
| 解析失败、未回传、部分回传、发送结果待确认、回传关闭 | 至少保留 3 天 |
| 解析记录 `result.json` | 任务结束后保留 30 天 |

达到期限后在下一次清理时删除，不会在发出文件后立即删除。
旧版本没有结束时间的任务，会按记录和媒体的最近写入时间保守计算；最近重新写入的媒体也会延后清理。
原始视频、图片、`send-*.png` 转换副本及遗留下载临时文件一起处理。
正在排队、解析、下载、回传的任务，以及正在通过资源页面播放/下载的任务，会跳过当前轮清理。
文件被占用或权限不足时记录失败原因，留待下一轮重试；文件清理后资源页显示“文件已清理”，不再生成失效的预览与下载链接。

清理扫描全部任务目录，不受页面最近 100 条的限制。只处理媒体目录下合法任务 ID 对应的本程序文件，
不递归删除目录，不跟随链接或 Windows 重解析点，不删除其他名称的文件。群配置、黑名单和聊天记录资料库不在清理范围内。
解析记录过期且媒体处理完毕后会删除记录；没有其他文件的任务文件夹随后移除。

可在 `douyin.local.yml`（没有该文件时使用 `douyin.yml`）设置后重启服务：

```yaml
cleanup-enabled: true
cleanup-hour: 3  # 北京时间，0–23
```

环境变量 `WXHOOK_MEDIA_CLEANUP_ENABLED=false` 可覆盖配置并关闭自动清理。
通过资源页保存群设置会保留这两项清理配置；页面展示清理规则、下次时间与上次释放的空间。

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
parser-provider: parse_video
parser-config-version: 2
parser-api-url: '' # 仅选择旧 API 来源时需要
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
原收发工具只提供手动文本发送，非文本回调显示原始内容；群内自动分享解析与 @问答使用各自的独立功能开关。

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

合法 JSON 但必填字段缺失或类型错误时返回 HTTP 422。`roomid` 可省略或为 `null`，统一转换为空字符串。

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
