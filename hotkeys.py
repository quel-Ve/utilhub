"""统一热键控制器 — 右 Alt 特化体系 (2026-08-14 触发规则定稿)。

热键表:
    右 Alt 按下          仅开启会话, 不弹面板
    右 Alt 按住 + , . /  槽 1/2/3: 短按 = 空槽快照/非空槽恢复; 长按 >=0.9s = 快照
    右 Alt 按住 + ;      面板 + 排序块效果 (arm); 松开 ; 执行排序 (防抖 0.5s)
    右 Alt 按住 + '      EQ 预设循环 (无防抖, 按下即切换)
    Ctrl 按住中          trigger 键与面板全部锁住 (hub 不参与组合热键)
    面板出现后           只有 Alt 松开才消失; trigger 松开面板保留
    Esc (右 Alt 按住中)  取消 hold + 隐藏预览 + 取消已 arm 的排序

粘性 hold: Windows AltGr 机制下右 Alt up 常先于槽位键 up 到达 — 右 Alt 松开
不终止 hold 会话, 槽位键 up 仍分派; 到点自动快照后释放不再分派。
排序同粘性: Alt 先松开只收面板, 已 arm 的排序仍等 ; 释放分派。
"""
import logging
import threading
import time

from pynput import keyboard

log = logging.getLogger("hub")

from zorder.decision import LONG_HOLD_MS

# 右 Alt 特化键位 (2026-08-13 用户设计): 右 Alt 按住 + 附近标点键
VK_COMMA, VK_PERIOD, VK_SLASH = 0xBC, 0xBE, 0xBF     # , . / = 槽 1/2/3
VK_SEMICOLON, VK_QUOTE = 0xBA, 0xDE                  # ; = 排序, ' = EQ 循环
SORT_DEBOUNCE_S = 0.5
_TICK_S = 0.05      # hold 监控节拍


class HotkeyController:
    """actions 接口 (全部在控制器线程回调):
        show_preview(slot) / hide_preview()
        set_hold(slot, hold_ms) / set_message(msg)
        snapshot(slot) / slot_action(slot) / sort_now() / sort_preview(on) / eq_cycle()
    """

    def __init__(self, actions, enabled=True):
        self.actions = actions
        self._enabled = enabled
        self._lock = threading.Lock()
        self._ralt_down = False            # 右 Alt 会话
        self._ralt_hold_slot = 0           # 按住中的槽 (1..3)
        self._ralt_hold_tick = 0.0
        self._ralt_hold_fired = False
        self._ctrl_down = 0                # 按住中的 Ctrl 键数 (锁 trigger/面板)
        self._sort_armed = False           # ; 按住中: 排序块效果
        self._sort_triggered = False       # 等 ; 释放分派排序
        self._last_sort = 0.0              # ; 释放防抖
        self._listener = None
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._last_restart = 0.0           # 监听器重启防抖 (自愈)
        self._last_mod_evt = time.monotonic()  # 最近修饰键事件 (自愈超时用)

    # ---------- 键判定 ----------

    @staticmethod
    def _is_right_alt(key):
        """仅右 Alt (含 AltGr)。"""
        if key == keyboard.Key.alt_r:
            return True
        if hasattr(keyboard.Key, "alt_gr") and key == keyboard.Key.alt_gr:
            return True
        return getattr(key, "vk", 0) == 0xA5  # VK_RMENU

    # ---------- hold 监控 (50ms 节拍线程) ----------

    def _monitor_loop(self):
        tick = 0
        while not self._stop.is_set():
            time.sleep(_TICK_S)
            tick += 1
            with self._lock:
                ralt_auto = None
                ralt_hold_elapsed = 0
                if self._ralt_hold_slot and not self._ralt_hold_fired:
                    ralt_hold_elapsed = (time.monotonic() - self._ralt_hold_tick) * 1000.0
                    if ralt_hold_elapsed >= LONG_HOLD_MS:
                        self._ralt_hold_fired = True
                        ralt_auto = self._ralt_hold_slot
                    elif not self._ralt_down and ralt_hold_elapsed > 2000:
                        # 粘性兜底: 右 Alt 已松 2s 仍无槽位释放 (事件丢失) → 取消
                        log.info("右Alt hold 会话超时取消 (槽 %d)", self._ralt_hold_slot)
                        self._ralt_hold_slot = 0
                        self._ralt_hold_fired = False
            if self._ralt_hold_slot and not self._ralt_hold_fired:
                self.actions.set_hold(self._ralt_hold_slot, int(ralt_hold_elapsed))
            if ralt_auto:
                log.info("右Alt长按 %.0fms → 自动快照槽 %d", ralt_hold_elapsed, ralt_auto)
                self.actions.snapshot(ralt_auto)
                self.actions.set_message("Snapshot done")
            if tick % 40 == 0:      # 每 2s 自愈检查 (防"进程活但热键死")
                self._health_check()

    # ---------- 自愈 (2026-08-16: 防"进程活但热键死") ----------

    def _reset_modifiers(self):
        """清空修饰键状态 (钩子重启/自愈时调用; 中断期间 release 事件已丢失)。"""
        self._ctrl_down = 0
        self._ralt_down = False
        self._ralt_hold_slot = 0
        self._ralt_hold_fired = False
        self._sort_armed = False
        self._sort_triggered = False

    def _restart_listener(self):
        """重建 pynput 监听器 (线程死亡后重装钩子)。"""
        if not self._enabled:
            return
        try:
            if self._listener:
                self._listener.stop()
        except Exception:
            log.exception("监听器停止失败 (忽略)")
        with self._lock:
            self._reset_modifiers()
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release)
        self._listener.start()
        log.warning("键盘监听器已重启")

    def _health_check(self):
        """2s 节拍自愈:
        1) 监听器线程死亡 → 重建; 2) 修饰键状态残留 (release 丢失) → 重置。
        """
        now = time.monotonic()
        if self._listener is not None and not self._listener.is_alive():
            if now - self._last_restart > 5.0:
                self._last_restart = now
                log.warning("检测到键盘监听器线程已退出, 自动重启")
                self._restart_listener()
        with self._lock:
            if (self._ctrl_down > 0 or self._ralt_down) and \
                    now - self._last_mod_evt > 10.0:
                log.warning("修饰键状态残留超时, 自愈重置 (ctrl=%d ralt=%s)",
                            self._ctrl_down, self._ralt_down)
                self._reset_modifiers()

    # ---------- pynput 回调 (钩子线程) ----------

    def _on_press(self, key):
        """回调免疫层: 任何异常只记日志, 绝不杀死 pynput 监听器。"""
        try:
            self._on_press_impl(key)
        except Exception:
            log.exception("热键按下回调异常 (已隔离)")

    def _on_press_impl(self, key):
        with self._lock:
            if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                self._ctrl_down += 1
                self._last_mod_evt = time.monotonic()
            if self._is_right_alt(key):
                self._ralt_down = True          # 只开会话, 不弹面板 (2026-08-14)
                self._last_mod_evt = time.monotonic()
            # Ctrl 按住中锁住整个体系: 不弹面板、trigger 不生效
            ralt_session = self._ralt_down and self._ctrl_down == 0
            ralt_vk = getattr(key, "vk", 0) if ralt_session else 0
            eq_now = False
            show_panel = False
            ralt_preview = 0
            sort_armed = False
            if ralt_vk in (VK_COMMA, VK_PERIOD, VK_SLASH):
                slot = {VK_COMMA: 1, VK_PERIOD: 2, VK_SLASH: 3}[ralt_vk]
                if self._ralt_hold_slot == 0:   # 首按 (repeat 不重置 tick)
                    self._ralt_hold_slot = slot
                    self._ralt_hold_tick = time.monotonic()
                    self._ralt_hold_fired = False
                    show_panel = True
                    ralt_preview = slot
            elif ralt_vk == VK_SEMICOLON:
                if not self._sort_armed:
                    self._sort_armed = True
                    self._sort_triggered = True  # 释放时分派 (2026-08-14)
                show_panel = True
                sort_armed = self._sort_armed
            elif ralt_vk == VK_QUOTE:
                eq_now = True   # 无防抖: 按下即切换 (2026-08-13)
                show_panel = True
            esc = key == keyboard.Key.esc
            cancel = esc and self._ralt_down
            if cancel:
                self._ralt_hold_slot = 0
                self._ralt_hold_fired = False
                self._sort_armed = False
                self._sort_triggered = False
        if show_panel:
            self.actions.show_preview(ralt_preview)
            if sort_armed:
                self.actions.sort_preview(True)
        if cancel:
            self.actions.hide_preview()
            self.actions.sort_preview(False)
        if eq_now:
            self.actions.eq_cycle()

    def _on_release(self, key):
        try:
            self._on_release_impl(key)
        except Exception:
            log.exception("热键释放回调异常 (已隔离)")

    def _on_release_impl(self, key):
        with self._lock:
            rvk = getattr(key, "vk", 0)
            ralt_release_slot = None
            ralt_fired = False
            if rvk in (VK_COMMA, VK_PERIOD, VK_SLASH) and self._ralt_hold_slot:
                slot2 = {VK_COMMA: 1, VK_PERIOD: 2, VK_SLASH: 3}[rvk]
                if slot2 == self._ralt_hold_slot:
                    ralt_release_slot = slot2
                    ralt_fired = self._ralt_hold_fired
                    self._ralt_hold_slot = 0
                    self._ralt_hold_fired = False
            sort_released = False
            sort_commit = False
            if rvk == VK_SEMICOLON and self._sort_triggered:
                self._sort_triggered = False
                self._sort_armed = False
                sort_released = True
                now = time.time()
                if now - self._last_sort >= SORT_DEBOUNCE_S:
                    self._last_sort = now
                    sort_commit = True
            if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                self._ctrl_down = max(0, self._ctrl_down - 1)
                self._last_mod_evt = time.monotonic()
            close = False
            if self._is_right_alt(key):
                # 粘性释放: 右 Alt up 只标记松开, hold/排序会话保留 (AltGr 事件顺序);
                # 面板无条件收起 (2026-08-14 用户规则: 只有 Alt 松开才消失)
                self._ralt_down = False
                self._last_mod_evt = time.monotonic()
                close = True
                self._sort_armed = False       # 效果随面板收; 已 arm 排序仍等 ; 释放
        if ralt_release_slot is not None and not ralt_fired:
            self.actions.slot_action(ralt_release_slot)  # 空槽快照 / 非空槽恢复
        if sort_released:
            self.actions.sort_preview(False)
            if sort_commit:
                self.actions.sort_now()
        if close:
            self.actions.hide_preview()
            self.actions.sort_preview(False)

    # ---------- 生命周期 ----------

    def start(self):
        if not self._enabled:
            return
        self._monitor.start()
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def stop(self):
        self._stop.set()
        if self._listener:
            self._listener.stop()
