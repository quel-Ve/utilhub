"""快照/恢复按键时序决策 — zorder decision.cpp 的 Python 移植。

用户 2026-08-12 调整: LONG_HOLD_MS 1500 → 900 (原长按偏久)。
"""

QUICK_TAP_MS = 600    # <600ms = 快速点按
LONG_HOLD_MS = 900    # >=900ms = 长按快照; 600~899ms 为死区

# 动作: None / "snapshot" / "restore"
def decide_action(hold_ms, slot_has):
    if hold_ms < QUICK_TAP_MS:
        return "restore" if slot_has else "snapshot"
    if hold_ms < LONG_HOLD_MS:
        return None
    return "snapshot"
