# 测试说明

- 功能测试按领域组织：`test_chatbot.py`、`test_chat_model.py`、`test_groups.py` 相关、`test_douyin*.py`、`test_group_tools.py`、`test_reminders.py`、`test_manage.py`、`test_wechat_sync.py` 等。
- 带 `*_fixes.py` / `*_review.py` 的文件是历史审查批次留下的跨模块回归，新增行为优先补到对应功能文件；跨领域不变量可保留在回归文件中。
- `tests/browser/` 需要 Playwright CLI 和 Chrome，按 `docs/GUIDE.md` 的“浏览器回归”手动运行。
- 全量后端回归：`.venv/Scripts/python.exe -m pytest -q`；静态检查：`ruff check src tests`、`compileall`、`pip check`。CI 配置见 `.github/workflows/ci.yml`。
