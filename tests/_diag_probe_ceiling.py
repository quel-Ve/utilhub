"""探针: 前景窗玻璃天花板 — SWP_NOACTIVATE 能否把窗口抬到前景窗之上?
对照: 裸调 vs AttachThreadInput(前景线程) 后调。
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32con
import win32gui
import win32process


def zlist():
    out = []

    def cb(h, _lp):
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
                if not win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST:
                    out.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, 0)
    return out


def name(h):
    try:
        return f"{win32gui.GetWindowText(h)[:34]!r}"
    except Exception:
        return f"#{h}"


def raise_noact(h):
    win32gui.SetWindowPos(h, win32con.HWND_TOP, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                          | win32con.SWP_NOACTIVATE)


fg = win32gui.GetForegroundWindow()
print("当前前景窗:", name(fg), "hwnd=", fg)
zs = zlist()
# 取两个非前景、非置顶的可见窗: 一个当前在前景之上(第0个), 一个在下
targets = [h for h in zs if h != fg][:4]
if len(targets) < 2:
    print("可见窗太少")
    sys.exit(1)
victim = targets[-1]          # 最靠下的那个当实验对象
print("实验对象  :", name(victim))

before = zlist()
print("操作前序  :", [name(h) for h in before[:5]])

# --- 步骤1: 裸 NOACTIVATE 抬升 ---
raise_noact(victim)
after1 = zlist()
r1_top = after1.index(victim) < after1.index(fg) if fg in after1 else "?"
print(f"裸抬后位次={after1.index(victim)} 前景位次={after1.index(fg)} -> 在前景之上? {r1_top}")

# --- 步骤2: AttachThreadInput 后抬升 ---
me = ctypes.windll.kernel32.GetCurrentThreadId()
fg_thread = win32process.GetWindowThreadProcessId(fg)[0]
a1 = ctypes.windll.user32.AttachThreadInput(me, fg_thread, True)
print("AttachThreadInput:", bool(a1), "fg_thread=", fg_thread)
raise_noact(victim)
after2 = zlist()
r2_top = after2.index(victim) < after2.index(fg) if fg in after2 else "?"
print(f"附加后位次={after2.index(victim)} 前景位次={after2.index(fg)} -> 在前景之上? {r2_top}")
ctypes.windll.user32.AttachThreadInput(me, fg_thread, False)
print("已分离")
