from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from wxhook_api.services import desktop


def test_process_query_uses_exact_executable_name_and_ignores_bad_rows(monkeypatch):
    query = Mock(return_value=SimpleNamespace(stdout=b'"Weixin.exe","123","Console"\r\n"Other.exe","456"\r\n"Weixin.exe","invalid"\r\n'))
    monkeypatch.setattr(desktop.subprocess, "run", query)
    assert desktop.wechat_process_ids(Path("Weixin.exe")) == {123}


def test_minimized_wechat_is_restored_and_focused_without_spawning(monkeypatch):
    api = SimpleNamespace(
        IsIconic=Mock(return_value=True),
        ShowWindowAsync=Mock(),
        SetForegroundWindow=Mock(return_value=True),
        FlashWindowEx=Mock(),
    )
    monkeypatch.setattr(desktop, "wechat_process_ids", lambda path: {123})
    monkeypatch.setattr(desktop, "_user32", lambda: api)
    monkeypatch.setattr(desktop, "_find_window", lambda api, process_ids: 42)
    spawn = Mock()
    monkeypatch.setattr(desktop.subprocess, "Popen", spawn)
    assert desktop.show_wechat_window(Path("Weixin.exe")) == "focused"
    api.ShowWindowAsync.assert_called_once_with(42, 9)
    api.SetForegroundWindow.assert_called_once_with(42)
    api.FlashWindowEx.assert_not_called()
    spawn.assert_not_called()


def test_focus_denial_flashes_taskbar_and_does_not_claim_focus(monkeypatch):
    api = SimpleNamespace(
        IsIconic=Mock(return_value=False),
        ShowWindowAsync=Mock(),
        SetForegroundWindow=Mock(return_value=False),
        GetForegroundWindow=Mock(return_value=99),
        FlashWindowEx=Mock(),
    )
    monkeypatch.setattr(desktop, "wechat_process_ids", lambda path: {123})
    monkeypatch.setattr(desktop, "_user32", lambda: api)
    monkeypatch.setattr(desktop, "_find_window", lambda api, process_ids: 42)
    assert desktop.show_wechat_window(Path("Weixin.exe")) == "shown"
    api.ShowWindowAsync.assert_called_once_with(42, 5)
    api.FlashWindowEx.assert_called_once()


def test_window_selection_ignores_other_processes_and_tiny_helper_windows():
    windows = {
        10: (999, "微信", (0, 0, 1500, 900), True),
        20: (123, "Weixin helper", (0, 0, 1, 1), False),
        30: (123, "微信", (0, 0, 1000, 700), False),
        40: (123, "提示", (0, 0, 300, 200), True),
    }

    def get_pid(handle, pointer):
        pointer._obj.value = windows[handle][0]

    def get_title(handle, buffer, length):
        buffer.value = windows[handle][1]
        return len(buffer.value)

    def get_rect(handle, pointer):
        rect = pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = windows[handle][2]
        return True

    def enumerate_windows(callback, parameter):
        for handle in windows:
            callback(handle, parameter)
        return True

    api = SimpleNamespace(
        GetWindowThreadProcessId=get_pid,
        GetWindowTextW=get_title,
        GetWindowRect=get_rect,
        IsIconic=lambda handle: False,
        IsWindowVisible=lambda handle: windows[handle][3],
        EnumWindows=enumerate_windows,
    )
    assert desktop._find_window(api, {123}) == 30
