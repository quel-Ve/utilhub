#!/usr/bin/env python3
"""Utility Hub — 统一快捷键管家 (zorder 快照/恢复 + 任务栏排序)。

用法:
    python hub.py                    # 前台控制台模式 (调试)
    pythonw hub.py --daemon          # 静默后台模式 (日志 logs/hub.log)

热键 (右 Alt 特化体系, 2026-08-14 触发规则定稿):
    右 Alt 按下        仅开会话, 不弹面板
    右 Alt + , . /     槽 1/2/3: 短按 = 空槽快照/非空槽恢复; 长按 >=0.9s = 快照
    右 Alt + ;         面板 + 排序块效果 (arm); 松开 ; 执行任务栏排序
    右 Alt + '         EQ 预设循环
    Ctrl 按住中        trigger 键与面板全部锁住 (不参与组合热键)
    面板出现后         只有 Alt 松开才消失; trigger 松开保留
    Esc (按住右 Alt 时) 取消 (含已 arm 的排序)

--daemon 模式: 单实例 Mutex (Local\\UtilityHub) + PID 文件 logs/hub.pid,
退出: uninstall.bat 或 taskkill /f /pid <logs/hub.pid>。
"""
import faulthandler
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque

ROOT = os.path.dirname(os.path.abspath(__file__))
SORT_ROOT = os.path.join(os.path.dirname(ROOT), "6TaskbarSortTool")
EQ_ROOT = os.path.join(os.path.dirname(ROOT), "21eq-switcher")
VOICE_ROOT = os.path.join(os.path.dirname(ROOT), "11cc-voice-input")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(SORT_ROOT, "python"))
sys.path.insert(0, EQ_ROOT)    # EQ 切换只读复用 21eq-switcher (Switcher 类 + data/)
sys.path.insert(0, VOICE_ROOT)  # VoiceInput 只读复用 11cc-voice-input (VoiceInputCore)

DAEMON = "--daemon" in sys.argv
LOG_DIR = os.path.join(ROOT, "logs")
LOG_FILE = os.path.join(LOG_DIR, "hub.log")
PID_FILE = os.path.join(LOG_DIR, "hub.pid")

from PyQt6.QtCore import QTimer, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import QApplication

from taskbar_sorter.config import ConfigManager
from taskbar_sorter.injector import InjectorManager
from taskbar_sorter.window_detector import sort_windows_by_rules
from zorder import audio, slots, windows
from hotkeys import HotkeyController
from preview import PreviewWindow, preset_summary_lines
from tray import Tray

log = logging.getLogger("hub")

DIAG_CONFIG = os.path.join(ROOT, "config.json")


def _diag():
    """读 config.json 的 diagnostic 段 (缺省/读不到 = 空 dict = 全功能)。"""
    try:
        with open(DIAG_CONFIG, encoding="utf-8") as f:
            return json.load(f).get("diagnostic", {})
    except (OSError, ValueError):
        return {}


def _disabled(name):
    """诊断开关: config.json → diagnostic.disable_<name> (二分禁用崩溃子系统)。"""
    return bool(_diag().get("disable_" + name))


def _load_sibling_module(module_name, root, filename="main.py"):
    """按绝对路径加载子项目模块。

    多个子项目 (21eq-switcher / 11cc-voice-input) 都有 main.py, sys.path
    导入会互相串 — importlib 显式按文件加载, 消除同名歧义 (2026-08-13)。
    """
    import importlib.util
    path = os.path.join(root, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def setup_logging():
    fmt = "%(asctime)s %(levelname)s %(message)s"
    if DAEMON:
        os.makedirs(LOG_DIR, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format=fmt,
                            handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")])
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)


def acquire_single_instance() -> bool:
    import win32api
    import win32event
    import winerror
    global _mutex_handle
    _mutex_handle = win32event.CreateMutex(None, False, r"Local\UtilityHub")
    return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS


class Hub:
    """动作编排: 热键/托盘回调全部经 _post 切回主线程执行 (Qt 线程亲和)。"""

    def __init__(self):
        self.app = QApplication(sys.argv)
        self.preview = PreviewWindow()
        self.controller = HotkeyController(self, enabled=not _disabled("hotkeys"))
        self.tray = Tray(self, enabled=not _disabled("tray"))
        self._sort_lock = threading.Lock()
        self._last_sort = 0.0
        # 主线程分发队列: 钩子线程 → _post → 20ms QTimer drain
        self._queue = deque()
        self._qtimer = QTimer()
        self._qtimer.timeout.connect(self._drain)
        self._qtimer.start(20)
        # EQ 切换 (21eq-switcher 只读复用)。初始化失败不阻塞 hub。
        self.eq = None
        self._eq_stop = threading.Event()
        self._init_eq()
        # VoiceInput (11cc-voice-input 只读复用, Pause 开关录音)。
        self.vi = None
        self._init_voice()

    def _init_voice(self):
        if _disabled("voice"):
            log.info("VoiceInput 已禁用 (diagnostic.disable_voice)")
            return
        try:
            voice_main = _load_sibling_module("voice_input_main", VOICE_ROOT)
            self.vi = voice_main.VoiceInputCore()
            self.vi.start_hotkey()   # 自带 pynput 监听处理 Pause toggle
            log.info("VoiceInput 已集成: Pause 开关录音")
        except BaseException as e:
            log.warning("VoiceInput 集成失败 (功能禁用): %s", e)
            self.vi = None

    def _init_eq(self):
        if _disabled("eq"):
            log.info("EQ 切换已禁用 (diagnostic.disable_eq)")
            return
        try:
            eq_main = _load_sibling_module("eq_switcher_main", EQ_ROOT)
            # 抑制右下角 toast: 反馈统一走 eq_panel 面板 (2026-08-13 用户要求)
            try:
                eq_main.toast.show = lambda *a, **k: None
            except Exception:
                pass
            self.eq = eq_main.Switcher(eq_main.load_config())
            if not self.eq.preset_order:
                log.error("EQ 预设为空, 禁用 EQ 切换")
                self.eq = None
                return
            self._eq_root = EQ_ROOT
            threading.Thread(target=self._eq_poll_loop, daemon=True).start()
            log.info("EQ 切换已启用: 预设=%s, 当前=%s",
                     list(self.eq.presets), self.eq.current)
        except BaseException as e:
            # Switcher 在 Equalizer APO 缺失时 SystemExit —— 必须捕获
            log.warning("EQ 切换初始化失败 (功能禁用): %s", e)
            self.eq = None

    def _eq_poll_loop(self):
        while not self._eq_stop.is_set():
            try:
                self.eq.tick()
            except Exception:
                log.exception("EQ 轮询异常")
            self._eq_stop.wait(self.eq.cfg.get("poll_interval_s", 1))

    # ---------- EQ 动作 (热键/托盘回调, 经 _post 切主线程) ----------

    def eq_cycle(self):
        if self.eq:
            self._post(self._eq_cycle_and_panel)

    def _eq_cycle_and_panel(self):
        self.eq.cycle_preset()
        self._show_eq_panel()

    def _refresh_eq_entries(self):
        entries = []
        for name in self.eq_presets():
            # self.eq.presets[name] 是预设文件内容 (Switcher 初始化已加载),
            # 不是文件名 — 2026-08-13 曾误当路径 open 导致摘要全空
            text = self.eq.presets.get(name, "") or ""
            entries.append((name, preset_summary_lines(text) if text else ""))
        return entries

    def _show_eq_panel(self):
        """EQ 切换后刷新预览窗右侧表格 (2026-08-13 用户要求: 拼接进预览窗)。

        会话驱动: 右 Alt/Ctrl+Alt 按住中 → 只刷新高亮不隐藏;
        无会话 (F12 单按/托盘) → 显示 1.6s 后隐藏。
        """
        entries = self._refresh_eq_entries()
        self.preview.set_eq_entries(entries, self.eq_current())
        if self.preview.session_visible:
            return                          # 会话中: 保持显示
        self.preview.show_temp()
        self.later(1.6, self.preview.hide_preview)

    def eq_current(self):
        return self.eq.current if self.eq else "disabled"

    def eq_presets(self):
        """全部预设 (托盘手动切换菜单用)。

        preset_order 优先, 未入序的 (如 game — 仅游戏白名单自动触发) 排后。
        用户要求五套全列可手动切。
        """
        if not self.eq:
            return []
        ordered = list(self.eq.preset_order)
        for name in self.eq.presets:
            if name not in ordered:
                ordered.append(name)
        return ordered

    def eq_set_preset(self, name):
        """手动切换预设 — 不关自动: 网易云学习/推断继续采集。"""
        if self.eq:
            self._post(lambda: self._eq_set_and_panel(name))

    def _eq_set_and_panel(self, name):
        self.eq.set_preset(name)
        self._show_eq_panel()

    def eq_auto_enabled(self):
        return bool(self.eq and self.eq._auto)

    def eq_set_auto(self, on):
        if self.eq:
            self.eq.set_auto(on)

    def eq_preset_summary(self, name):
        """预设文件 → 设置摘要 "Preamp -5dB · 5 filters" (托盘菜单内联显示)。"""
        if not self.eq or not name:
            return ""
        rel = self.eq.presets.get(name)
        if not rel:
            return ""
        try:
            with open(os.path.join(EQ_ROOT, "data", "presets", rel),
                      encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return ""
        preamp = ""
        filters = 0
        for line in text.splitlines():
            if line.lower().startswith("preamp"):
                m = re.search(r"[-+]?\d+(\.\d+)?", line)
                if m:
                    preamp = m.group(0) + "dB"
            elif line.startswith("Filter ") and " ON " in line:
                filters += 1
        parts = [("Preamp " + preamp) if preamp else "",
                 f"{filters} filters" if filters else ""]
        return " · ".join(p for p in parts if p)

    def open_eq_editor(self):
        """打开 EqualizerAPO Editor — 显示当前 config.txt = 当前激活预设的完整设置。

        Editor.exe 必须带 cwd=EqualizerAPO 目录, 否则 Qt 找不到相对 qt/ 平台插件
        报 "no Qt platform plugin" (与 DeviceSelector 同坑, 2026-08-16)。
        """
        eq_dir = r"D:\Program Files\EqualizerAPO"
        editor = os.path.join(eq_dir, "Editor.exe")
        if os.path.exists(editor):
            subprocess.Popen([editor], cwd=eq_dir)
        else:
            log.warning("EqualizerAPO Editor.exe 未找到")

    # ---------- 线程分发 ----------

    def _post(self, fn):
        self._queue.append(fn)

    def _drain(self):
        while self._queue:
            fn = self._queue.popleft()
            try:
                fn()
            except Exception:
                log.exception("动作执行异常")

    def later(self, sec, fn):
        """延迟后切主线程执行 (死区提示保留定时器)。"""
        def _t():
            time.sleep(sec)
            self._post(fn)
        threading.Thread(target=_t, daemon=True).start()

    # ---------- 任务栏排序 ----------

    def sort_preview(self, on):
        """Alt+; 按住中: 预览面板左块排序效果开关 (2026-08-14)。"""
        self._post(lambda: self.preview.set_sort_armed(on))

    def sort_now(self):
        self._post(self._sort_once)

    def _sort_once(self):
        if not self._sort_lock.acquire(blocking=False):
            log.info("上一次排序还在进行,跳过")
            return
        try:
            mgr = InjectorManager()
            items = mgr.probe().get("items", [])
            if not items:
                raise RuntimeError("探测失败: 没有任务栏分组")
            preset = ConfigManager().get_active_preset()
            ordered = sort_windows_by_rules(items, preset.get("rules", []))
            for i, item in enumerate(ordered):
                item["target_index"] = i
            result = mgr.sort(ordered)
            msg = result.get("message") or result.get("error") or "?"
            if result.get("success"):
                log.info("排序成功: %s", msg)
                audio.play_success()
            else:
                log.error("排序失败: %s", msg)
        except Exception as e:
            log.exception("排序异常: %s", e)
        finally:
            self._sort_lock.release()

    # ---------- zorder 槽位动作 ----------

    def snapshot(self, slot):
        self._post(lambda: self._do_snapshot(slot))

    def _do_snapshot(self, slot):
        data = windows.capture_windows()
        fg_pid = 0
        fg = windows_foreground()
        if fg:
            fg_pid = fg
        taken = slots.now_str()
        json_ok, png_ok = slots.save_slot(slot, data, fg_pid, taken)
        if not json_ok:
            self.tray.notify(f"Slot {slot} snapshot FAILED: write error")
            log.error("快照槽 %d 写入失败", slot)
            return
        log.info("快照槽 %d ok (%d windows%s)", slot, len(data),
                 "" if png_ok else ", 无截图")
        audio.play_success()
        self.tray.notify(f"Slot {slot} snapshot saved ({len(data)} windows)"
                         + ("" if png_ok else ", no screenshot"))
        self.preview.refresh_slots()

    def restore(self, slot):
        self._post(lambda: self._do_restore(slot))

    def _do_restore(self, slot):
        data = slots.load_slot(slot)
        if data is None:
            self.tray.notify(f"Slot {slot} empty — nothing to restore")
            log.info("恢复槽 %d 为空,跳过", slot)
            return
        ok = windows.restore_slot(data["windows"])
        log.info("恢复槽 %d ok=%d/%d", slot, ok, len(data["windows"]))
        windows.bring_foreground(data["windows"], data["foreground_pid"])
        self.tray.notify(f"Slot {slot} restored ({ok}/{len(data['windows'])} windows)")
        self.preview.refresh_slots()

    def clear(self, slot):
        self._post(lambda: self._do_clear(slot))

    def _do_clear(self, slot):
        slots.clear_slot(slot)
        self.preview.refresh_slots()
        self.tray.notify(f"Slot {slot} cleared")

    def slot_action(self, slot):
        """右 Alt 特化键位语义: 空槽=快照, 非空槽=恢复 (与短按 1/2/3 一致)。"""
        self._post(lambda: self._do_slot_action(slot))

    def _do_slot_action(self, slot):
        if slots.slot_has_snapshot(slot):
            self._do_restore(slot)
        else:
            self._do_snapshot(slot)

    # ---------- 热键回调 (经 _post 切主线程) ----------

    def slot_has(self, slot):
        return slots.slot_has_snapshot(slot)

    def hold_start(self, slot):
        self._post(lambda: self.preview.show_preview(slot))

    def set_hold(self, slot, hold_ms):
        self._post(lambda: self.preview.set_hold(slot, hold_ms))

    def show_preview(self, slot):
        self._post(lambda: self._show_preview(slot))

    def _show_preview(self, slot):
        # 右 Alt/Ctrl+Alt 按住弹出: 注入 EQ 表格数据 (2026-08-13)
        if self.eq and not self.preview._eq_entries:
            self.preview.set_eq_entries(self._refresh_eq_entries(), self.eq_current())
        self.preview.show_preview(slot)

    def hide_preview(self):
        self._post(self.preview.hide_preview)

    def cancel(self):
        self._post(self.preview.hide_preview)

    def set_message(self, msg):
        self._post(lambda: self.preview.set_message(msg))

    def on_dead_zone(self):
        self._post(self._on_dead_zone)

    def _on_dead_zone(self):
        self.preview.set_message("Hold longer (>=0.9s) to snapshot")
        self.later(0.8, self.preview.hide_preview)

    # ---------- 托盘 ----------

    def open_log_dir(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(LOG_DIR))

    def quit(self):
        log.info("托盘退出")
        # 标记正常退出 (看门狗据此不弹"闪退"提示)。写在前: 后续 teardown 若
        # 触发 python312.dll 硬崩溃 (事件日志 11:32/11:53 的 0xc0000005 即退出路径),
        # 标记已落盘, 看门狗仍判为正常退出。
        try:
            with open(CLEAN_EXIT_FILE, "w", encoding="utf-8") as f:
                f.write("clean")
        except OSError:
            pass
        self._eq_stop.set()
        if self.vi:
            try:
                self.vi.stop()
            except Exception:
                pass
        self.controller.stop()
        self.tray.destroy()
        self.app.quit()

    # ---------- 主循环 ----------

    def run(self):
        log.info("=" * 50)
        log.info("Utility Hub %s — 右Alt+,./ 槽位 / 右Alt+; 排序 / 右Alt+' EQ",
                 "daemon" if DAEMON else "console")
        log.info("=" * 50)
        self.controller.start()
        self._welcome_toast()
        self.app.exec()
        self.controller.stop()

    def _welcome_toast(self):
        """启动欢迎 toast (2026-08-14): 复用预览窗 show_temp + 消息 + 自动消失。

        免去启动后检查托盘确认是否成功; 顺带在首次显示时触发亚克力安装。
        """
        try:
            if self.eq:
                try:
                    self.preview.set_eq_entries(self._refresh_eq_entries(),
                                                self.eq_current())
                except Exception:
                    pass
            self.preview.set_message("UtilityHub 已启动 — 右Alt + , . / 槽位 · ; 排序 · ' EQ")
            self.preview.show_temp()
            self.later(1.8, self._dismiss_welcome)
        except Exception:
            log.exception("欢迎 toast 失败")

    def _dismiss_welcome(self):
        # 1.8s 后: 若用户已进入热键会话 (右Alt 按住中), 只清消息不抢关预览
        if self.preview.session_visible:
            self.preview.set_message("")
        else:
            self.preview.hide_preview()


def windows_foreground():
    import win32gui
    import win32process
    h = win32gui.GetForegroundWindow()
    if not h:
        return 0
    return win32process.GetWindowThreadProcessId(h)[1]


CLEAN_EXIT_FILE = os.path.join(LOG_DIR, "hub.clean_exit")


def _launch_watchdog(pid):
    """拉起崩溃看门狗 (分离子进程): hub 崩溃时弹右下角 toast。

    DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 独立于 hub 生命周期 —
    hub 硬崩溃 (0xc0000005) 不会带死看门狗, 看门狗才能报丧。
    """
    try:
        watchdog = os.path.join(ROOT, "watchdog.py")
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [sys.executable, watchdog, str(pid)],
            cwd=ROOT,
            creationflags=flags,
            close_fds=True,
        )
    except Exception as e:
        log.warning("崩溃看门狗启动失败: %s", e)


def main():
    setup_logging()
    # 崩溃时 dump 全部线程 Python 栈到 logs/faulthandler.log — 定位 python312.dll
    # 0xc0000005 硬崩溃 (若日志为空 = 纯原生崩溃, 佐证 ctypes 回调生命周期问题)。
    os.makedirs(LOG_DIR, exist_ok=True)
    faulthandler.enable(
        file=open(os.path.join(LOG_DIR, "faulthandler.log"), "a", encoding="utf-8"),
        all_threads=True)
    if DAEMON:
        if not acquire_single_instance():
            return
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        # 清掉上次遗留的"正常退出"标记 (否则本次崩溃会被误判为正常)
        try:
            if os.path.exists(CLEAN_EXIT_FILE):
                os.remove(CLEAN_EXIT_FILE)
        except OSError:
            pass
        _launch_watchdog(os.getpid())
    Hub().run()


if __name__ == "__main__":
    main()
