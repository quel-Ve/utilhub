"""系统托盘 — 原生 Win32 (ctypes), 替代 QSystemTrayIcon。

原因: Qt 的 showMessage 无静音选项 (NIIF_NONE 会播放系统默认提示音), 快照/恢复时
与自定义叮咚形成双层音效。原生 Shell_NotifyIconW + NIIF_NOSOUND 静音气泡,
tooltip 仅 "Utility Hub"。

结构: 隐藏消息窗口 (HWND_MESSAGE) 收 WM_TRAY 回调 → 右键 TrackPopupMenu 原生菜单;
左键双击 = 快照槽 1 (与 zorder C++ 托盘行为一致)。
"""
import ctypes
import os
import struct
import subprocess
from ctypes import wintypes

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
MF_POPUP, MF_STRING, MF_SEPARATOR = 0x10, 0, 0x800
MF_CHECKED, MF_UNCHECKED, MF_GRAYED = 0x8, 0, 0x1
MF_BYCOMMAND = 0x400
TPM_RETURNCMD, TPM_NONOTIFY = 0x100, 0x80

# 菜单项 ID
ID_SORT = 100
ID_SNAP1, ID_RESTORE1, ID_CLEAR1 = 101, 111, 121
ID_EQ_CYCLE, ID_EQ_AUTO = 130, 131
ID_EQ_PRESET0 = 200  # 预设手动切换: 200 + 索引 (数量动态) — 高位区间, 避免与固定 ID 冲突
# 曾撞号: 原 132 起, ID_EQ_EDITOR=133 落在预设区间内 → 点"调整面板"误触发 indie 预设+预览窗 (2026-08-16)
ID_EQ_EDITOR = 133   # 打开 EqualizerAPO Editor (当前预设调整面板)
ID_AUTOSTART, ID_LOG, ID_EXIT = 140, 141, 142

TASK_NAME = "UtilityHub"
TRAY_CLASS = "HubTrayMsgWindow"


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
    return subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                          capture_output=True).returncode == 0


def _task_set(on: bool) -> bool:
    if on:
        py = subprocess.run(["where", "python"], capture_output=True, text=True)
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
                        capture_output=True)
                    return r.returncode == 0
        return False
    return subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                          capture_output=True).returncode == 0


class Tray:
    def __init__(self, hub, enabled=True):
        self.hub = hub
        self._nid = None
        self._hwnd = None
        self._wndproc_ref = None  # 防 GC
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
        self._hwnd = user32.CreateWindowExW(0, TRAY_CLASS, None, 0,
                                            0, 0, 0, 0, wintypes.HWND(-3),
                                            None, wc.hInstance, None)

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

    # ---------- 右键菜单 ----------

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
            return 0
        if msg == WM_NULL:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _append_slots(self, menu, base_id, label):
        sub = user32.CreatePopupMenu()
        for i in range(1, 4):
            user32.AppendMenuW(sub, MF_STRING, base_id + i - 1, f"Slot {i}")
        user32.AppendMenuW(menu, MF_POPUP, sub, label)
        return sub

    def _popup_menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_SORT, "任务栏排序 (右Alt+;)")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        self._append_slots(menu, ID_SNAP1, "快照 Snapshot (右Alt 长按 ,./)")
        self._append_slots(menu, ID_RESTORE1, "恢复 Restore (右Alt 短按 ,./)")
        self._append_slots(menu, ID_CLEAR1, "清空 Clear")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        # EQ 平铺 (无子菜单): 预设项带勾选 + 内联设置摘要 (原生菜单无悬停 tooltip,
        # 摘要即替代方案)
        current = self.hub.eq_current()
        user32.AppendMenuW(menu, MF_STRING, ID_EQ_CYCLE, "循环预设 (右Alt+')")
        for i, name in enumerate(self.hub.eq_presets()):
            summary = self.hub.eq_preset_summary(name)
            label = f"{name}  · {summary}" if summary else name
            flags = MF_STRING | (MF_CHECKED if name == current else MF_UNCHECKED)
            user32.AppendMenuW(menu, flags, ID_EQ_PRESET0 + i, label)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if self.hub.eq_auto_enabled() else MF_UNCHECKED),
                           ID_EQ_AUTO, "自动切换")
        user32.AppendMenuW(menu, MF_STRING, ID_EQ_EDITOR,
                           "打开调整面板 (EqualizerAPO Editor)")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        cur_sum = self.hub.eq_preset_summary(current)
        cur_label = f"当前: {current}  · {cur_sum}" if cur_sum else f"当前: {current}"
        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, cur_label)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if _task_exists() else MF_UNCHECKED),
                           ID_AUTOSTART, "开机自启 (计划任务)")
        user32.AppendMenuW(menu, MF_STRING, ID_LOG, "打开日志目录")
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "退出")
        user32.SetForegroundWindow(self._hwnd)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY,
                                    pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            self._dispatch(cmd)

    def _dispatch(self, cmd):
        h = self.hub
        if cmd == ID_SORT:
            h.sort_now()
        elif ID_SNAP1 <= cmd < ID_SNAP1 + 3:
            h.snapshot(cmd - ID_SNAP1 + 1)
        elif ID_RESTORE1 <= cmd < ID_RESTORE1 + 3:
            h.restore(cmd - ID_RESTORE1 + 1)
        elif ID_CLEAR1 <= cmd < ID_CLEAR1 + 3:
            h.clear(cmd - ID_CLEAR1 + 1)
        elif cmd == ID_EQ_CYCLE:
            h.eq_cycle()
        elif ID_EQ_PRESET0 <= cmd < ID_EQ_PRESET0 + len(h.eq_presets()):
            h.eq_set_preset(h.eq_presets()[cmd - ID_EQ_PRESET0])
        elif cmd == ID_EQ_AUTO:
            h.eq_set_auto(not h.eq_auto_enabled())
        elif cmd == ID_EQ_EDITOR:
            h.open_eq_editor()
        elif cmd == ID_AUTOSTART:
            ok = _task_set(not _task_exists())
            self.notify("开机自启已开启 (计划任务)" if ok and _task_exists()
                        else ("开机自启已关闭" if ok else "设置失败: 需要管理员权限运行"))
        elif cmd == ID_LOG:
            h.open_log_dir()
        elif cmd == ID_EXIT:
            h.quit()
