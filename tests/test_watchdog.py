"""看门狗辅助进程的启动方式 — console 工具必须 CREATE_NO_WINDOW。

2026-09-23 用户报"每十秒屏幕上闪一个 ghost window（像控制台的黑窗口）"，
实测抓到现行: win2blur 反复退出 → 看门狗每轮 (5s) 判"已退出" → _toast +
_restart() 跑 schtasks.exe / _kill_pid() 跑 taskkill.exe，两个都是 console
程序，而 subprocess 默认给它们分配**可见控制台窗口** → 用户屏幕上弹黑框
（实测 rect=52,52 993x519 vis=1 class=ConsoleWindowClass）。

同仓库的 edgedock_host.kill_standalone 与 edge-dock 的 mesh provider 早就
带了 CREATE_NO_WINDOW，看门狗这两处漏了。
"""
import sys
import types
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import watchdog


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """截获 subprocess.run，返回 [(cmd, kwargs)]；ROOT 指向 tmp 免写真 logs。"""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    monkeypatch.setattr(watchdog, "ROOT", str(tmp_path))
    (tmp_path / "logs").mkdir()
    return calls


def test_restart_runs_schtasks_windowless(captured):
    """_restart 拉起的 schtasks.exe 不许分配可见控制台窗口。"""
    assert watchdog._restart("win2blur", "w2b") is True
    assert captured, "_restart 应真的调用 schtasks"
    cmd, kw = captured[-1]
    assert cmd[0] == "schtasks"
    flags = kw.get("creationflags")
    assert flags is not None and flags & watchdog.subprocess.CREATE_NO_WINDOW, \
        f"schtasks 未带 CREATE_NO_WINDOW → 会闪黑框: {kw!r}"


def test_kill_pid_runs_taskkill_windowless(captured):
    """_kill_pid 拉起的 taskkill.exe 同样不许分配可见控制台窗口。"""
    watchdog._kill_pid(12345)
    assert captured, "_kill_pid 应真的调用 taskkill"
    cmd, kw = captured[-1]
    assert cmd[:2] == ["taskkill", "/f"]
    flags = kw.get("creationflags")
    assert flags is not None and flags & watchdog.subprocess.CREATE_NO_WINDOW, \
        f"taskkill 未带 CREATE_NO_WINDOW → 会闪黑框: {kw!r}"


def test_restart_debounce_still_works(captured, tmp_path):
    """加了标志不许碰坏防抖: 30s 内第二次必须跳过且不再拉起进程。"""
    assert watchdog._restart("win2blur", "w2b") is True
    n = len(captured)
    assert watchdog._restart("win2blur", "w2b") is False
    assert len(captured) == n, "防抖期内不该再跑 schtasks"


def test_tray_menu_probe_is_windowless(monkeypatch):
    """托盘菜单每次构建都探一次任务是否存在 → schtasks /query 不许闪黑框。"""
    import tray

    calls = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tray.subprocess, "run", fake_run)
    assert tray._task_exists() is True
    assert calls, "_task_exists 应真的调用 schtasks"
    cmd, kw = calls[-1]
    assert cmd[0] == "schtasks"
    flags = kw.get("creationflags")
    assert flags is not None and flags & tray.subprocess.CREATE_NO_WINDOW, \
        f"托盘菜单探测未带 CREATE_NO_WINDOW → 每次右键闪黑框: {kw!r}"
