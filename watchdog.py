#!/usr/bin/env python3
"""托盘双进程看门狗 (utilhub + win2blur)。

由 hub.py 在 --daemon 启动时以分离子进程拉起 (watchdog.py <hub_pid>)；也可独立
注册为 onlogon 计划任务运行 (watchdog.py 无参数，按 logs/hub.pid 找 hub)。

保证 "utilhub 和 win2blur 永远在托盘" (2026-08-25 用户要求)：
  1. 目标托盘窗口消失 (进程死/崩) → schtasks /run /tn <task> 拉起
  2. 目标托盘窗口存在但不响应 SendMessageTimeout(WM_NULL) 超时 = 主线程挂起
     → taskkill /f 后 schtasks /run 重启
  3. utilhub 干净退出 (托盘"退出"已写 logs/hub.clean_exit 标记) → 不拉起，
     尊重用户主动退出 (标记只消费一次)

为什么用"托盘窗口"而不是进程存在性判断：
  - 托盘窗口 = 用户可见的服务面；窗口没了等于托盘图标没了 (进程死窗口必销毁)
  - SendMessageTimeout(WM_NULL) 超时 = 主线程消息泵停摆 = 进程活着但"死了"，
    这正是 2026-08-25 现场 (CPU=0、热键/托盘全无响应、taskkill 却 Access denied
    因为它是提权进程) 需要兜住的形态。

看门狗与目标同为提权进程 (hub 拉起继承提权 / 独立任务 RunLevel=highest)，
跨完整性 SendMessageTimeout 与 taskkill 均可行。

单实例 Mutex Local\\UtilityHubWatchdog (hub 每次启动都会拉起一个，去重)。
纯 ctypes + 标准库，零第三方依赖。绝不 import hub.py (避免再拉 PyQt6/pynput)。
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

# ---------- Win32 常量 ----------
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INFINITE = 0xFFFFFFFF
WM_NULL = 0
SMTO_ABORTIFHUNG = 0x2
ERROR_ALREADY_EXISTS = 183

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

# WaitForSingleObject 返回值
WAIT_OBJECT_0 = 0x0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                       ctypes.POINTER(wintypes.DWORD)]

# ---------- 配置 ----------
ROOT = os.path.dirname(os.path.abspath(__file__))
HUB_PID_FILE = os.path.join(ROOT, "logs", "hub.pid")
HUB_CLEAN_EXIT = os.path.join(ROOT, "logs", "hub.clean_exit")
HUB_TRAY_CLASS = "HubTrayMsgWindow"
HUB_TASK = "UtilityHub"
W2B_TRAY_CLASS = "win2blurTray"
W2B_TASK = "win2blur"
LOOP_INTERVAL_S = 5          # 轮询周期
HANG_TIMEOUT_MS = 3000       # WM_NULL 超时 = 判定主线程挂起
STARTUP_GRACE_S = 12         # 启动宽限: hub 刚 spawn 看门狗时托盘窗口可能还没建
HUB_STARTUP_GRACE_S = 120    # hub 启动宽限: pid 文件写于 main() 开头, mtime 即启动时刻;
                             # 启动期主线程忙初始化 (import/EQ init), 窗口暂时无响应 ≠ 挂死
                             # — 2026-09-18 启动慢被误杀, 34s 周期 9 连崩
RESTART_COOLDOWN_S = 30      # 每个目标独立防抖, 防重启死循环
WATCHDOG_MUTEX = "Local\\UtilityHubWatchdog"


def _wlog(msg):
    """看门狗动作日志 — toast 转瞬即逝, 文件才是事后归因的证据 (logs/watchdog.log)。"""
    try:
        with open(os.path.join(ROOT, "logs", "watchdog.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _hub_in_startup_grace() -> bool:
    try:
        return (time.time() - os.path.getmtime(HUB_PID_FILE)) < HUB_STARTUP_GRACE_S
    except OSError:
        return False


# ---------- 右下角 toast (移植自 21eq-switcher/toast.py) ----------
user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
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
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)
gdi32 = ctypes.windll.gdi32


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


def _toast(text, duration_ms=5000):
    """右下角 toast, 同步运行在当前线程直到消失。"""
    _ensure_class()
    w = min(520, _measure_text(text) + 26)
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


# ---------- 单实例 ----------

_MUTEX_HANDLE = None  # 全局持有, 防 GC 关闭句柄释放互斥量


def _single_instance() -> bool:
    """独占看门狗 Mutex; 已有在跑则返回 False。

    bInitialOwner=True 保证首个实例真正持有。旧实例若崩死, 内核对象仍在 →
    CreateMutexW 仍报 ERROR_ALREADY_EXISTS, 但 WaitForSingleObject 返回
    WAIT_ABANDONED → 接管 (否则看门狗死后新实例会误判"已有在跑"而退出,
    看门狗就永远消失了)。返回 True 前必须全局持有句柄 (防 GC)。
    """
    global _MUTEX_HANDLE
    _MUTEX_HANDLE = kernel32.CreateMutexW(None, True, WATCHDOG_MUTEX)
    if not _MUTEX_HANDLE:
        return False
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return True  # 首次创建并已持有
    r = kernel32.WaitForSingleObject(_MUTEX_HANDLE, 0)
    if r == WAIT_TIMEOUT:
        # 被活动进程持有 → 已有看门狗在跑
        kernel32.CloseHandle(_MUTEX_HANDLE)
        _MUTEX_HANDLE = None
        return False
    return True  # WAIT_OBJECT_0 (恰好释放) 或 WAIT_ABANDONED (前主人崩死, 已接管)


# ---------- 探活 ----------

def _window_hwnd(class_name):
    return user32.FindWindowW(class_name, None)


def _window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _window_responsive(hwnd) -> bool:
    """SendMessageTimeout WM_NULL: 主线程消息泵活着才算活。"""
    out = wintypes.DWORD()
    r = user32.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0,
                                   SMTO_ABORTIFHUNG, HANG_TIMEOUT_MS,
                                   ctypes.byref(out))
    return r != 0


STILL_ACTIVE = 259


def _pid_alive(pid) -> bool:
    """真存活判断。

    不能只用 OpenProcess: 刚被 taskkill 杀死的进程对象会因外部句柄残留在内核,
    OpenProcess 仍返回句柄 → 误判活着 (2026-08-25 实战: 看门狗因此不重启死 hub)。
    必须 GetExitCodeProcess: 运行中 = STILL_ACTIVE(259), 已终止 = 实际退出码。
    """
    if not pid or pid <= 0:
        return False
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    code = wintypes.DWORD()
    ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    kernel32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


# ---------- 重启 (带防抖) ----------

def _restart(task_name, key) -> bool:
    """schtasks /run 拉起目标; 每个目标 30s 内不重复重启 (防死循环)。

    ⚠ 必须 CREATE_NO_WINDOW: 看门狗是 pythonw 无窗进程, 但 schtasks.exe /
    taskkill.exe 是 console 程序, 不带此标志 Windows 会给它们分配**可见**
    控制台窗口 → 用户屏幕上每轮闪一个黑框 (2026-09-23 实测 rect=52,52
    993x519 vis=1 class=ConsoleWindowClass; 目标反复退出时循环闪)。
    同仓库 edgedock_host.kill_standalone 与 edge-dock 的 mesh provider 早
    就这么做了, 这里此前漏了。
    """
    marker = os.path.join(ROOT, "logs", key + ".restart")
    try:
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as f:
                last = float(f.read().strip())
            if time.time() - last < RESTART_COOLDOWN_S:
                _wlog("%s 重启防抖 (<%ds), 跳过" % (task_name, RESTART_COOLDOWN_S))
                return False
    except (OSError, ValueError):
        pass
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    r = subprocess.run(["schtasks", "/run", "/tn", task_name], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    _wlog("schtasks /run %s → rc=%d" % (task_name, r.returncode))
    return r.returncode == 0


def _kill_pid(pid):
    subprocess.run(["taskkill", "/f", "/pid", str(pid)], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)


# ---------- 目标检查 ----------

def _check_hub():
    """utilhub: 窗口消失(死/退) 或窗口不响应(挂起) → 处理。"""
    hwnd = _window_hwnd(HUB_TRAY_CLASS)
    if hwnd:
        if not _window_responsive(hwnd):
            if _hub_in_startup_grace():
                # 启动期主线程忙初始化 (import/EQ init), 窗口已建但事件循环未跑,
                # 暂时无响应 ≠ 挂死 — 2026-09-18 曾被误杀, 34s 周期 9 连崩
                _wlog("hub 无响应但处于启动宽限期 (<%ds), 跳过本次"
                      % HUB_STARTUP_GRACE_S)
                return
            _wlog("hub 无响应 (主线程挂起, WM_NULL %dms 超时) → taskkill + 重启"
                  % HANG_TIMEOUT_MS)
            _toast("Utility Hub 无响应 (主线程挂起)，正在重启…")
            _kill_pid(_window_pid(hwnd))
            _restart(HUB_TASK, "hub")
        return
    # 窗口不存在: 进程可能崩了 / 正常退了 / 还在启动
    pid = 0
    try:
        with open(HUB_PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pass
    if _pid_alive(pid):
        return  # hub 进程还活着, 只是托盘窗口还没建 (启动中) → 等
    if os.path.exists(HUB_CLEAN_EXIT):
        # 干净退出 (托盘"退出"), 尊重用户; 消费标记防下次误判
        try:
            os.remove(HUB_CLEAN_EXIT)
        except OSError:
            pass
        _wlog("hub 干净退出 (clean_exit 标记), 不拉起")
        return
    _wlog("hub 窗口消失 + 进程已死 + 无干净退出标记 → toast + 重启 "
          "(注: 无此日志的死亡=外部 taskkill; 配合 hub.log aboutToQuit 判归因)")
    _toast("Utility Hub 已退出 (异常)，正在重启…")
    _restart(HUB_TASK, "hub")


def _check_win2blur():
    """win2blur: 窗口消失(死) 或窗口不响应(挂起) → 重启 (无干净退出概念)。"""
    hwnd = _window_hwnd(W2B_TRAY_CLASS)
    if hwnd:
        if not _window_responsive(hwnd):
            _toast("win2blur 无响应 (主线程挂起)，正在重启…")
            _kill_pid(_window_pid(hwnd))
            _restart(W2B_TASK, "w2b")
        return
    _toast("win2blur 已退出，正在重启…")
    _restart(W2B_TASK, "w2b")


def main(argv):
    if not _single_instance():
        return 0  # 已有看门狗在跑 (hub 拉起 / onlogon 任务并存时去重)
    # 写自身 pid 供 uninstall.bat 停止 (与 hub.pid 同构)
    try:
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "watchdog.pid"), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    # 启动宽限: hub 刚 spawn 看门狗时托盘窗口可能尚未创建 (Hub.__init__ 后才建)
    time.sleep(STARTUP_GRACE_S)
    while True:
        try:
            _check_hub()
        except Exception:
            pass
        try:
            _check_win2blur()
        except Exception:
            pass
        time.sleep(LOOP_INTERVAL_S)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
