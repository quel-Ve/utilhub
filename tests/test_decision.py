"""decide_action 边界测试 — 移植 zorder tests/main.cpp test_decision()。

三段时间语义: <600ms 快速点按; 600~899ms 死区; >=900ms 长按快照。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from zorder.decision import decide_action, QUICK_TAP_MS, LONG_HOLD_MS


def test_quick_tap_restore():
    assert decide_action(0, True) == "restore"
    assert decide_action(599, True) == "restore"


def test_quick_tap_snapshot_when_empty():
    assert decide_action(0, False) == "snapshot"
    assert decide_action(599, False) == "snapshot"


def test_dead_zone():
    assert decide_action(600, True) is None
    assert decide_action(899, True) is None
    assert decide_action(600, False) is None
    assert decide_action(899, False) is None


def test_long_hold_snapshot():
    assert decide_action(900, True) == "snapshot"
    assert decide_action(900, False) == "snapshot"
    assert decide_action(5000, True) == "snapshot"


def test_constants():
    assert QUICK_TAP_MS == 600
    assert LONG_HOLD_MS == 900  # 用户调整: 原 1500
