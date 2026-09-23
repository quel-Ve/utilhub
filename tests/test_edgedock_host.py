"""EdgeDock 集成测试 — HubDock 手动隐藏旗 / EdgeDockHost 生命周期 / 托盘 QMenu 构建。

offscreen 平台跑（DockStrip.show() 不闪真窗口）; 不触网（initial_refresh=False,
fetcher 全部换桩）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PyQt6.QtWidgets import QApplication

from edgedock_host import (EdgeDockHost, HubDock, acquire_mutex,
                           release_mutex, standalone_running)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeFetcher:
    """替身: refresh_async 不触网, done 信号不发射。"""
    class _Sig:
        def connect(self, *a):
            pass

        def disconnect(self, *a):
            pass

    done = _Sig()

    def refresh_async(self):
        pass


class _FakeDialog:
    def close(self):
        pass

    def deleteLater(self):
        pass


class _Hub:
    """最小 hub 桩: tray.notify 可收, app 用模块级 QApplication。"""
    tray = None
    app = None


def _make_host(qapp, take_over=False):
    _Hub.app = qapp
    host = EdgeDockHost(_Hub(), take_over=take_over, initial_refresh=False)
    # 换桩: 不触网、不建真详情窗
    host.fetcher = _FakeFetcher()
    host.dialog = _FakeDialog()
    return host


def test_hubdock_user_hidden_gates_fullscreen(qapp):
    dock = HubDock()
    try:
        dock._fullscreen_covering = lambda: False  # 全屏检测桩掉, 测试确定性
        dock.pin()
        assert dock.isVisible()
        dock.set_user_hidden(True)
        assert dock.user_hidden and not dock.isVisible()
        dock._check_fullscreen()  # 全屏避让轮询不许把手动隐藏的 dock 拉回来
        assert not dock.isVisible()
        dock.set_user_hidden(False)
        assert dock.isVisible()
    finally:
        dock.deleteLater()


def test_host_lifecycle_stop_and_restart(qapp):
    host = _make_host(qapp)
    try:
        assert host.running() and host.dock is not None
        assert not host.is_hidden()
        host.set_hidden(True)
        assert host.is_hidden() and not host.dock.isVisible()
        host.stop(quiet=True)
        assert not host.running() and host.dock is None
        assert host.snaps == []
        host.start(initial_refresh=False)  # 恢复: 重建 dock 条
        assert host.running() and not host.is_hidden()
    finally:
        host.stop(quiet=True)


def test_take_over_kills_standalone_then_holds_mutex(qapp, monkeypatch):
    """接管: 独立 exe 在跑 → taskkill; 之后必须持互斥体挡住后来的独立 exe。

    用桩保证确定性（真 taskkill /im 会打到自己人）。这正是用户报的"两个
    dock 条叠在一起"的修复点。
    """
    import edgedock_host

    calls = {"killed": 0}
    monkeypatch.setattr(edgedock_host, "standalone_running", lambda: True)
    monkeypatch.setattr(edgedock_host, "kill_standalone",
                        lambda: calls.__setitem__("killed", calls["killed"] + 1) or True)

    host = _make_host(qapp, take_over=True)
    try:
        assert calls["killed"] == 1, "启动时应结束独立实例"
        assert host._mutex, "接管后必须持互斥体"
        host.start(initial_refresh=False)  # 幂等: 已在跑, 不重复接管
        assert calls["killed"] == 1
    finally:
        host.stop(quiet=True)


def test_take_over_warns_when_kill_fails(qapp, monkeypatch, caplog):
    """taskkill 失败不许静默: 否则用户只会看到两个 dock 条不知为何。"""
    import logging

    import edgedock_host

    monkeypatch.setattr(edgedock_host, "standalone_running", lambda: True)
    monkeypatch.setattr(edgedock_host, "kill_standalone", lambda: False)
    with caplog.at_level(logging.WARNING, logger="hub"):
        host = _make_host(qapp, take_over=True)
        try:
            assert any("结束失败" in r.message for r in caplog.records)
        finally:
            host.stop(quiet=True)


def test_host_status_line(qapp):
    host = _make_host(qapp)
    try:
        host._updated_at = "10:32"

        class _S:
            def __init__(self, error=None):
                self.error = error

        host.snaps = [_S(), _S()]
        assert "10:32 更新" in host.status_line()
        host.snaps = [_S("boom"), _S()]
        assert "1 个出错" in host.status_line()
    finally:
        host.stop(quiet=True)


def test_mutex_roundtrip_blocks_standalone():
    if standalone_running():
        pytest.skip("独立 edgedock.exe 正在运行")
    h = acquire_mutex()
    try:
        assert h
        assert standalone_running()  # 自持时同名互斥体存在 (OpenMutexW 查存在性)
    finally:
        release_mutex(h)
    assert not standalone_running()


def test_mutex_name_is_single_source_of_truth():
    """互斥体名必须取自 edgedock.tray, 不许在本模块硬编码。

    2026-09-19 tray.py 把名字改成 -v2（旧名被提权僵尸占死）而本模块留旧名，
    两侧互相看不见 → 接管双向失效 → 独立 dock 与 hub dock 并存 4 天
    （两个 vis=1 窗口同 rect 2438,0 122x1440）。名字再变，本测试先红。
    """
    from edgedock import tray as ed_tray

    import edgedock_host

    assert edgedock_host.MUTEX_NAME == ed_tray.MUTEX_NAME


def test_host_restart_reacquires_mutex(qapp):
    """托盘"退出 EdgeDock → 启动"后必须重新持有互斥体。

    老写法只在 __init__ 拿一次、stop() 放掉，重启走的 start() 不再获取 →
    托管 dock 裸奔，独立 exe 可随时并存。
    """
    host = _make_host(qapp)
    try:
        assert host._mutex, "初始应持有互斥体"
        host.stop(quiet=True)
        assert not host._mutex, "stop 应放掉互斥体"
        host.start(initial_refresh=False)
        assert host._mutex, "重启后必须重新持有互斥体"
        assert standalone_running(), "持有期间同名互斥体应可见"
    finally:
        host.stop(quiet=True)


def _tray_with_stub(qapp, edgedock=None):
    from tray import Tray

    hub = _Hub()
    hub.app = qapp
    hub.edgedock = edgedock  # None → 未启用分支
    hub.sort_now = lambda: None
    hub.snapshot = lambda slot: None
    hub.restore = lambda slot: None
    hub.clear = lambda slot: None
    hub.eq_current = lambda: "disabled"
    hub.eq_presets = lambda: []
    hub.eq_preset_summary = lambda name: ""
    hub.eq_auto_enabled = lambda: False
    hub.eq_set_auto = lambda on: None
    hub.eq_cycle = lambda: None
    hub.eq_set_preset = lambda name: None
    hub.open_eq_editor = lambda: None
    hub.focus_status_lines = lambda: [("○ 非工作时段", 0)]
    hub.open_log_dir = lambda: None
    hub.quit = lambda: None
    return Tray(hub, enabled=False), hub


def test_tray_menu_build(qapp):
    from tray import Tray

    tray, hub = _tray_with_stub(qapp, edgedock=None)
    menu = tray._build_menu()
    try:
        texts = [a.text() for a in menu.actions()]
        assert any("任务栏排序" in t for t in texts)
        assert any("EdgeDock" in t for t in texts)
        # edgedock=None → 子菜单只有灰显"未启用"一行
        sub = next(a.menu() for a in menu.actions() if a.menu() and "EdgeDock" in a.text())
        assert sub.actions()[0].text().startswith("未启用")
        assert not sub.actions()[0].isEnabled()

        # 挂上运行中的 edgedock → 动作齐全 (隐藏/刷新/详情/游戏模式/边缘/退出)
        class _ED:
            running = lambda s: True
            is_hidden = lambda s: False
            game_mode = lambda s: True
            form = lambda s: "hand"
            set_form = lambda s, f: None
            snaps = []
            status_line = lambda s: "10:32 更新"
            set_hidden = lambda s, on: None
            refresh = lambda s: None
            show_detail = lambda s: None
            set_game_mode = lambda s, on: None
            stop = lambda s: None
            start = lambda s: None

        hub.edgedock = _ED()
        menu2 = tray._build_menu()
        try:
            sub2 = next(a.menu() for a in menu2.actions()
                        if a.menu() and "EdgeDock" in a.text())
            labels = [a.text() for a in sub2.actions()]
            assert "隐藏 dock" in labels
            assert "立即刷新" in labels
            assert "详情窗口" in labels
            assert "游戏模式（隐藏 dock）" in labels
            assert "退出 EdgeDock" in labels
            gm = next(a for a in sub2.actions() if a.text() == "游戏模式（隐藏 dock）")
            assert gm.isChecked()  # game_mode 桩 True → 勾选透传
            fm = next(a for a in sub2.actions()
                      if "边缘模式" in a.text())
            assert not fm.isChecked()  # form 桩 hand → 未勾选
        finally:
            menu2.deleteLater()
    finally:
        menu.deleteLater()


def test_tcp_up_probe():
    import socket

    from edgedock_host import EdgeDockHost
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert EdgeDockHost._tcp_up("127.0.0.1", port)       # 监听中 → up
        assert not EdgeDockHost._tcp_up("127.0.0.1", 1, timeout=0.2)  # 未监听
    finally:
        srv.close()


def test_merge_synthetic_adds_tool_cells(qapp):
    from types import SimpleNamespace

    host = _make_host(qapp)
    try:
        net = [SimpleNamespace(provider_id="deepseek")]
        # Focus 未启用 → screentime 灰态, dockhide 恒在
        host.hub.focus = None
        merged = host._merge_synthetic(net)
        ids = [s.provider_id for s in merged]
        assert ids == ["deepseek", "screentime", "dockhide"]
        st = next(s for s in merged if s.provider_id == "screentime")
        assert not st.available and "Focus" in st.error

        # focus 记账桩: combined_today {域: 秒} → 总分钟 + top
        host.hub.focus = SimpleNamespace(tracker=SimpleNamespace(
            combined_today=lambda: {"github.com": 600, "bilibili.com": 1800}))
        merged = host._merge_synthetic(net)
        st = next(s for s in merged if s.provider_id == "screentime")
        assert st.used == 40.0                    # (600+1800)/60
        assert st.extra["top"] == ["bilibili.com", 30.0]
        # 合成格幂等: 二次合并不重复追加
        merged2 = host._merge_synthetic(merged)
        assert [s.provider_id for s in merged2].count("dockhide") == 1
    finally:
        host.stop(quiet=True)


def test_spawn_forces_utf8_child_stdio(monkeypatch, tmp_path):
    """_spawn 拉起的子进程 stdout 是文件, 必须显式指定 PYTHONIOENCODING=utf-8。

    2026-09-23: 仪表盘格单击不打开浏览器。链路 = _spawn 用 DETACHED_PROCESS +
    stdout=文件 → 子进程 Python 对非控制台流退回 locale 编码（本机 cp1252）→
    agent_dashboard.py 启动那句 print 含中文（"Ctrl+C 退出"）→ 在 bind 之前
    UnicodeEncodeError 秒退 → _open_when_up 轮询 12s 无果 → 无浏览器。
    （顺带：日志文件本身是 utf-8 打开的, 不指定就是两套编码混写。）
    """
    import subprocess

    import edgedock_host

    seen = {}

    class _FakePopen:
        def __init__(self, cmd, **kw):
            seen.update(kw)
            seen["cmd"] = cmd

    monkeypatch.setattr(edgedock_host.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(edgedock_host, "ROOT", str(tmp_path))
    (tmp_path / "logs").mkdir()

    EdgeDockHost._spawn([sys.executable, "x.py"], str(tmp_path), "child.log")

    env = seen.get("env") or {}
    assert env.get("PYTHONIOENCODING", "").lower().startswith("utf-8"), \
        f"子进程 stdio 未强制 UTF-8: {seen!r}"


def test_spawned_child_can_print_cjk(tmp_path, monkeypatch):
    """行为回归: 真拉起一个打印中文的子进程, 必须正常跑完。

    cp1252 下该子进程会在 print 处 UnicodeEncodeError 死掉（仪表盘就是这样
    静默死掉的）, 因此以"子进程跑到了 write 哨兵文件"为准——崩溃时日志里的
    traceback 会回显源码行, 只断言日志含中文是假绿。
    """
    import time as _time

    import edgedock_host

    monkeypatch.setattr(edgedock_host, "ROOT", str(tmp_path))
    (tmp_path / "logs").mkdir()
    script = tmp_path / "child.py"
    script.write_text(
        "print('子进程中文输出 ok')\n"
        "open('done.txt', 'w', encoding='utf-8').write('1')\n",
        encoding="utf-8")

    EdgeDockHost._spawn([sys.executable, str(script)], str(tmp_path), "child.log")

    done = tmp_path / "done.txt"
    for _ in range(60):  # 最多 6s
        if done.exists():
            break
        _time.sleep(0.1)
    log = (tmp_path / "logs" / "child.log").read_text(encoding="utf-8",
                                                      errors="replace")
    assert done.exists(), f"子进程没跑完（stdio 编码崩）: {log!r}"
    assert "Traceback" not in log
    assert "子进程中文输出 ok" in log


def test_tray_popup_keeps_menu_alive(qapp, monkeypatch):
    """popup() 非模态后方法立即返回 — 菜单必须由 Tray 自持引用, 否则本地
    引用出作用域即被 GC, 菜单刚弹即毁（用户报"右键不再弹出"）。"""
    from PyQt6.QtWidgets import QMenu
    from tray import Tray

    tray, _hub = _tray_with_stub(qapp, edgedock=None)
    monkeypatch.setattr(QMenu, "popup", lambda self, pos: None)
    tray._popup_menu()
    assert tray._menu_ref is not None
    tray._menu_dismissed()
    assert tray._menu_ref is None
