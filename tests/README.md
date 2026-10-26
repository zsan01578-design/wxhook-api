# 测试说明

- 功能测试按领域组织：`test_chatbot.py`、`test_chat_model.py`、`test_groups.py` 相关、`test_douyin*.py`、`test_group_tools.py`、`test_reminders.py`、`test_manage.py`、`test_wechat_sync.py` 等。
- 带 `*_fixes.py` / `*_review.py` 的文件是历史审查批次留下的跨模块回归，新增行为优先补到对应功能文件；跨领域不变量可保留在回归文件中。
- `tests/browser/` 需要 Playwright CLI 和 Chrome，按 `docs/GUIDE.md` 的“浏览器回归”手动运行。
- `test_group_insights.py` 覆盖梗百科、爬楼、漫画、来源校验及权限变化；`tests/browser/group_insights.js` 使用模拟模型响应检查桌面/手机表单、预览、来源、重试和弹窗竞态。
- 全量后端回归：`.venv/Scripts/python.exe -m pytest -q`；静态检查：`ruff check src tests`、`compileall`、`pip check`。CI 配置见 `.github/workflows/ci.yml`。
- 兼容性回归：`test_configuration_compatibility.py` 覆盖 UTF-8/BOM 配置、旧模型配置及 IPv4/IPv6 回调地址；`test_retention_and_search.py` 覆盖已建 FTS 索引的数据库禁用索引或缺少 FTS 模块后的写入、删除和重新启用重建。CI 配置 Windows / Python 3.11、3.12、3.13、3.14 矩阵，未执行的矩阵任务不算已验证。
- `tests/browser/http_compatibility.js` 在模拟服务上移除 `AbortSignal.timeout`，验证三个页面、布尔登录状态、后台回调地址，以及请求头/响应体超时和定时器清理。运行后关闭该独立浏览器会话，避免缺失接口模拟影响其他手工操作。
- 更新/重启脚本回归：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/restart_project.Tests.ps1`；错误诊断回归：同样运行 `tests/update_diagnostics.Tests.ps1`。后者仅用本地临时 Git 仓库模拟冲突，不连接 GitHub、不停止真实服务，也不读取真实 Cookie。
