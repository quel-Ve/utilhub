"""build_matches 顺序感知合一匹配测试 — 2026-08-25 修复恢复顺序随机。

回归场景: 同应用多窗口 (同 pid+cls) 时旧 find_window_by_pid_class 在 >=2 个窗口
返回 None 全部跳过; 同标题窗口 find_window_by_pid 首个匹配歧义 → 恢复顺序随机
(用户 2026-08-25 报告 "restore good but not in order")。build_matches 保证合一:
每记录至多一个 HWND、每个候选 HWND 至多分配一次, 结果确定。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from zorder.windows import build_matches, Candidate
from zorder.windows import WinRecord


def R(pid, title, cls="Chrome_WidgetWin_1"):
    return WinRecord(pid, "app.exe", title, cls, 0, 0, 100, 100)


def C(hwnd, pid, title, cls="Chrome_WidgetWin_1"):
    return Candidate(hwnd, pid, cls, title)


def test_single_window_exact():
    assert build_matches([R(1, "A")], [C(10, 1, "A")]) == [10]


def test_no_such_pid_returns_none():
    assert build_matches([R(9, "Ghost")], [C(10, 1, "A")]) == [None]


def test_multi_app_distinct_pids_current_order_irrelevant():
    records = [R(1, "Notepad"), R(2, "Chrome")]
    cands = [C(10, 2, "Chrome"), C(20, 1, "Notepad")]   # 当前 z 序颠倒无所谓
    assert build_matches(records, cands) == [20, 10]


def test_same_app_two_windows_order_preserved():
    records = [R(1, "Tab1"), R(1, "Tab2")]
    cands = [C(10, 1, "Tab1"), C(20, 1, "Tab2")]
    assert build_matches(records, cands) == [10, 20]


def test_same_app_titles_swapped_still_one_to_one():
    records = [R(1, "Tab1"), R(1, "Tab2")]
    cands = [C(10, 1, "Tab2"), C(20, 1, "Tab1")]        # 标题互换
    assert build_matches(records, cands) == [20, 10]


def test_same_app_identical_titles_zorder_pairing():
    records = [R(1, "Same"), R(1, "Same")]
    cands = [C(10, 1, "Same"), C(20, 1, "Same")]
    # 顶记录→顶候选, 次记录→次候选; 每个 HWND 只分配一次
    assert build_matches(records, cands) == [10, 20]


def test_window_added_stays_unmatched():
    records = [R(1, "Tab1")]
    cands = [C(10, 1, "Tab1"), C(20, 1, "NewWindow")]
    assert build_matches(records, cands) == [10]        # 新窗口留给 stray 最小化


def test_window_removed_returns_none():
    records = [R(1, "Tab1"), R(1, "Tab2")]
    cands = [C(10, 1, "Tab1")]
    assert build_matches(records, cands) == [10, None]


def test_all_titles_changed_fallback_by_zorder():
    records = [R(1, "Old1"), R(1, "Old2")]
    cands = [C(10, 1, "New1"), C(20, 1, "New2")]
    assert build_matches(records, cands) == [10, 20]


def test_exact_identity_wins_over_fallback():
    # 顶记录标题已变 → 回退; 但次记录精确 "Keep" 必须不被顶记录抢走
    records = [R(1, "Gone"), R(1, "Keep")]
    cands = [C(10, 1, "Keep"), C(20, 1, "Other")]
    assert build_matches(records, cands) == [20, 10]


def test_empty_inputs():
    assert build_matches([], []) == []
    assert build_matches([R(1, "A")], []) == [None]
