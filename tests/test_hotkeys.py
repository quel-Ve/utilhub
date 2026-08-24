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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pynput import keyboard

from hotkeys import (HotkeyController, VK_COMMA, VK_PERIOD, VK_SLASH,
                     VK_SEMICOLON, VK_QUOTE)

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
