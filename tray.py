"""系统托盘 — 原生 Win32 (ctypes), 替代 QSystemTrayIcon。

原因: Qt 的 showMessage 无静音选项 (NIIF_NONE 会播放系统默认提示音), 快照/恢复时
与自定义叮咚形成双层音效。原生 Shell_NotifyIconW + NIIF_NOSOUND 静音气泡,
tooltip 仅 "Utility Hub"。

结构: 顶层隐藏窗口收 WM_TRAY 回调 → 右键弹 Qt QMenu（暗夜模式, app 级 NIGHT_QSS;
原生 TrackPopupMenu 无法着色, 2026-09-19 换 QMenu）; 左键双击 = 快照槽 1
(与 zorder C++ 托盘行为一致)。图标 + 静音气泡仍走原生 Shell_NotifyIconW。
"""
import ctypes
import os
import struct
import subprocess
from ctypes import wintypes

from PyQt6.QtGui import QColor, QCursor, QPalette
from PyQt6.QtWidgets import QMenu

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --- 64 位指针必须显式声明, 否则 ctypes 默认按 32 位 c_int 截断 ---
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.CreatePopupMenu.restype = wintypes.HANDLE
user32.DestroyMenu.argtypes = [wintypes.HANDLE]
user32.DestroyMenu.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_ulonglong,
                               wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.CreateIconFromResourceEx.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                            wintypes.BOOL, wintypes.DWORD,
                                            ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.CreateIconFromResourceEx.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HANDLE

# --- 常量 ---
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_NOSOUND = 0x1, 0x8
WM_TRAY = 0x8000 + 1
WM_RBUTTONUP, WM_LBUTTONDBLCLK, WM_CONTEXTMENU = 0x205, 0x203, 0x7B
WM_NULL = 0
# Explorer 托盘重建时广播此消息 → 收到后重新 NIM_ADD (登录早期启动图标丢失的修复)
WM_TASKBARCREATED = user32.RegisterWindowMessageW("TaskbarCreated")

# 菜单项命令值: 仅 hub.focus_status_lines() 返回的 cmd 用 (其余菜单项直连回调,
# 不再有 ID 表 — 原 ID_EQ_PRESET0 动态区间撞号事故见 git 历史)
ID_FOCUS_PAUSE, ID_FOCUS_CONFIG, ID_FOCUS_DATA = 151, 152, 153

TASK_NAME = "UtilityHub"
TRAY_CLASS = "HubTrayMsgWindow"

# 暗夜模式 (sunset 系, 同 28edge-dock): 托盘 QMenu + 全进程 QToolTip。
# app 级样式表 → dock 右键菜单 / 详情窗等所有 QMenu/QToolTip 一并染暗。
NIGHT_QSS = """
QToolTip { background-color: #12090d; color: #D9CCD2; border: 1px solid #6E4F5B;
           padding: 6px 9px; font-size: 12px; }
QMenu { background-color: #12090d; color: #D9CCD2; border: 1px solid #6E4F5B; }
QMenu::item { padding: 5px 26px 5px 16px; }
QMenu::item:selected { background-color: #32222A; }
QMenu::item:disabled { color: #8A7580; }
QMenu::separator { height: 1px; background-color: #43262F; margin: 4px 8px; }
QMenu::indicator { width: 12px; height: 12px; margin-left: 8px;
                   border: 1px solid #6E4F5B; border-radius: 2px;
                   background-color: #1A1014; }
QMenu::indicator:checked { background-color: #C95D81; }
"""


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HANDLE),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeout", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class WNDCLASSEXW(ctypes.Structure):
    """ctypes.wintypes 无 WNDCLASSEXW, 手写 (x64: 80 字节)。"""
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HANDLE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10


def _make_icon_handle():
    """托盘图标: 优先 LoadImageW 读 hub.ico 文件 (Shell 同源, 已验证有效)。

    CreateIconFromResourceEx 内存加载 32bpp 无掩码 ICO 会解析失败 → 空白图标
    (开始菜单能显示而托盘空白的原因: 前者读文件, 后者走内存构造)。
    文件读取失败时回退到内存构造。
    """
    ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub.ico")
    if os.path.exists(ico):
        h = user32.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if h:
            return h
    return _build_icon_in_memory()


def _build_icon_in_memory():
    """回退: 16x16 32bpp ICO 内存构造 (无掩码, 部分场景解析失败)。"""
    W = H = 16
    pixels = bytearray()
    for y in range(H):
        for x in range(W):
            dx, dy = x - 7.5, y - 7.5
            r = (dx * dx + dy * dy) ** 0.5
            if r <= 6.5:
                a = 255 if r <= 5.5 else int(255 * (6.5 - r))
                px = (255, 105, 180, a)
            else:
                px = (30, 30, 30, 255)
            pixels += bytes((px[2], px[1], px[0], px[3]))  # BGRA
    header = struct.pack("<HHH", 0, 1, 1)
    img_size = 40 + len(pixels)
    entry = struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, img_size, 22)
    bmi = struct.pack("<IiiHHIIiiII", 40, W, H * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    ico = header + entry + bmi + bytes(pixels)
    buf = ctypes.create_string_buffer(ico)
    return user32.CreateIconFromResourceEx(buf, len(ico), True, 0x00030000,
                                           W, H, 0)


def _task_exists() -> bool:
    # ⚠ CREATE_NO_WINDOW: 本函数**每次构建托盘菜单**都要调 (每右键一次)，
    # 不带此标志 schtasks.exe 会弹一个可见黑框 → 用户每右键托盘闪一次。
    return subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                          capture_output=True,
                          creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0


def _task_set(on: bool) -> bool:
    if on:
        py = subprocess.run(["where", "python"], capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
        if py.returncode != 0:
            return False
        for line in py.stdout.splitlines():
            if line.strip().lower().endswith("python.exe"):
                pythonw = os.path.join(os.path.dirname(line.strip()), "pythonw.exe")
                if os.path.exists(pythonw):
                    hub = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub.py")
                    tr = f'"{pythonw}" "{hub}" --daemon'
                    r = subprocess.run(
                        ["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr,
                         "/sc", "onlogon", "/ru", os.environ.get("USERNAME", ""),
                         "/rl", "highest", "/f"],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    return r.returncode == 0
        return False
    return subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                          capture_output=True,
                          creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0


class Tray:
    def __init__(self, hub, enabled=True):
        self.hub = hub
        self._nid = None
        self._hwnd = None
        self._wndproc_ref = None  # 防 GC
        self._menu_ref = None     # popup() 非模态菜单的存活引用 (防 GC 提前销毁)
        if enabled:
            self._create_window()
            self._add_icon()
        # 禁用时 _hwnd/_nid 保持 None: notify()/destroy() 已有 None 守卫

    # ---------- 窗口 ----------

    def _create_window(self):
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        self._wndproc_ref = WNDPROC(self._wndproc)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = TRAY_CLASS
        user32.RegisterClassExW(ctypes.byref(wc))
        # 顶层隐藏窗口（非 HWND_MESSAGE）：TaskbarCreated 经 HWND_BROADCAST 只广播
        # 到顶层窗口，消息专用窗口 "does not receive broadcast messages" → explorer
        # 重启后重挂图标逻辑从未触发，托盘图标消失 (2026-08-25 修复)。
        # WS_EX_TOOLWINDOW(0x80)：隐藏窗口不进任务栏/Alt-Tab；无父窗口 + 无 WS_VISIBLE。
        self._hwnd = user32.CreateWindowExW(
            0x80, TRAY_CLASS, None, 0,
            0, 0, 0, 0, None, None, wc.hInstance, None)

    def _add_icon(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = _make_icon_handle()
        nid.szTip = "Utility Hub"  # tooltip 不带功能描述 (用户已知悉)
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._nid = nid

    def destroy(self):
        if self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    # ---------- 气泡 (静音) ----------

    def notify(self, text):
        """NIIF_NOSOUND 静音气泡 — 快照/恢复时不再叠加系统默认提示音。"""
        if self._nid is None:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_INFO
        nid.szInfo = text[:255]
        nid.szInfoTitle = "Utility Hub"
        nid.dwInfoFlags = NIIF_INFO | NIIF_NOSOUND
        nid.uTimeout = 2000
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # ---------- 右键菜单 (Qt QMenu, 暗夜) ----------

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_TRAY:
            lmsg = lp & 0xFFFF
            if lmsg in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._popup_menu()
                return 0
            if lmsg == WM_LBUTTONDBLCLK:
                self.hub.snapshot(1)  # 双击 = 快照槽 1
                return 0
        if msg == WM_TASKBARCREATED:
            self._add_icon()  # 托盘重建 (登录早期/Explorer 重启) 后重挂图标
            # explorer 重启 → 任务栏彩虹自愈 (autosort 内部有节流+独立线程, 不阻塞 wndproc)
            autosort = getattr(self.hub, "autosort", None)
            if autosort:
                autosort.notify_shell_restart()
            return 0
        if msg == WM_NULL:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    _FOCUS_ACTIONS = {ID_FOCUS_PAUSE: "focus_toggle_pause",
                      ID_FOCUS_CONFIG: "open_focus_config",
                      ID_FOCUS_DATA: "open_focus_data"}

    def _build_menu(self) -> QMenu:
        """托盘右键菜单, 每次右键现建 — EQ 勾选/EdgeDock 运行态自然最新。

        暗色由 app 级 NIGHT_QSS 接管; palette 补齐是给子菜单箭头等 QSS 没盖到的
        style 元素用 (QPalette 文字色暗掉, 箭头才不隐身)。
        """
        h = self.hub
        menu = QMenu()
        pal = menu.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#12090d"))
        pal.setColor(QPalette.ColorRole.WindowText, QColor("#D9CCD2"))
        pal.setColor(QPalette.ColorRole.Text, QColor("#D9CCD2"))
        pal.setColor(QPalette.ColorRole.Highlight, QColor("#32222A"))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#E8C7D4"))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor("#D9CCD2"))
        menu.setPalette(pal)

        act = menu.addAction("任务栏排序 (右Alt+;)")
        act.triggered.connect(h.sort_now)
        menu.addSeparator()
        self._add_slot_menu(menu, "快照 Snapshot (右Alt 长按 ,./)", h.snapshot)
        self._add_slot_menu(menu, "恢复 Restore (右Alt 短按 ,./)", h.restore)
        self._add_slot_menu(menu, "清空 Clear", h.clear)
        menu.addSeparator()
        # EQ 平铺 (无子菜单): 预设项带勾选 + 内联设置摘要 (摘要即原生 tooltip 替代)
        current = h.eq_current()
        act = menu.addAction("循环预设 (右Alt+')")
        act.triggered.connect(h.eq_cycle)
        for name in h.eq_presets():
            summary = h.eq_preset_summary(name)
            label = f"{name}  · {summary}" if summary else name
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name == current)
            act.triggered.connect(lambda checked=False, n=name: h.eq_set_preset(n))
        act = menu.addAction("自动切换")
        act.setCheckable(True)
        act.setChecked(h.eq_auto_enabled())
        act.triggered.connect(lambda checked: h.eq_set_auto(checked))
        act = menu.addAction("打开调整面板 (EqualizerAPO Editor)")
        act.triggered.connect(h.open_eq_editor)
        cur_sum = h.eq_preset_summary(current)
        info = menu.addAction(f"当前: {current}  · {cur_sum}" if cur_sum
                              else f"当前: {current}")
        info.setEnabled(False)
        menu.addSeparator()
        self._append_edgedock(menu)
        menu.addSeparator()
        self._append_focus(menu)
        menu.addSeparator()
        act = menu.addAction("开机自启 (计划任务)")
        act.setCheckable(True)
        act.setChecked(_task_exists())
        act.triggered.connect(self._toggle_autostart)
        act = menu.addAction("打开日志目录")
        act.triggered.connect(h.open_log_dir)
        act = menu.addAction("退出")
        act.triggered.connect(h.quit)
        return menu

    def _add_slot_menu(self, menu, label, fn):
        sub = menu.addMenu(label)
        for i in range(1, 4):
            act = sub.addAction(f"Slot {i}")
            act.triggered.connect(lambda checked=False, s=i: fn(s))

    def _append_edgedock(self, menu):
        """EdgeDock 子菜单: 隐藏/显示、退出/启动(恢复)、刷新、详情、游戏模式。"""
        sub = menu.addMenu("EdgeDock (右缘)")
        ed = getattr(self.hub, "edgedock", None)
        if ed is None:
            act = sub.addAction("未启用 (diagnostic.disable_edgedock)")
            act.setEnabled(False)
            return
        if not ed.running():
            act = sub.addAction("启动 EdgeDock (恢复)")
            act.triggered.connect(ed.start)
            return
        act = sub.addAction("隐藏 dock" if not ed.is_hidden() else "显示 dock")
        act.triggered.connect(lambda: ed.set_hidden(not ed.is_hidden()))
        act = sub.addAction("立即刷新")
        act.triggered.connect(ed.refresh)
        act = sub.addAction("详情窗口")
        act.triggered.connect(ed.show_detail)
        act = sub.addAction("游戏模式（隐藏 dock）")
        act.setCheckable(True)
        act.setChecked(ed.game_mode())
        act.triggered.connect(lambda checked: ed.set_game_mode(checked))
        act = sub.addAction("边缘模式（无 hand，右缘悬停 1.2s 弹出）")
        act.setCheckable(True)
        act.setChecked(ed.form() == "edge")
        act.triggered.connect(lambda checked: ed.set_form("edge" if checked else "hand"))
        if ed.snaps:
            sub.addSeparator()
            head = sub.addAction(ed.status_line())
            head.setEnabled(False)
            from edgedock.tray import _row_text  # 局部 import: tray 不强依赖 edgedock
            for s in ed.snaps:
                row = sub.addAction(_row_text(s))
                row.setEnabled(False)
        sub.addSeparator()
        act = sub.addAction("退出 EdgeDock")
        act.triggered.connect(ed.stop)

    def _append_focus(self, menu):
        sub = menu.addMenu("专注 Focus")
        for label, cmd in self.hub.focus_status_lines():
            act = sub.addAction(label)
            if cmd:
                act.triggered.connect(getattr(self.hub, self._FOCUS_ACTIONS[cmd]))
            else:
                act.setEnabled(False)

    def _toggle_autostart(self):
        ok = _task_set(not _task_exists())
        self.notify("开机自启已开启 (计划任务)" if ok and _task_exists()
                    else ("开机自启已关闭" if ok else "设置失败: 需要管理员权限运行"))

    def _popup_menu(self):
        # popup() 非模态: 立即返回, 主线程不进嵌套循环。exec() 的模态循环在托盘
        # 这种无前台窗口场景会因焦点竞争挂死主线程 → watchdog 判"无响应"误杀
        # (2026-09-19 09:52/09:55 两次重启即此形态)。
        # ⚠ popup() 后本方法立刻返回, 局部引用一出作用域就被 GC → 菜单刚弹即毁
        # (用户报"右键不再弹出"), 必须自持引用, aboutToHide 再放。
        menu = self._build_menu()
        self._menu_ref = menu
        menu.aboutToHide.connect(self._menu_dismissed)
        menu.popup(QCursor.pos())

    def _menu_dismissed(self):
        menu = self._menu_ref
        self._menu_ref = None
        if menu is not None:
            menu.deleteLater()
