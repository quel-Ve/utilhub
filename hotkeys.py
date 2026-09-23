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

Ctrl 锁规则 (2026-08-26 修正): 原"Ctrl 按住即锁"在游戏长按 Ctrl (repeat
洪水 + 释放丢失) 时把 _ctrl_down 冲到几百 → 右 Alt 全部失效 (08-18 自愈触发
17 次, ctrl=532)。改为 **Ctrl 首按 <0.3s 才判为 AltGr 组合锁**, 长按 (游戏/编辑)
不锁 → 打游戏时右 Alt 照常可用。另: _ctrl_down 限幅 2 防膨胀。

热键自愈 (2026-08-26): 原 _health_check 只查监听器线程 is_alive, 查不到
"线程活但钩子静默失效" (22.5h 长跑后热键全死、无任何日志)。新增 F24 心跳
自测: 每 5 分钟注入 F24 (全系统无绑定键) → 钩子 0.5s 内无响应即自动重启。
用户离开 (系统空闲 >4min) 时跳过注入: keybd_event 会重置系统空闲计时器,
已证实会让 SleepAtMidnight 整夜测不到空闲 → PC 通宵不睡 (2026-09-15 事故)。

按键吞除 (2026-09-16): pynput Listener 默认被动观察, 右 Alt 会话中的槽位/
排序/EQ 键原样漏给前台应用 → Windows 视作 Alt+未注册键, DefWindowProc 调
MessageBeep() 弹系统默认提示音 (用户感知为"每次用热键多一声错误音")。
新增 win32_event_filter: 会话中 (Ctrl 锁除外) 吞掉 , . / ; ' Esc。
pynput 1.8.x 语义: filter 返回 False 只跳过本地回调、事件仍传给应用;
self.suppress_event() 抛 SuppressException 才是系统级吞键, 但会中断消息
post → 本地回调不再触发。故分派在 filter 内直接调 _on_press_impl/
_on_release_impl 完成后再 suppress — 每个事件只走 filter/callbacks 一条
路径, 不双派。
"""
import ctypes
import logging
import threading
import time

from pynput import keyboard

log = logging.getLogger("hub")

from zorder.decision import LONG_HOLD_MS

# 右 Alt 特化键位 (2026-08-13 用户设计): 右 Alt 按住 + 附近标点键
VK_COMMA, VK_PERIOD, VK_SLASH = 0xBC, 0xBE, 0xBF     # , . / = 槽 1/2/3
VK_SEMICOLON, VK_QUOTE = 0xBA, 0xDE                  # ; = 排序, ' = EQ 循环
VK_F24 = 0x87                                        # 自测心跳键 (全系统无绑定)
VK_ESC = 0x1B                                        # 取消 (会话中须吞: Alt+Esc 是系统切窗)
CTRL_LOCK_WINDOW_S = 0.3                             # Ctrl 首按 <0.3s = AltGr 锁
SELF_TEST_TICKS = 6000                               # 每 6000 tick(50ms)=5min 自测
SELF_TEST_RESULT_WAIT_S = 0.5                        # 自测注入后等钩子响应
SELF_TEST_MAX_IDLE_S = 240                           # 空闲>4min 跳过注入 (防重置系统空闲计时)
SORT_DEBOUNCE_S = 0.5
_TICK_S = 0.05      # hold 监控节拍

# --- 按键吞除 (2026-09-16, 见模块 docstring) ---
_WM_KEYDOWN, _WM_KEYUP = 0x0100, 0x0101
_WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x0104, 0x0105
# 会话中吞除的 vk → 直接分派用的 pynput key 对象
_SESSION_VK_KEYS = {
    VK_COMMA: keyboard.KeyCode.from_vk(VK_COMMA),
    VK_PERIOD: keyboard.KeyCode.from_vk(VK_PERIOD),
    VK_SLASH: keyboard.KeyCode.from_vk(VK_SLASH),
    VK_SEMICOLON: keyboard.KeyCode.from_vk(VK_SEMICOLON),
    VK_QUOTE: keyboard.KeyCode.from_vk(VK_QUOTE),
    VK_ESC: keyboard.Key.esc,
}


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
        self._ctrl_down_at = 0.0           # Ctrl 0→1 (首按) 时刻 — AltGr 判别
        self._sort_armed = False           # ; 按住中: 排序块效果
        self._sort_triggered = False       # 等 ; 释放分派排序
        self._last_sort = 0.0              # ; 释放防抖
        self._listener = None
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._last_restart = 0.0           # 监听器重启防抖 (自愈)
        self._last_mod_evt = time.monotonic()  # 最近修饰键事件 (自愈超时用)
        self._last_key_evt = time.monotonic()  # 任意键事件 (F24 心跳自测用)

    # ---------- 键判定 ----------

    @staticmethod
    def _is_right_alt(key):
        """仅右 Alt (含 AltGr)。"""
        if key == keyboard.Key.alt_r:
            return True
        if hasattr(keyboard.Key, "alt_gr") and key == keyboard.Key.alt_gr:
            return True
        return getattr(key, "vk", 0) == 0xA5  # VK_RMENU

    def _ctrl_locked(self):
        """Ctrl 首按 <0.3s = AltGr 组合锁 (与 _on_press_impl 同判据)。"""
        return (self._ctrl_down > 0
                and time.monotonic() - self._ctrl_down_at < CTRL_LOCK_WINDOW_S)

    # ---------- 按键吞除 (win32_event_filter, 2026-09-16) ----------

    def _win32_event_filter(self, msg, data):
        """右 Alt 会话中吞掉 hub 键位, 不让 Alt+X 泄漏给前台应用
        (Alt+未注册键 → WM_SYSCHAR → DefWindowProc MessageBeep → 系统默认提示音)。

        pynput 1.8.x: 返回 False 只跳过本地回调 (事件仍传给应用);
        suppress_event() 抛 SuppressException → 钩子返回 1 → 系统级吞键,
        但中断消息 post → on_press/on_release 不触发。故先在 filter 内
        直接分派再 suppress; 未吞的事件照常走 callbacks, 不双派。

        分派判定:
        - keydown: 会话中且非 AltGr 锁 (松开 Alt 后的新按下不属于 hub);
        - keyup: 只看粘性跟踪状态 (_ralt_hold_slot / _sort_triggered) —
          AltGr 事件顺序下 Alt up 常先于槽位键 up, 此时分派必须照常
          (旧行为: "槽位键 up 仍分派"); 无跟踪 = 用户自己的键, 放行。
        """
        key = _SESSION_VK_KEYS.get(data.vkCode)
        if key is None:
            return
        vk = data.vkCode
        if msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
            dispatch = self._ralt_down and not self._ctrl_locked()
        else:
            dispatch = ((vk == VK_SEMICOLON and self._sort_triggered)
                        or (vk in (VK_COMMA, VK_PERIOD, VK_SLASH)
                            and self._ralt_hold_slot))
        if not dispatch:
            return                       # 非 hub 的键 → 放行 (AltGr 输入/正常打字)
        self._last_key_evt = time.monotonic()
        try:
            if msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                self._on_press_impl(key)
            else:
                self._on_release_impl(key)
        except Exception:
            log.exception("吞键分派异常 (已隔离)")
        if self._listener is not None:
            self._listener.suppress_event()   # 抛异常 → 系统级吞键

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
            if tick % SELF_TEST_TICKS == 0:     # 每 5min F24 心跳自测 (钩子静默失效)
                self._self_test_hook()

    # ---------- 自愈 (2026-08-16: 防"进程活但热键死") ----------

    def _reset_modifiers(self):
        """清空修饰键状态 (钩子重启/自愈时调用; 中断期间 release 事件已丢失)。"""
        self._ctrl_down = 0
        self._ctrl_down_at = 0.0
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
            on_press=self._on_press, on_release=self._on_release,
            win32_event_filter=self._win32_event_filter)
        self._listener.start()
        log.warning("键盘监听器已重启")

    @staticmethod
    def _user_idle_seconds():
        """系统空闲秒数 (GetLastInputInfo, 含注入输入)。读不到 = 按 0 (活跃)。"""
        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0

    def _self_test_hook(self):
        """F24 心跳自测 (2026-08-26): 注入 F24 (全系统无绑定键) → 监听器应收到
        并更新 _last_key_evt。0.5s 内无响应 = 钩子静默失效 (线程活着但钩子死,
        is_alive() 查不到) → 自动重启监听器。覆盖 22.5h 长跑后热键全死的形态。
        用户离开时跳过: 注入会重置系统空闲计时, 整夜通宵不睡 (2026-09-15)。"""
        if not self._enabled:
            return
        if self._user_idle_seconds() > SELF_TEST_MAX_IDLE_S:
            return
        before = self._last_key_evt
        try:
            ctypes.windll.user32.keybd_event(VK_F24, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_F24, 0, 2, 0)  # KEYEVENTF_KEYUP
        except Exception as e:
            log.warning("热键自测注入失败 (跳过): %s", e)
            return
        time.sleep(SELF_TEST_RESULT_WAIT_S)
        if self._last_key_evt > before:
            return                                   # 钩子活着
        now = time.monotonic()
        if now - self._last_restart <= 5.0:
            return
        self._last_restart = now
        log.warning("键盘钩子自测无响应 (静默失效), 自动重启")
        self._restart_listener()

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
        self._last_key_evt = time.monotonic()       # F24 心跳自测信号
        try:
            self._on_press_impl(key)
        except Exception:
            log.exception("热键按下回调异常 (已隔离)")

    def _on_press_impl(self, key):
        with self._lock:
            if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                if self._ctrl_down == 0:
                    self._ctrl_down_at = time.monotonic()   # 记录首按时刻
                self._ctrl_down = min(self._ctrl_down + 1, 2)  # 防 repeat 洪水膨胀
                self._last_mod_evt = time.monotonic()
            if self._is_right_alt(key):
                self._ralt_down = True          # 只开会话, 不弹面板 (2026-08-14)
                self._last_mod_evt = time.monotonic()
            # AltGr 判别 (2026-08-26): Ctrl 首按 <0.3s = 组合键 → 锁; 长按(游戏)
            # 不锁 → 打游戏按住 Ctrl 时右 Alt 热键照常可用
            ctrl_locked = self._ctrl_locked()
            ralt_session = self._ralt_down and not ctrl_locked
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
        self._last_key_evt = time.monotonic()       # F24 心跳自测信号
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
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release,
            win32_event_filter=self._win32_event_filter)
        self._listener.start()

    def stop(self):
        self._stop.set()
        if self._listener:
            self._listener.stop()
