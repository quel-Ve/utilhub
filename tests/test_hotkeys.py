"""HotkeyController 状态机测试 — 2026-08-14 触发规则。

    仅右 Alt 按下        不弹面板 (只开会话)
    trigger 键按下       面板才出现
    Ctrl 按住中          trigger 与面板全部锁住
    ; 按下               arm 排序 (面板 + 排序效果), 松开 ; 才执行排序
    Alt 松开             无条件收面板
    Esc                  取消 (含已 arm 的排序)
    trigger 松开          面板保留
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pynput import keyboard

from hotkeys import (HotkeyController, VK_COMMA, VK_PERIOD, VK_SLASH,
                     VK_SEMICOLON, VK_QUOTE, VK_ESC)

ALT_R = keyboard.Key.alt_r
CTRL_L = keyboard.Key.ctrl_l
CTRL_R = keyboard.Key.ctrl_r
ESC = keyboard.Key.esc


def key_vk(vk):
    return keyboard.KeyCode.from_vk(vk)


class Recorder:
    """记录 actions 调用的 stub (监控线程不启动, 只测回调路径)。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _f(*a):
            self.calls.append((name,) + a)
        return _f


def make():
    rec = Recorder()
    return HotkeyController(rec), rec


def test_alt_alone_no_panel():
    c, rec = make()
    c._on_press(ALT_R)
    assert not rec.calls, rec.calls


def test_slot_trigger_shows_panel_and_action_on_release():
    c, rec = make()
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_COMMA))
    assert ("show_preview", 1) in rec.calls
    c._on_release(key_vk(VK_COMMA))
    assert ("slot_action", 1) in rec.calls
    # trigger 松开面板保留
    assert not any(x[0] == "hide_preview" for x in rec.calls)


def test_alt_release_hides_panel_unconditionally():
    c, rec = make()
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_COMMA))
    c._on_release(ALT_R)          # hold 未结束时 Alt 先松 → 也收面板
    assert ("hide_preview",) in rec.calls
    c._on_release(key_vk(VK_COMMA))
    assert ("slot_action", 1) in rec.calls   # 粘性: 槽位释放仍分派


def test_ctrl_blocks_trigger_and_panel():
    c, rec = make()
    c._on_press(CTRL_R)
    c._on_press(ALT_R)
    assert not rec.calls
    c._on_press(key_vk(VK_COMMA))
    assert not rec.calls, rec.calls
    c._on_release(key_vk(VK_COMMA))
    assert not rec.calls
    # Ctrl 松开后同会话恢复
    c._on_release(CTRL_R)
    c._on_press(key_vk(VK_PERIOD))
    assert ("show_preview", 2) in rec.calls
    assert ("slot_action", 2) not in rec.calls


def test_semicolon_arms_preview_sorts_on_release():
    c, rec = make()
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_SEMICOLON))
    assert ("show_preview", 0) in rec.calls
    assert ("sort_preview", True) in rec.calls
    assert not any(x[0] == "sort_now" for x in rec.calls)   # 按下不排序
    c._on_release(key_vk(VK_SEMICOLON))
    assert ("sort_now",) in rec.calls
    assert ("sort_preview", False) in rec.calls


def test_alt_release_before_semicolon_release_still_sorts():
    c, rec = make()
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_SEMICOLON))
    c._on_release(ALT_R)          # Alt 先松: 收面板, 排序仍等 ; 释放
    assert ("hide_preview",) in rec.calls
    assert not any(x[0] == "sort_now" for x in rec.calls)
    c._on_release(key_vk(VK_SEMICOLON))
    assert ("sort_now",) in rec.calls


def test_esc_cancels_armed_sort():
    c, rec = make()
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_SEMICOLON))
    c._on_press(ESC)
    assert ("hide_preview",) in rec.calls
    c._on_release(key_vk(VK_SEMICOLON))
    assert not any(x[0] == "sort_now" for x in rec.calls)


def test_quote_shows_panel_and_cycles_eq_on_press():
    c, rec = make()
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_QUOTE))
    assert ("show_preview", 0) in rec.calls
    assert ("eq_cycle",) in rec.calls


def test_ctrl_blocks_semicolon_sort():
    c, rec = make()
    c._on_press(CTRL_L)
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_SEMICOLON))
    assert not rec.calls, rec.calls
    c._on_release(key_vk(VK_SEMICOLON))
    assert not any(x[0] == "sort_now" for x in rec.calls)


# ---------- 2026-08-26: Ctrl 锁修正 (游戏长按 / 幻影 Ctrl / AltGr) ----------

def test_ctrl_repeat_flood_capped():
    """游戏长按 Ctrl 的 repeat 洪水不应把计数冲到几百 (08-18 ctrl=532 事故)。"""
    c, rec = make()
    for _ in range(10):
        c._on_press(CTRL_L)
    assert c._ctrl_down == 2, c._ctrl_down


def test_ctrl_long_hold_does_not_lock():
    """游戏按住 Ctrl 几秒后按右 Alt + 槽位 → 应生效 (长按不算 AltGr)。"""
    c, rec = make()
    c._on_press(CTRL_L)                      # Ctrl 按下 (长按开始)
    c._ctrl_down_at = time.monotonic() - 5   # 已按住 5s (repeat 洪水被限幅)
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_COMMA))
    assert ("show_preview", 1) in rec.calls, rec.calls


def test_phantom_ctrl_does_not_lock():
    """Ctrl 释放事件丢失后计数残留 (幻影 Ctrl) → 右 Alt 不应被锁。"""
    c, rec = make()
    c._ctrl_down = 1                           # 残留: 释放丢失
    c._ctrl_down_at = time.monotonic() - 60    # 首按在 60s 前 (非 AltGr)
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_SEMICOLON))
    assert ("show_preview", 0) in rec.calls
    c._on_release(key_vk(VK_SEMICOLON))
    assert ("sort_now",) in rec.calls


def test_ctrl_press_within_window_is_altgr():
    """Ctrl 与右 Alt 同时按下 (<0.3s) = AltGr 组合 → 槽位键锁住。"""
    c, rec = make()
    c._on_press(CTRL_L)
    c._on_press(ALT_R)                         # 紧接 Ctrl: 视为 AltGr
    c._on_press(key_vk(VK_COMMA))
    assert not rec.calls, rec.calls


# ---------- 2026-08-26: F24 心跳自测 (钩子静默失效自愈) ----------

def test_f24_heartbeat_restarts_on_silence(monkeypatch):
    """钩子静默失效 (线程活但无事件) → 自测注入 F24 无响应 → 自动重启。"""
    import hotkeys as _hk
    c, rec = make()
    c._listener = object()                     # 非 None, 走重启路径
    restarts = []
    monkeypatch.setattr("ctypes.windll.user32.keybd_event", lambda *a: None)
    monkeypatch.setattr(c, "_restart_listener", lambda: restarts.append(1))
    monkeypatch.setattr(_hk, "SELF_TEST_RESULT_WAIT_S", 0.001)
    c._last_key_evt = time.monotonic() - 60    # 长时间无任何键盘事件
    c._self_test_hook()
    assert restarts == [1]


def test_f24_heartbeat_keeps_alive_hook(monkeypatch):
    """钩子活着: 自测注入后收到事件 → 不重启。"""
    import hotkeys as _hk
    c, rec = make()
    restarts = []
    monkeypatch.setattr(c, "_restart_listener", lambda: restarts.append(1))
    monkeypatch.setattr(_hk, "SELF_TEST_RESULT_WAIT_S", 0.001)

    def fake_inject(vk, scan, flags, extra):
        c._last_key_evt = time.monotonic()     # 模拟钩子收到 F24
    monkeypatch.setattr("ctypes.windll.user32.keybd_event", fake_inject)
    c._last_key_evt = time.monotonic() - 60
    c._self_test_hook()
    assert restarts == []


def test_f24_heartbeat_disabled_noop(monkeypatch):
    """diagnostic 禁用热键时不注入自测。"""
    c, rec = make()
    c._enabled = False
    injected = []
    monkeypatch.setattr("ctypes.windll.user32.keybd_event",
                        lambda *a: injected.append(1))
    c._self_test_hook()
    assert injected == []


# ---------- 2026-09-16: 会话中吞键 (win32_event_filter) ----------

class _Data:
    """KBDLLHOOKSTRUCT 最小替身 (filter 只读 vkCode)。"""
    def __init__(self, vk):
        self.vkCode = vk


class _SuppressListener:
    """pynput Listener 替身: suppress_event 记录而非抛异常。"""
    def __init__(self):
        self.suppressed = []

    def suppress_event(self):
        self.suppressed.append(True)


WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105


def make_filtered():
    c, rec = make()
    c._listener = _SuppressListener()
    return c, rec


def test_filter_suppresses_slot_key_in_session():
    """会话中 , 按下/释放 → 正常分派 + 系统级吞键 (不再漏给前台应用)。"""
    c, rec = make_filtered()
    c._on_press(ALT_R)
    c._win32_event_filter(WM_KEYDOWN, _Data(VK_COMMA))
    assert ("show_preview", 1) in rec.calls
    assert c._listener.suppressed == [True]
    c._win32_event_filter(WM_KEYUP, _Data(VK_COMMA))
    assert ("slot_action", 1) in rec.calls
    assert c._listener.suppressed == [True, True]
    assert not any(x[0] == "hide_preview" for x in rec.calls)


def test_filter_suppresses_semicolon_sort():
    """会话中 ; 按下/释放 → arm + 排序照常, 且全部吞除。"""
    c, rec = make_filtered()
    c._on_press(ALT_R)
    c._win32_event_filter(WM_SYSKEYDOWN, _Data(VK_SEMICOLON))
    assert ("sort_preview", True) in rec.calls
    c._win32_event_filter(WM_SYSKEYUP, _Data(VK_SEMICOLON))
    assert ("sort_now",) in rec.calls
    assert len(c._listener.suppressed) == 2


def test_filter_passes_without_session():
    """无右 Alt 会话 → 不吞不分派 (用户正常打字)。"""
    c, rec = make_filtered()
    c._win32_event_filter(WM_KEYDOWN, _Data(VK_COMMA))
    assert not rec.calls
    assert c._listener.suppressed == []


def test_filter_passes_non_hub_keys_in_session():
    """会话中按非 hub 键 (如 w) → 放行, 应用菜单加速键不受影响。"""
    c, rec = make_filtered()
    c._on_press(ALT_R)
    c._win32_event_filter(WM_KEYDOWN, _Data(0x57))
    assert not rec.calls
    assert c._listener.suppressed == []


def test_filter_ctrl_lock_does_not_suppress():
    """AltGr 输入 (Ctrl<0.3s + 右 Alt) → 不吞, 字符正常到达应用。"""
    c, rec = make_filtered()
    c._on_press(CTRL_L)
    c._on_press(ALT_R)
    c._win32_event_filter(WM_KEYDOWN, _Data(VK_COMMA))
    assert not rec.calls
    assert c._listener.suppressed == []


def test_filter_esc_suppressed_and_cancels():
    """会话中 Esc → 吞掉 (防 Alt+Esc 系统切窗) + 取消会话。"""
    c, rec = make_filtered()
    c._on_press(ALT_R)
    c._on_press(key_vk(VK_SEMICOLON))          # arm
    c._win32_event_filter(WM_KEYDOWN, _Data(VK_ESC))
    assert ("hide_preview",) in rec.calls
    assert c._listener.suppressed == [True]
    c._win32_event_filter(WM_KEYUP, _Data(VK_SEMICOLON))
    assert not any(x[0] == "sort_now" for x in rec.calls)   # 已取消


def test_filter_quote_cycles_eq():
    """会话中 ' → EQ 循环照常分派 + 吞除。"""
    c, rec = make_filtered()
    c._on_press(ALT_R)
    c._win32_event_filter(WM_KEYDOWN, _Data(VK_QUOTE))
    assert ("eq_cycle",) in rec.calls
    assert c._listener.suppressed == [True]


def test_filter_sticky_semicolon_release_after_alt():
    """粘性 (AltGr 顺序): Alt 先松后 ; 释放 → keyup 仍吞除并分派排序
    (旧行为: "已 arm 的排序仍等 ; 释放分派")。"""
    c, rec = make_filtered()
    c._on_press(ALT_R)
    c._win32_event_filter(WM_KEYDOWN, _Data(VK_SEMICOLON))
    c._on_release(ALT_R)
    c._win32_event_filter(WM_KEYUP, _Data(VK_SEMICOLON))
    assert ("sort_now",) in rec.calls
    assert len(c._listener.suppressed) == 2


def test_filter_sticky_slot_release_after_alt():
    """粘性: Alt 先松后 , 释放 → 空槽快照仍分派。"""
    c, rec = make_filtered()
    c._on_press(ALT_R)
    c._win32_event_filter(WM_KEYDOWN, _Data(VK_PERIOD))
    c._on_release(ALT_R)
    assert ("hide_preview",) in rec.calls
    c._win32_event_filter(WM_KEYUP, _Data(VK_PERIOD))
    assert ("slot_action", 2) in rec.calls
    assert len(c._listener.suppressed) == 2


def test_filter_keyup_passes_untracked_key():
    """无粘性状态 (如 AltGr 输入 ; 的 keyup) → 放行, 应用收到完整键序。"""
    c, rec = make_filtered()
    c._on_press(ALT_R)                    # 会话开着但从未按 ;
    c._win32_event_filter(WM_KEYUP, _Data(VK_SEMICOLON))
    assert not rec.calls
    assert c._listener.suppressed == []
