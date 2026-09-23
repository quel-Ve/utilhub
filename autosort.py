"""任务栏彩虹自愈 — explorer 重启后自动恢复排序 (2026-09-15)。

背景 (2026-09-15 诊断, 见当日排查): TaskbandHook 排序是 _MoveTaskBtnGroup
注入手术, 顺序不写 TaskbandStream 持久层 — explorer 一崩一重启, 任务栏必回
默认序 ("非彩虹"), LiveWallpaper2 挂在 WorkerW 后的壁纸层也随之消失
(壁纸进程本身零崩溃, 是 explorer 崩溃的连带症状)。本模块监听两类信号,
检测到 shell 重启即自动按当前活跃预设重排, 免去每次手动右Alt+;:

    ① WM_TASKBARCREATED 广播 (tray._wndproc 转发, explorer 重启必发)
    ② Shell_TrayWnd 属主 PID 轮询 (2s, 兜底: 广播错过/hub 晚启)

触发后流程: 节流 (30s 去重双路触发) → 等 SETTLE_WAIT_S 让任务栏结构落定
→ 组数稳定探测 (开机自启应用陆续挂图标, 连续两轮组数一致才动手, 最多 5 轮)
→ 排序 (复用 hub._sort_core, 内部失败已自动重试一轮) → 静音托盘气泡反馈。
全程独立 daemon 线程 + 持 _sort_lock 与手动排序互斥, 绝不阻塞主线程
(注入超时最长 ~60s, 主线程卡死会连累热键/托盘)。

诊断开关: config.json → diagnostic.disable_autosort (与既有二分禁用体系一致)。
"""
import ctypes
import logging
import threading
import time
from ctypes import wintypes

log = logging.getLogger("hub")

POLL_INTERVAL_S = 2.0     # PID 轮询节拍
TRIGGER_DEDUP_S = 30.0    # 双路触发 (广播/轮询) 去重窗口
SETTLE_WAIT_S = 8.0       # shell 重启后等任务栏基础结构落定
STABLE_GAP_S = 4.0        # 组数稳定探测间隔
STABLE_MAX_ROUNDS = 5     # 稳定探测最多轮数 (超时后尽力排序)

user32 = ctypes.WinDLL("user32", use_last_error=True)


def tray_pid():
    """Shell_TrayWnd 属主 PID (0 = 任务栏不存在, explorer 未起/已崩)。"""
    try:
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


class TaskbarAutoSort:
    """explorer 重启检测 → 自动排序。hub 提供 _sort_lock / _sort_core / _post / tray。"""

    def __init__(self, hub, settle_wait_s=SETTLE_WAIT_S):
        self.hub = hub
        self._settle_wait_s = settle_wait_s
        self._stop = threading.Event()
        self._last_trigger = 0.0            # monotonic, 节流闸
        self._heal_thread = None            # 当前 heal 线程 (测试可 join)
        self._poll = threading.Thread(target=self._poll_loop, daemon=True)

    def start(self):
        self._poll.start()

    def stop(self):
        self._stop.set()

    # ---------- 触发 ----------

    def notify_shell_restart(self):
        """托盘窗口收到 WM_TASKBARCREATED 时转发进来 (explorer 重启广播)。"""
        self._trigger("TaskbarCreated 广播")

    def _poll_loop(self):
        last = tray_pid()
        while not self._stop.wait(POLL_INTERVAL_S):
            cur = tray_pid()
            if cur and cur != last:
                last = cur
                self._trigger(f"Shell_TrayWnd PID 变更 → {cur}")
            elif cur:
                last = cur

    def _trigger(self, why):
        now = time.monotonic()
        if now - self._last_trigger < TRIGGER_DEDUP_S:
            log.info("任务栏自愈: %s (节流窗口内, 跳过)", why)
            return
        self._last_trigger = now
        log.info("任务栏自愈: %s → %.0fs 后稳定探测+自动排序", why,
                 self._settle_wait_s)
        self._heal_thread = threading.Thread(target=self._heal, daemon=True)
        self._heal_thread.start()

    # ---------- 执行 ----------

    def _probe_count(self):
        """当前任务栏组数 (注入 probe); None = 探测失败。"""
        try:
            from taskbar_sorter.injector import InjectorManager
            items = InjectorManager().probe().get("items", [])
            return len(items) if items else None
        except Exception as e:
            log.warning("任务栏自愈: probe 失败: %s", e)
            return None

    def _heal(self):
        """settle → 组数稳定探测 → 排序。全程持 _sort_lock (手动排序互斥)。"""
        self._stop.wait(self._settle_wait_s)
        if self._stop.is_set():
            return
        if not self.hub._sort_lock.acquire(blocking=False):
            log.info("任务栏自愈: 手动排序进行中, 跳过")
            return
        try:
            # 组数稳定探测: 自启应用陆续挂图标时避免漏组
            prev = self._probe_count()
            for _ in range(STABLE_MAX_ROUNDS):
                if prev is None:
                    log.warning("任务栏自愈: 任务栏探测不到分组, 放弃本次")
                    return
                self._stop.wait(STABLE_GAP_S)
                cur = self._probe_count()
                if cur is None:
                    log.warning("任务栏自愈: 探测中断, 放弃本次")
                    return
                if cur == prev:
                    break
                prev = cur
            ok, msg = self.hub._sort_core()
        finally:
            self.hub._sort_lock.release()
        if ok:
            log.info("任务栏自愈: 排序成功 (%s)", msg)
            self.hub._post(lambda m=msg: self.hub.tray.notify(
                f"任务栏已自动恢复排序 ({m})"))
        else:
            log.error("任务栏自愈: 排序失败 (%s)", msg)
