#!/usr/bin/env python3
"""Utility Hub 崩溃看门狗 (crash watchdog)。

由 hub.py 在 --daemon 启动时以分离子进程拉起。打开 hub 进程句柄并阻塞等待其退出:
  - 退出码 0 (托盘"退出") 或存在 logs/hub.clean_exit 标记 → 静默退出
  - 其他 (0xc0000005 等硬崩溃 / taskkill /f) → 右下角 toast 提示

纯 ctypes + 标准库, 零第三方依赖。绝不 import hub.py (避免再拉 PyQt6/pynput,
否则看门狗本身也会踩进同一个崩溃点)。
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

# ---------- Win32: 进程等待 ----------
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INFINITE = 0xFFFFFFFF

kernel32 = ctypes.windll.kernel32
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

# ---------- Win32: 右下角 toast (移植自 21eq-switcher/toast.py, 同步运行) ----------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WM_TIMER = 0x0113
WM_DESTROY = 0x0002
WM_PAINT = 0x000F
SPI_GETWORKAREA = 0x0030
SW_SHOWNOACTIVATE = 4
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
PS_SOLID = 0
NULL_BRUSH = 5
DEFAULT_GUI_FONT = 17
TRANSPARENT = 1
ERROR_CLASS_ALREADY_EXISTS = 1410

BG_COLOR = 0x202020
BORDER_COLOR = 0x464646
FG_COLOR = 0xE8E8E8

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)
user32.DefWindowProcW.argtypes = (
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)
user32.DefWindowProcW.restype = LRESULT


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


_CLASS_NAME = "HubCrashToast"
_class_registered = False


@WNDPROC
def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_TIMER:
        user32.DestroyWindow(hwnd)
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    if msg == WM_PAINT:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        brush = gdi32.CreateSolidBrush(BG_COLOR)
        user32.FillRect(hdc, ctypes.byref(ps.rcPaint), brush)
        gdi32.DeleteObject(brush)
        pen = gdi32.CreatePen(PS_SOLID, 1, BORDER_COLOR)
        gdi32.SelectObject(hdc, pen)
        gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
        rc = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        gdi32.Rectangle(hdc, 0, 0, rc.right - 1, rc.bottom - 1)
        gdi32.DeleteObject(pen)
        gdi32.SelectObject(hdc, gdi32.GetStockObject(DEFAULT_GUI_FONT))
        gdi32.SetBkMode(hdc, TRANSPARENT)
        gdi32.SetTextColor(hdc, FG_COLOR)
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        user32.DrawTextW(hdc, buf, -1, ctypes.byref(rc), DT_VCENTER | DT_SINGLELINE)
        user32.EndPaint(hwnd, ctypes.byref(ps))
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_wnd_proc_ref = _wnd_proc  # 防 GC: 回调必须存活


def _ensure_class():
    global _class_registered
    if _class_registered:
        return
    wc = WNDCLASSW()
    wc.lpfnWndProc = _wnd_proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = _CLASS_NAME
    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = ctypes.get_last_error() or kernel32.GetLastError()
        if err != ERROR_CLASS_ALREADY_EXISTS:
            return
    _class_registered = True


def _measure_text(text):
    hdc = user32.GetDC(None)
    gdi32.SelectObject(hdc, gdi32.GetStockObject(DEFAULT_GUI_FONT))
    sz = SIZE()
    gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
    user32.ReleaseDC(None, hdc)
    return sz.cx


def _toast(text, duration_ms=6000):
    """右下角 toast, 同步运行在当前线程直到消失 (看门狗主线阻塞住, 保证显示完整)。"""
    _ensure_class()
    w = min(460, _measure_text(text) + 26)
    h = 40
    rc = wintypes.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rc), 0)
    x = rc.right - w - 12
    y = rc.bottom - h - 12
    hwnd = user32.CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
        _CLASS_NAME, text, WS_POPUP,
        x, y, w, h, None, None, kernel32.GetModuleHandleW(None), None,
    )
    if not hwnd:
        return
    rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, 10, 10)
    user32.SetWindowRgn(hwnd, rgn, True)
    user32.SetTimer(hwnd, 1, duration_ms, None)
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    user32.UpdateWindow(hwnd)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


TASK_NAME = "UtilityHub"
RESTART_COOLDOWN_S = 60  # 两次自动重启最小间隔, 防崩溃重启死循环


def _maybe_restart(root):
    """hub 硬崩溃后自动重启 (schtasks /run)。返回是否已重启。

    - HUB_WATCHDOG_NO_RESTART 环境变量存在 → 不重启 (允许用户关闭)
    - logs/hub.watchdog_restart 标记 < 60s → 不重启 (防抖)
    """
    if os.environ.get("HUB_WATCHDOG_NO_RESTART"):
        return False
    marker = os.path.join(root, "logs", "hub.watchdog_restart")
    try:
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as f:
                last = float(f.read().strip())
            if time.time() - last < RESTART_COOLDOWN_S:
                return False
    except (OSError, ValueError):
        pass
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    r = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                       capture_output=True)
    return r.returncode == 0


def main(argv):
    if len(argv) < 2:
        return 1
    try:
        pid = int(argv[1])
    except ValueError:
        return 1
    root = os.path.dirname(os.path.abspath(__file__))
    marker = os.path.join(root, "logs", "hub.clean_exit")

    # 打开 hub 进程句柄 (SYNCHRONIZE 用于等待; QUERY_LIMITED 取退出码)
    h = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not h:
        return 2  # hub 已不在 (启动竞态 / 已退出) — 静默

    # 阻塞直到 hub 退出
    kernel32.WaitForSingleObject(h, INFINITE)
    code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
        code.value = 0
    kernel32.CloseHandle(h)

    # 0 = 托盘"退出"正常退出; 标记 = uninstall.bat 主动 taskkill /f
    if code.value == 0 or os.path.exists(marker):
        return 0

    restarted = _maybe_restart(root)
    msg = "Utility Hub 已闪退 (0x%08X)" % code.value
    if restarted:
        msg += " — 已自动重启"
    _toast(msg, 6000)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
