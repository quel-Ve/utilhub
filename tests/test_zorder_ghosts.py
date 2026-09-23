"""幽灵窗过滤 + TOPMOST 舞步单测 — 2026-09-02 修复快照幽灵记录与恢复顺序错乱。

回归场景 (用户 2026-09-02 报告):
  1. 快照/预览图常驻幽灵窗: Sticky Notes / Realtek Audio Console /
     Microsoft Text Input Application (cloaked, IsWindowVisible=TRUE)。
  2. EXCLUDED_EXES 从未生效 — 旧 QueryFullProcessImageName(pid) 调用恒失败。
  3. 恢复顺序错乱 — HWND_TOP 抬升被前景窗静默拒绝, 须 TOPMOST 舞步重建。
"""
import os
import sys
from contextlib import ExitStack
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import win32con

from zorder import windows as zw
from zorder.windows import WinRecord


class FakeWin32:
    """win32gui 替身: hwnd → 属性表, EnumWindows 按 z 序顶→底回调。"""

    def __init__(self, wins):
        # wins: {hwnd: dict(visible, title, cls, pid, iconic, topmost, rect)}
        self.wins = wins
        self.order = list(wins)          # 枚举序 = z 序顶→底
        self.setwindowpos_calls = []
        self.showwindow_calls = []

    def EnumWindows(self, cb, _lp):
        for h in self.order:
            cb(h, 0)
        return True

    def IsWindowVisible(self, h):
        return self.wins[h]["visible"]

    def GetWindowTextLength(self, h):
        return len(self.wins[h]["title"])

    def GetWindowText(self, h):
        return self.wins[h]["title"]

    def GetClassName(self, h):
        return self.wins[h]["cls"]

    def IsIconic(self, h):
        return self.wins[h]["iconic"]

    def GetWindowLong(self, h, i):
        return win32con.WS_EX_TOPMOST if self.wins[h]["topmost"] else 0

    def GetWindowRect(self, h):
        return self.wins[h]["rect"]

    def SetWindowPos(self, h, after, x, y, w, ht, flags):
        self.setwindowpos_calls.append((h, after, flags))
        return True

    def ShowWindow(self, h, cmd):
        self.showwindow_calls.append((h, cmd))
        return True


def W(hwnd, pid, title="Doc", cls="Notepad", visible=True, iconic=False,
      topmost=False, rect=(0, 0, 800, 600), cloaked=False, exe="app.exe"):
    win = dict(visible=visible, title=title, cls=cls, pid=pid, iconic=iconic,
               topmost=topmost, rect=rect)
    return win, cloaked, exe


def patched(wins, own_pid=99999):
    """(fake, ctx) — wins: {hwnd: W(...)}; 进入 ctx 后 zorder.windows 全部指假。"""
    fake = FakeWin32({h: w for h, (w, _c, _e) in wins.items()})
    cloaked = {h for h, (_w, c, _e) in wins.items() if c}
    exes = {w["pid"]: e for w, _c, e in wins.values()}
    fakeproc = MagicMock()
    fakeproc.GetWindowThreadProcessId.side_effect = (
        lambda h: (0, fake.wins[h]["pid"]))
    stack = ExitStack()
    stack.enter_context(patch.object(zw, "win32gui", fake))
    stack.enter_context(patch.object(zw, "win32process", fakeproc))
    stack.enter_context(patch.object(zw, "_is_cloaked", lambda h: h in cloaked))
    stack.enter_context(patch.object(zw, "_win_exe", lambda pid: exes.get(pid, "")))
    stack.enter_context(patch.object(zw.os, "getpid", return_value=own_pid))
    return fake, stack


def test_capture_excludes_cloaked_ghost():
    wins = {
        1: W(1, 100, title="Sticky Notes", cls="ApplicationFrameWindow", cloaked=True),
        2: W(2, 200, title="Notepad"),
    }
    fake, stack = patched(wins)
    with stack:
        recs = zw.capture_windows()
    assert [r.title for r in recs] == ["Notepad"]


def test_capture_excludes_own_process_preview():
    wins = {
        1: W(1, 99999, title="pythonw", cls="Qt6111QWindowToolSaveBits"),
        2: W(2, 200, title="Notepad"),
    }
    fake, stack = patched(wins)
    with stack:
        recs = zw.capture_windows()
    assert [r.title for r in recs] == ["Notepad"]


def test_capture_excludes_inputhost_by_exe():
    wins = {
        1: W(1, 3148, title="Microsoft Text Input Application",
             cls="Windows.UI.Core.CoreWindow", exe="TextInputHost.exe"),
        2: W(2, 200, title="Notepad"),
    }
    fake, stack = patched(wins)
    with stack:
        recs = zw.capture_windows()
    assert [r.title for r in recs] == ["Notepad"]


def test_capture_record_fields_filled():
    wins = {5: W(5, 200, title="Obsidian", rect=(10, 20, 300, 400))}
    fake, stack = patched(wins)
    with stack:
        recs = zw.capture_windows()
    assert len(recs) == 1
    r = recs[0]
    assert (r.pid, r.title, r.cls, r.exe) == (200, "Obsidian", "Notepad", "app.exe")
    assert (r.left, r.top, r.width, r.height) == (10, 20, 290, 380)


def test_capture_keeps_zorder():
    wins = {1: W(1, 100, title="Top"), 2: W(2, 200, title="Mid"),
            3: W(3, 300, title="Bottom")}
    fake, stack = patched(wins)
    with stack:
        recs = zw.capture_windows()
    assert [r.title for r in recs] == ["Top", "Mid", "Bottom"]


def test_reapply_zorder_dance_bottom_to_top_twice():
    fake = FakeWin32({})
    with patch.object(zw, "win32gui", fake):
        zw._reapply_zorder([30, 20, 10])   # 底→顶
    calls = fake.setwindowpos_calls
    assert [c[0] for c in calls] == [30, 20, 10, 30, 20, 10]
    tops = [c[1] for c in calls]
    assert tops[:3] == [win32con.HWND_TOPMOST] * 3
    assert tops[3:] == [win32con.HWND_NOTOPMOST] * 3
    expected_flags = (win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                      | win32con.SWP_NOACTIVATE)
    assert all(c[2] == expected_flags for c in calls)


def test_reapply_zorder_empty_noop():
    fake = FakeWin32({})
    with patch.object(zw, "win32gui", fake):
        zw._reapply_zorder([])
    assert fake.setwindowpos_calls == []


def test_restore_skips_cloaked_match_old_slot():
    """旧快照幽灵记录: 匹配到 cloaked hwnd → 几何/舞步都不碰。"""
    wins = {1: W(1, 8172, title="Realtek Audio Console",
                 cls="ApplicationFrameWindow", cloaked=True)}
    fake, stack = patched(wins)
    rec = WinRecord(8172, "", "Realtek Audio Console",
                    "ApplicationFrameWindow", 0, 0, 100, 100)
    with stack:
        with patch.object(zw, "capture_candidates",
                          lambda: [zw.Candidate(1, 8172, "ApplicationFrameWindow",
                                                "Realtek Audio Console")]):
            with patch.object(zw, "minimize_strays") as ms:
                ok, matches = zw.restore_slot([rec])
    assert ok == 0
    assert matches == [1]
    assert fake.setwindowpos_calls == []          # 几何 + 舞步都没碰它
    assert fake.showwindow_calls == []
    ms.assert_called_once_with({1})


def test_restore_dance_covers_placed_windows_only():
    """两窗都恢复 → 舞步只收 placed hwnd, 顺序 = 快照底→顶。"""
    wins = {10: W(10, 100, title="TopWin"), 20: W(20, 200, title="BottomWin")}
    fake, stack = patched(wins)
    top = WinRecord(100, "app.exe", "TopWin", "Notepad", 0, 0, 800, 600)
    bottom = WinRecord(200, "app.exe", "BottomWin", "Notepad", 0, 0, 800, 600)
    with stack:
        with patch.object(zw, "capture_candidates",
                          lambda: [zw.Candidate(10, 100, "Notepad", "TopWin"),
                                   zw.Candidate(20, 200, "Notepad", "BottomWin")]):
            with patch.object(zw, "minimize_strays"):
                ok, matches = zw.restore_slot([top, bottom])
    assert ok == 2
    assert matches == [10, 20]
    seq = [c[0] for c in fake.setwindowpos_calls]
    # 几何两笔 (底先) + 舞步四笔 (TOPMOST 底→顶, NOTOPMOST 底→顶)
    assert seq == [20, 10, 20, 10, 20, 10]
    geo_flags = (win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                 | win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER)
    assert fake.setwindowpos_calls[0][2] == geo_flags
    assert fake.setwindowpos_calls[1][2] == geo_flags


def test_same_identity_windows_paired_by_geometry():
    """同 pid+cls+title 双窗 (主窗+伴生小窗): 按快照几何锚定, 不按当前 z 序乱配。

    2026-09-02 实测回归: ZCode 双窗在打乱后恢复, hwnd 互换 (可见结果错位)。
    """
    records = [
        WinRecord(100, "z.exe", "ZCode", "Chrome_WidgetWin_1", 0, 0, 1200, 800),
        WinRecord(100, "z.exe", "ZCode", "Chrome_WidgetWin_1", 32, 613, 324, 64),
    ]
    cands = [
        zw.Candidate(20, 100, "Chrome_WidgetWin_1", "ZCode", (32, 613, 324, 64)),
        zw.Candidate(10, 100, "Chrome_WidgetWin_1", "ZCode", (0, 0, 1200, 800)),
    ]
    assert zw.build_matches(records, cands) == [10, 20]


def test_same_identity_geometry_mismatch_falls_back_to_zorder():
    """几何都对不上 (用户挪过窗) → 回退当前 z 序首个, 与旧行为一致。"""
    records = [
        WinRecord(100, "z.exe", "ZCode", "Chrome_WidgetWin_1", 0, 0, 1200, 800),
        WinRecord(100, "z.exe", "ZCode", "Chrome_WidgetWin_1", 32, 613, 324, 64),
    ]
    cands = [
        zw.Candidate(20, 100, "Chrome_WidgetWin_1", "ZCode", (5, 5, 500, 500)),
        zw.Candidate(10, 100, "Chrome_WidgetWin_1", "ZCode", (7, 7, 700, 700)),
    ]
    assert zw.build_matches(records, cands) == [20, 10]


def test_candidates_without_rect_old_callers_still_work():
    """rect 缺省 None (旧调用方): 不触发几何锚定, 行为与 2026-08-25 版一致。"""
    records = [WinRecord(1, "a.exe", "A", "C", 0, 0, 100, 100)]
    cands = [zw.Candidate(10, 1, "C", "A")]
    assert zw.build_matches(records, cands) == [10]
