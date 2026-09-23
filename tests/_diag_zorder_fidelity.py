"""实证: restore_slot 能否精确还原 z-order? 快照 → 打乱 → 恢复 → 对比.

不调用 minimize_strays (避免动用户桌面), 不调用 bring_foreground。
"""
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32con
import win32gui

from zorder import windows as zw

# 关掉清扫, 测试只关心顺序保真
zw.minimize_strays = lambda keep: None


def zorder_of(hwnds):
    """当前 z-order 里这些 hwnd 的顺序 (顶→底), 过滤已消失的。"""
    live = set(hwnds)
    out = []
    found = []

    def cb(h, _lp):
        found.append(h)
        return True

    win32gui.EnumWindows(cb, 0)
    for h in found:
        if h in live:
            out.append(h)
    return out


def shuffle(n_iter=1):
    """把现有可见窗口随机逐个置顶, 制造与快照不同的 z-order。"""
    hs = [h for h, *_ in live_visible()]
    random.shuffle(hs)
    for h in hs:
        try:
            win32gui.SetWindowPos(h, win32con.HWND_TOP, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                                  | win32con.SWP_NOACTIVATE)
        except Exception:
            pass


def live_visible():
    """可见+有标题+非置顶 (与 capture_windows 同过滤, 附 hwnd)。"""
    out = []

    def cb(h, _lp):
        try:
            if not win32gui.IsWindowVisible(h):
                return True
            t = win32gui.GetWindowText(h)
            if not t:
                return True
            if win32gui.IsIconic(h):
                return True
            if win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST:
                return True
            out.append((h, win32gui.GetClassName(h), t[:30]))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, 0)
    return out


def label(hwnd, cap):
    for h, cls, t in cap:
        if h == hwnd:
            return f"{t!r}"
    return f"hwnd#{hwnd}"


rounds = 3
for it in range(rounds):
    print(f"\n===== ROUND {it + 1} =====")
    records = zw.capture_windows()
    caps = live_visible()
    hwnd_by_idx = {i: h for i, (h, *_caps) in enumerate(
        sorted(zip([r for r in records],
                   [None] * len(records)), key=lambda x: 0))}  # placeholder
    # 快照记录没有 hwnd — 用匹配反查: 直接对 records 跑 build_matches 拿初始 hwnd 序
    orig_matches = zw.build_matches(records, zw.capture_candidates())
    orig_order = [m for m in orig_matches if m is not None]
    print("快照序(顶→底):", [label(m, caps) for m in orig_order])

    shuffle()
    after_shuffle = zorder_of(orig_order)
    print("打乱后    :", [label(m, caps) for m in after_shuffle])

    ok, matches = zw.restore_slot(records)
    restored = zorder_of(orig_order)
    print(f"恢复后 ok={ok}: ", [label(m, caps) for m in restored])

    if restored == orig_order:
        print(">>> PASS: 顺序与快照一致")
    else:
        print(">>> FAIL: 顺序不一致!")
        for a, b in zip(orig_order, restored):
            flag = "  ==" if a == b else "  !=  <<<< 差异"
            print(f"  {label(a, caps):<40} {label(b, caps):<40}{flag}")

print("\ndone")
