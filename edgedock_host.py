"""EdgeDock 宿主 — 28edge-dock 的右缘 dock 条 + balances 在 UtilityHub 进程内运行。

hub 只读复用 edgedock 包（同 EQ/VoiceInput 套路，sys.path 注入 28edge-dock 仓库根）:
  - HubDock = DockStrip 子类，加 user_hidden 旗: 托盘"隐藏 dock"后全屏避让轮询
    (2s) 不再把 dock 拉回来（否则全屏一退 dock 就复活）
  - Fetcher/DetailDialog 直接复用 edgedock.tray（只 import，不跑其 main）
  - 互斥体接管: 每次 start()（含托盘"退出→启动"的恢复）先 taskkill 独立
    edgedock.exe，再持 edgedock.tray.MUTEX_NAME 单实例互斥体——之后手动启动
    独立 exe 会自行静默退出（其 single_instance_acquired 撞
    ERROR_ALREADY_EXISTS）。hub 退出/托盘"退出 EdgeDock"时放掉互斥体，
    独立 exe 恢复可用。互斥体名从 edgedock.tray 取（单一事实源，别硬编码）。

托盘菜单 EdgeDock 子菜单: 隐藏/显示、退出/启动（恢复）、立即刷新、详情窗口、
游戏模式（checkable，透传 dock 持久化）。dock 自身右键菜单的"退出"也接这里。
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices

log = logging.getLogger("hub")

ROOT = os.path.dirname(os.path.abspath(__file__))
EDGEDOCK_ROOT = os.path.join(os.path.dirname(ROOT), "28edge-dock")
RU_ROOT = os.path.join(os.path.dirname(ROOT), "25ru-recite")
MESH_SRC = os.path.join(os.path.dirname(ROOT), "23agent-mesh", "src")
if EDGEDOCK_ROOT not in sys.path:
    sys.path.insert(0, EDGEDOCK_ROOT)

from edgedock.dock import DockStrip          # noqa: E402 — 依赖上面的 sys.path
from edgedock import paths as ed_paths       # noqa: E402
from edgedock.balances.models import BalanceSnapshot  # noqa: E402
# ⚠ 单一事实源：名字必须从 edgedock.tray 取，绝不在这里硬编码。
# 2026-09-19 tray.py 把名字改成 -v2（旧名被提权僵尸占死），本文件留旧名 →
# 两侧互不可见 → 接管双向失效 → 两个 dock 条同 rect 并存 4 天。
# tests/test_edgedock_host.py::test_mutex_name_is_single_source_of_truth 锁死。
from edgedock.tray import MUTEX_NAME         # noqa: E402
REFRESH_MS = 10 * 60 * 1000                # 10 分钟自动刷新（同独立托盘）
SYNTHETIC_MS = 60 * 1000                   # 屏幕时间等本机格刷新周期
SCREEN_GOAL_MIN_DEFAULT = 360.0            # 日目标（config.toml [dock] screen_time_goal_min 覆盖）
RU_PORT_DEFAULT = 8625                     # 25ru-recite config.json port
MESH_PORT_DEFAULT = 8765                   # 23agent-mesh agent_dashboard --port
SYNCHRONIZE = 0x00100000


def standalone_running() -> bool:
    """独立 edgedock.exe 是否持有单实例互斥体（OpenMutexW 只查存在性）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = ctypes.c_void_p
    h = k32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if h:
        k32.CloseHandle(h)
        return True
    return False


def acquire_mutex():
    """持有 edgedock 单实例互斥体（不拥有也行——同名互斥体存在即挡住独立 exe）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    return k32.CreateMutexW(None, False, MUTEX_NAME)


def release_mutex(handle) -> None:
    if handle:
        ctypes.WinDLL("kernel32").CloseHandle(handle)


def kill_standalone() -> bool:
    """结束独立 edgedock.exe（PyInstaller onefile 父子进程同名，/im 全中）。"""
    r = subprocess.run(["taskkill", "/f", "/im", "edgedock.exe"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode == 0


class HubDock(DockStrip):
    """DockStrip + user_hidden 旗: 手动隐藏优先于全屏避让轮询。"""

    # 类属性兜底: DockStrip.__init__ 会先多态调 _check_fullscreen(), 实例属性还没建
    user_hidden = False

    def __init__(self, on_detail=None, on_refresh=None, on_quit=None,
                 cell_actions=None):
        super().__init__(on_detail=on_detail, on_refresh=on_refresh,
                         on_quit=on_quit, cell_actions=cell_actions)
        self.user_hidden = False

    def _check_fullscreen(self):
        if self.user_hidden:
            return  # 手动隐藏期间，全屏避让（2s 轮询）不许把 dock 拉回来
        super()._check_fullscreen()

    def set_user_hidden(self, on: bool):
        self.user_hidden = bool(on)
        if on:
            self._expanded = False
            self._t = 0.0
            self._hand_hover = False
            self.hide()
        elif not self._fs_hidden:
            # 当前正全屏时只揭旗不 show，等轮询 tick 自己恢复（_check_fullscreen）
            self.show()
            self._set_topmost()
            self.update()


class EdgeDockHost:
    """hub 进程内的 edgedock：dock 条 + 10min 刷新 + 详情窗 + 托盘状态查询。

    生命周期: start() 建 Fetcher/DetailDialog/HubDock + 定时刷新；stop() 全部
    拆掉并放互斥体（托盘"退出 EdgeDock"/dock 右键"退出"走这里）；隐藏/显示只动
    HubDock.user_hidden，balances 后台照常刷，恢复即时。
    """

    def __init__(self, hub, take_over=True, initial_refresh=True):
        self.hub = hub
        self.take_over = take_over
        self._mutex = None
        self.dock: HubDock | None = None
        self.fetcher = None
        self.dialog = None
        self._timer: QTimer | None = None
        self.snaps: list = []
        self._updated_at = ""
        self.start(initial_refresh=initial_refresh)

    # ---- 生命周期 ----

    def _take_over(self):
        """结束独立 edgedock.exe 并持有单实例互斥体（幂等，已在 start 里跑）。

        必须放 start() 而不是 __init__: 托盘"退出 EdgeDock → 启动"走的是
        stop()→start(), 老写法只在 __init__ 拿互斥体、stop() 放掉、start() 不补
        → 重启后托管 dock 裸奔, 独立 exe 可随时并存（实测旧名互斥体在 hub
        运行中不存在）。stop() 放、start() 拿, 两侧成对。
        """
        if self._mutex:
            return
        if self.take_over and standalone_running():
            if kill_standalone():
                log.info("EdgeDock 接管: 独立 edgedock.exe 已结束")
                self._notify("EdgeDock 已并入 UtilityHub（独立实例已结束）")
            else:
                log.warning("EdgeDock 接管: 独立实例结束失败 (可能与 dock 条重复)")
        self._mutex = acquire_mutex()

    def start(self, initial_refresh=True):
        if self.dock is not None:
            return
        self._take_over()
        from edgedock.tray import DetailDialog, Fetcher
        self.fetcher = Fetcher()
        self.fetcher.done.connect(self._apply)
        self.dialog = DetailDialog(self.fetcher)
        self.dock = HubDock(
            on_detail=self.show_detail, on_refresh=self.refresh, on_quit=self.stop,
            cell_actions={
                "webproj": self.open_ru_recite,   # ru-recite 格: 启动+开浏览器
                "mesh": self.open_agentmesh,      # agentmesh 格: 开仪表盘
                "dockhide": lambda: self.set_hidden(True),
            })
        self.dock.pin()
        self._timer = QTimer(self.dock)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        # 本机合成格（屏幕时间）独立刷新——不走网络, 1min 级即可
        self._syn_timer = QTimer(self.dock)
        self._syn_timer.timeout.connect(self._syn_tick)
        self._syn_timer.start(SYNTHETIC_MS)
        log.info("EdgeDock dock 条已启动 (config=%s, state=%s)",
                 ed_paths.project_root() / "config.toml",
                 ed_paths.project_root() / "edgedock.json")
        if initial_refresh:
            self.refresh()

    def stop(self, quiet=False):
        """退出 edgedock：停刷新、拆 dock/详情窗、放互斥体（独立 exe 恢复可用）。"""
        if self.dock is None:
            return
        if self._timer is not None:
            self._timer.stop()
        if getattr(self, "_syn_timer", None) is not None:
            self._syn_timer.stop()
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
        try:
            self.fetcher.done.disconnect(self._apply)
        except TypeError:
            pass
        self.dock.deleteLater()
        self.dock = self.fetcher = self.dialog = self._timer = None
        self._syn_timer = None
        self.snaps = []
        release_mutex(self._mutex)
        self._mutex = None
        log.info("EdgeDock 已退出 (托盘菜单可重新启动)")
        if not quiet:
            self._notify("EdgeDock 已退出（UtilityHub 托盘可重新启动）")

    def _notify(self, text):
        tray = getattr(self.hub, "tray", None)
        if tray is not None:
            tray.notify(text)

    # ---- 托盘菜单状态 ----

    def running(self) -> bool:
        return self.dock is not None

    def is_hidden(self) -> bool:
        return self.dock is not None and self.dock.user_hidden

    def set_hidden(self, on: bool):
        if self.dock is not None:
            self.dock.set_user_hidden(on)

    def game_mode(self) -> bool:
        return bool(self.dock is not None and self.dock.game_mode)

    def set_game_mode(self, on: bool):
        if self.dock is not None:
            self.dock.set_game_mode(on)

    def status_line(self) -> str:
        """托盘子菜单灰显行：更新时间 + 出错平台数。"""
        if not self.snaps:
            return f"查询中… {self._updated_at}".strip()
        errs = sum(1 for s in self.snaps if s.error)
        tail = f"，{errs} 个出错" if errs else ""
        return f"{self._updated_at} 更新{tail}" if self._updated_at else "查询中…"

    # ---- 形态切换（hand / edge）----

    def form(self) -> str:
        return self.dock.form if self.dock is not None else "hand"

    def set_form(self, form: str):
        if self.dock is not None:
            self.dock.set_form(form)
            self._notify("EdgeDock 已切换到%s" % ("边缘模式（右缘悬停 1.2s 弹出）"
                                                if form == "edge" else "hand 模式"))

    # ---- 数据 ----

    def refresh(self):
        if self.fetcher is None:
            return
        log.info("EdgeDock 刷新中…")
        self.fetcher.refresh_async()

    def _apply(self, snaps):
        self.snaps = self._merge_synthetic(snaps)
        self._updated_at = datetime.now().strftime("%H:%M")
        if self.dock is not None:
            self.dock.set_snaps(self.snaps)

    def _syn_tick(self):
        """屏幕时间等本机格 1min 级刷新（不动网络快照）。"""
        if self.dock is None:
            return
        self.snaps = self._merge_synthetic(self.snaps)
        self.dock.set_snaps(self.snaps)

    # ---- 本机合成格（screentime / dockhide）----

    def _merge_synthetic(self, snaps: list) -> list:
        """网络快照 + 本机合成格合并；合成格不存在于 collect()（hub 宿主专属）。"""
        syn = self._synthetic_snaps()
        out = [syn.pop(s.provider_id, s) for s in snaps]
        out.extend(syn.values())
        return out

    def _screen_goal(self) -> float:
        try:
            import tomllib
            cfg = tomllib.loads(
                (ed_paths.project_root() / "config.toml").read_text("utf-8"))
            return float(cfg.get("dock", {}).get("screen_time_goal_min")
                         or SCREEN_GOAL_MIN_DEFAULT)
        except Exception:
            return SCREEN_GOAL_MIN_DEFAULT

    def _synthetic_snaps(self) -> dict:
        out = {}
        # 屏幕时间（focus 记账，Android 数字健康 / Watch 活动环风格渲染）
        focus = getattr(self.hub, "focus", None)
        goal = self._screen_goal()
        if focus is None:
            out["screentime"] = BalanceSnapshot(
                "screentime", "屏幕时间", kind="count", available=False,
                error="Focus 未启用")
        else:
            try:
                usage = focus.tracker.combined_today() or {}
                total_min = round(sum(usage.values()) / 60.0, 1)
                dom, sec = (max(usage.items(), key=lambda kv: kv[1])
                            if usage else ("", 0.0))
                snap = BalanceSnapshot("screentime", "屏幕时间", kind="count")
                snap.available = True
                snap.used = total_min
                snap.limit = goal
                snap.note = (f"今日屏幕 {int(total_min // 60)}h{int(total_min % 60):02d}"
                             f" · 目标 {int(goal // 60)}h")
                snap.extra = {"goal_min": goal, "top": [dom, round(sec / 60.0, 1)]}
                out["screentime"] = snap
            except Exception as e:
                out["screentime"] = BalanceSnapshot(
                    "screentime", "屏幕时间", kind="count",
                    available=False, error=f"{type(e).__name__}: {e}")
        # 隐藏 dock 工具格
        out["dockhide"] = BalanceSnapshot("dockhide", "隐藏 dock", kind="service",
                                          available=True,
                                          note="单击隐藏 dock（托盘可恢复）")
        return out

    # ---- 单元格启动器（鼠标友好的项目入口）----

    def _open_browser(self, url):
        QDesktopServices.openUrl(QUrl(url))

    def _notify_later(self, text):
        self.hub._post(lambda: self._notify(text))

    @staticmethod
    def _tcp_up(host: str, port: int, timeout: float = 0.4) -> bool:
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _open_when_up(self, port: int, url: str, name: str):
        """等本地服务监听后开浏览器（服务由本函数前的 Popen 拉起）。"""
        for _ in range(24):  # 最多 12s
            if self._tcp_up("127.0.0.1", port):
                self.hub._post(lambda u=url: self._open_browser(u))
                return
            time.sleep(0.5)
        self._notify_later(f"{name} 启动失败（12s 未监听 {port} 端口，看其日志）")

    @staticmethod
    def _spawn(cmd: list, cwd: str, log_name: str):
        """拉起项目服务。⚠ 三件事缺一不可:

        1. 输出必须落文件——DEVNULL 时 pythonw 子进程挂了毫无痕迹
           (2026-09-19 ru-recite 点击后无声死亡, 无从归因)。
        2. CREATE_BREAKAWAY_FROM_JOB——计划任务给 hub 的 job 对象在任务结束时
           清整棵进程树, detached 子进程也被连坐 (hub 每次重启, ru-recite 就死;
           11:18 点击启动成功、11:31 hub 重启即被收割)。脱离 job 独立存活;
           job 禁止 breakaway 时回退普通 spawn。
        3. stdio 必须显式 UTF-8——stdout 是文件（不是控制台）时子进程 Python
           退回 locale 编码（本机 cp1252），子模块里的中文 print 会在启动路径上
           直接 UnicodeEncodeError 秒退 (2026-09-23 仪表盘格点击不开浏览器:
           agent_dashboard.py 的 print 含"Ctrl+C 退出"，崩在 bind 之前 →
           _open_when_up 白等 12s)。顺带与日志文件的 utf-8 写入对齐。
        """
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        f = open(os.path.join(ROOT, "logs", log_name), "a", encoding="utf-8",
                 buffering=1)
        try:
            try:
                subprocess.Popen(cmd, cwd=cwd, env=env,
                                 creationflags=flags | 0x01000000,  # BREAKAWAY
                                 stdout=f, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL)
            except OSError:  # job 禁止 breakaway → 回退
                subprocess.Popen(cmd, cwd=cwd, env=env, creationflags=flags,
                                 stdout=f, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL)
        finally:
            f.close()

    def open_ru_recite(self):
        """25ru-recite 格: 没起先拉起, 起来后浏览器开 localhost。"""
        port = RU_PORT_DEFAULT
        try:
            with open(os.path.join(RU_ROOT, "config.json"), encoding="utf-8") as f:
                port = int(json.load(f).get("port") or port)
        except (OSError, ValueError):
            pass
        url = f"http://localhost:{port}"
        if self._tcp_up("127.0.0.1", port):
            self._open_browser(url)
            return
        log.info("ru-recite 未运行, 拉起 25ru-recite (port %d)…", port)
        self._notify("ru-recite 启动中…")
        try:
            self._spawn([sys.executable, "ru_recite.py"], RU_ROOT, "ru-recite-child.log")
        except OSError as e:
            log.warning("ru-recite 启动失败: %s", e)
            self._notify_later(f"ru-recite 启动失败: {e}")
            return
        threading.Thread(target=self._open_when_up, args=(port, url, "ru-recite"),
                         daemon=True).start()

    def open_agentmesh(self):
        """agentmesh 格: 没起先拉起 agent_dashboard, 然后浏览器开仪表盘。"""
        port = MESH_PORT_DEFAULT
        url = f"http://127.0.0.1:{port}"
        if self._tcp_up("127.0.0.1", port):
            self._open_browser(url)
            return
        log.info("agent-mesh dashboard 未运行, 拉起 (port %d)…", port)
        self._notify("agent-mesh 仪表盘启动中…")
        try:
            self._spawn([sys.executable, "agent_dashboard.py"], MESH_SRC, "agent-mesh-child.log")
        except OSError as e:
            log.warning("agent-mesh 启动失败: %s", e)
            self._notify_later(f"agent-mesh 启动失败: {e}")
            return
        threading.Thread(target=self._open_when_up, args=(port, url, "agent-mesh"),
                         daemon=True).start()

    def show_detail(self):
        if self.dialog is None or self.dock is None:
            return
        self.dialog.show()
        # 详情窗贴 dock 左侧弹出（同独立托盘）
        geo = self.dock.geometry()
        self.dialog.move(max(0, geo.left() - self.dialog.width() - 8), geo.top() + 80)
        self.dialog.raise_()
