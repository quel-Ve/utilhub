"""诊断: 枚举全部顶层窗口 — 可见/cloaked/置顶/类名/exe, 找幽灵窗口成因."""
import ctypes
import os
from ctypes import wintypes

import win32con
import win32gui
import win32process
import win32api

DWMA_CLOAKED = 14
CLOAKED_APP = 1
CLOAKED_SHELL = 2
CLOAKED_INHERITED = 4

_dwm = ctypes.windll.dwmapi


def is_cloaked(h):
    val = wintypes.DWORD(0)
    try:
        r = _dwm.DwmGetWindowAttribute(wintypes.HWND(h), DWMA_CLOAKED,
                                       ctypes.byref(val), ctypes.sizeof(val))
        return r == 0 and val.value != 0, val.value
    except Exception:
        return False, -1


def get_exe(pid):
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = win32api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return f"<OpenProcess fail err={win32api.GetLastError()}>"
        try:
            name = win32process.QueryFullProcessImageName(h, 0)
            return os.path.basename(name)
        finally:
            win32api.CloseHandle(h)
    except Exception as e:
        return f"<err {e}>"


rows = []


def cb(h, _lp):
    try:
        visible = win32gui.IsWindowVisible(h)
        title = win32gui.GetWindowText(h)
        cls = win32gui.GetClassName(h)
        pid = win32process.GetWindowThreadProcessId(h)[1]
        exstyle = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
        iconic = win32gui.IsIconic(h)
        cloaked, cloakkind = is_cloaked(h)
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
        except Exception:
            l = t = r = b = 0
        rows.append((h, visible, iconic, bool(exstyle & win32con.WS_EX_TOPMOST),
                     cloaked, cloakkind, cls, title[:44], pid, get_exe(pid),
                     (l, t, r - l, b - t)))
    except Exception as e:
        rows.append((h, "?", "?", "?", "?", "?", "?", f"<enum err {e}>", 0, "?", None))
    return True


win32gui.EnumWindows(cb, 0)
print(f"{'hwnd':>8} vis icon topm clak kind class({' ':<31}) title({' ':<42}) pid exe")
for (h, vis, iconic, topm, cloaked, kind, cls, title, pid, exe, rect) in rows:
    mark = " <<< GHOST?" if (vis and not cloaked and not title) or (vis and cloaked and title) else ""
    print(f"{h:>8} {int(vis):>3} {int(iconic):>4} {int(topm):>4} {int(cloaked):>4} {kind:>4} "
          f"{cls:<36} {title:<48} {pid:>6} {exe}{mark} rect={rect}")

print("\n--- 汇总: 可见+有标题+非cloaked (会被快照捕获的) ---")
for (h, vis, iconic, topm, cloaked, kind, cls, title, pid, exe, rect) in rows:
    if vis and title and not iconic and not topm and not cloaked:
        print(f"  {cls:<36} {title:<48} {exe}")

print("\n--- 汇总: 可见但 cloaked (当前过滤器漏进来的幽灵) ---")
for (h, vis, iconic, topm, cloaked, kind, cls, title, pid, exe, rect) in rows:
    if vis and cloaked:
        print(f"  kind={kind} {cls:<36} {title:<48} {exe}")

print("\n--- pythonw / python (hub 侧) 进程的窗口 ---")
for (h, vis, iconic, topm, cloaked, kind, cls, title, pid, exe, rect) in rows:
    if "python" in exe.lower():
        print(f"  hwnd={h} vis={vis} topm={topm} cloaked={cloaked} cls={cls!r} "
              f"title={title!r} pid={pid} rect={rect}")
