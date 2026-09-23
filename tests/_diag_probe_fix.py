"""探针2: 三种精确重建 z-order 的方案对照 + 前景窗沉降许可。

T1: SetWindowPos(fg, HWND_BOTTOM, NOACTIVATE) 是否被允许 (前景窗沉降)
T2: 沉降后 insert-after 链 (top→bottom) 能否精确重建序 (含 fg 归位)
T3: TOPMOST 舞步 (升 bottom→top, 降 bottom→top) 能否精确重建序
最后把原始顺序复原。
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32con
import win32gui
import win32process

SWP = (win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)


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
        return win32gui.GetWindowText(h)[:26]
    except Exception:
        return f"#{h}"


def place(h, after):
    win32gui.SetWindowPos(h, after, 0, 0, 0, 0, SWP)


def zmatch(expected):
    """按 expected 顺序过滤当前还活着的窗口, 与 zlist 对齐。"""
    cur = zlist()
    return [h for h in expected if h in cur]


orig = zlist()
fg = win32gui.GetForegroundWindow()
print("前景:", name(fg))
print("原始序:", [name(h) for h in orig[:6]], "...")

# ---------- T1: 沉降前景窗 ----------
try:
    place(fg, win32con.HWND_BOTTOM)
    after = zlist()
    t1 = after.index(fg) == len(after) - 1 if fg in after else False
    print(f"T1 沉降前景到底层: {'允许' if t1 else '被拒绝'} (fg 现位次={after.index(fg) if fg in after else '?'}/{len(after)})")
except Exception as e:
    t1 = False
    print("T1 沉降前景: 异常", e)

# ---------- T2: insert-after 链重建原始序 ----------
def chain(order):
    """order 顶→底: 第一个 HWND_TOP, 其后逐个放到前一个正下方。"""
    prev = None
    for h in order:
        if prev is None:
            place(h, win32con.HWND_TOP)
        else:
            place(h, prev)   # 插到 prev 正下方
        prev = h


if t1:
    chain(orig)
    got = zmatch(orig)
    t2 = got == orig
    print("T2 沉降+链重建:", "PASS ✓" if t2 else "FAIL ✗")
    if not t2:
        for a, b in zip(orig, got):
            print("   ", name(a), "=>", name(b), "OK" if a == b else "<<<<")

    # 链重建后 fg 沉在底 — 复原: fg 抬回原位次 (用链放回 orig 里它的位置)
    idx = orig.index(fg)
    above = orig[idx - 1] if idx > 0 else None
    place(fg, win32con.HWND_TOP if above is None else above)
    got2 = zmatch(orig)
    print("T2b fg 归位:", "PASS ✓" if got2 == orig else f"FAIL ✗ {got2 == orig}")
else:
    print("T2 跳过 (T1 失败)")
    # fg 在底了也要捞回来
    place(fg, win32con.HWND_TOP)

# ---------- T3: TOPMOST 舞步 ----------
# 升: bottom→top 逐个 HWND_TOPMOST; 降: bottom→top 逐个 HWND_NOTOPMOST
for h in reversed(orig):
    try:
        win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0, SWP)
    except Exception:
        pass
for h in reversed(orig):
    try:
        win32gui.SetWindowPos(h, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, SWP)
    except Exception:
        pass
got3 = zmatch(orig)
t3 = got3 == orig
print("T3 TOPMOST 舞步重建:", "PASS ✓" if t3 else "FAIL ✗")
if not t3:
    for a, b in zip(orig, got3):
        print("   ", name(a), "=>", name(b), "OK" if a == b else "<<<<")

print("\n最终序 vs 原始序:", "一致 ✓" if zmatch(orig) == orig else "不一致 ✗")
