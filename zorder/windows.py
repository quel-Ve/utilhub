"""窗口采集/恢复 — zorder snapshot.cpp/restore.cpp 的 Python 移植 (win32gui)。

采集顺序 = EnumWindows z-order 顶→底; 恢复 = 逆序置 TOPMOST 再解除, 还原快照顺序。
"""
import os
import win32con
import win32gui
import win32process
from dataclasses import dataclass

EXCLUDED_CLASSES = frozenset([
    "Progman",              # 桌面
    "Shell_TrayWnd",        # 任务栏
    "WorkerW",              # 壁纸 worker
    "ZOS_Preview",          # 自身预览窗 (类名保持兼容)
    "DDE Server Window",    # 系统 DDE 消息隐藏窗
])

# 常驻系统进程窗口: 快照/恢复/清扫三路全忽略 (2026-08-14 用户确认 —
# 始终运行且不受 zorder 控制, 忽略后也不会遮挡预览图下方信息)
EXCLUDED_EXES = frozenset([
    "TextInputHost.exe",    # Microsoft Text Input Software (触控键盘宿主)
])


def _is_excluded_exe(exe):
    return exe in EXCLUDED_EXES


@dataclass
class WinRecord:
    pid: int
    exe: str
    title: str
    cls: str
    left: int
    top: int
    width: int
    height: int


def is_excluded_class(cls):
    return cls in EXCLUDED_CLASSES


def should_capture_by_meta(visible, has_title, iconic, topmost, cls):
    if not visible or not has_title or iconic or topmost:
        return False
    return not is_excluded_class(cls)


def _win_title(h):
    """GetWindowText 在受保护窗口上会抛 UIPI Access denied (error 5)。
    回调路径必须永不抛出, 失败当无标题处理。"""
    try:
        n = win32gui.GetWindowTextLength(h)
        if n <= 0:
            return ""
        return win32gui.GetWindowText(h)  # 按实际长度读取, 无 512 截断问题
    except Exception:
        return ""


def _win_class(h):
    try:
        return win32gui.GetClassName(h)
    except Exception:
        return ""


def _win_pid(h):
    try:
        return win32process.GetWindowThreadProcessId(h)[1]
    except Exception:
        return 0


def _win_exe(pid):
    try:
        name, _ = win32process.QueryFullProcessImageName(pid, 0)
        return os.path.basename(name)
    except Exception:
        return ""


def capture_windows():
    """返回当前可见顶层窗口列表 (z-order 顶→底), 应用与 C++ 相同的过滤。"""
    out = []

    def cb(h, _lp):
        try:
            if not win32gui.IsWindowVisible(h):
                return True
            title = _win_title(h)
            if not title:
                return True
            if win32gui.IsIconic(h):
                return True
            if win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST:
                return True
            cls = _win_class(h)
            if is_excluded_class(cls):
                return True
            pid = _win_pid(h)
            if _is_excluded_exe(_win_exe(pid)):
                return True
            left, top, right, bottom = win32gui.GetWindowRect(h)
            out.append(WinRecord(pid, _win_exe(pid), title, cls,
                                 left, top, right - left, bottom - top))
        except Exception:
            pass  # 受保护窗口 (UIPI) 或已销毁窗口: 跳过, 绝不中断枚举
        return True

    win32gui.EnumWindows(cb, 0)
    return out


def record_matches(rec, pid, cls, title):
    return rec.pid == pid and rec.cls == cls and rec.title == title


def find_window_by_pid(pid, cls, title):
    """精确匹配 pid+cls+title 的窗口; 无则 None。回调绝不抛出 (UIPI 保护窗口跳过)。"""
    result = [None]

    def cb(h, _lp):
        if result[0] is not None:
            return False
        if _win_pid(h) != pid:
            return True
        if _win_class(h) != cls:
            return True
        if _win_title(h) != title:
            return True
        result[0] = h
        return False

    try:
        win32gui.EnumWindows(cb, 0)
    except Exception:
        pass
    return result[0]


def find_window_by_pid_class(pid, cls):
    """降级匹配: 同 pid+同 class 恰好一个时返回, 多个返回 None (无法安全判定)。"""
    found = []

    def cb(h, _lp):
        if _win_pid(h) == pid and _win_class(h) == cls:
            found.append(h)
        return True

    try:
        win32gui.EnumWindows(cb, 0)
    except Exception:
        pass
    return found[0] if len(found) == 1 else None


def match_window(rec):
    """两级匹配: 精确 pid+cls+title → 降级 pid+cls (标题变化兜底)。"""
    h = find_window_by_pid(rec.pid, rec.cls, rec.title)
    if not h:
        h = find_window_by_pid_class(rec.pid, rec.cls)
    return h


def restore_slot(windows):
    """恢复快照窗口几何+顺序, 返回成功恢复数。

    文件顺序 z-order 顶→底, 逆序 HWND_TOP 处理: 底→顶逐个置到非置顶带顶,
    最终顶序与快照一致。

    **不用 TOPMOST→NOTOPMOST 舞步** (zorder C++ 原做法): 该做法会临时把窗口
    抬进置顶带, 打乱 window2clear 等非置顶叠加层与目标的相对 z-order, 导致
    恢复后蒙版错位悬在窗口前面。HWND_TOP 只作用于非置顶带, 叠加层相对位置
    不受影响 (其 33ms tick 自愈检测也能正常工作)。
    位置/尺寸未变的窗口跳过 SetWindowPos, 减少逐个闪烁与叠加层重锚抖动。
    """
    ok = 0
    for rec in reversed(windows):
        try:
            if _is_excluded_exe(rec.exe):
                # 旧快照可能含常驻系统窗 (TextInputHost): 跳过, 不参与恢复
                continue
            h = match_window(rec)
            if not h:
                continue
            # 最小化窗口先还原 (最小化窗口无位置, SetWindowPos 位置参数会被忽略)。
            # 用 SW_SHOWNOACTIVATE 而非 SW_RESTORE: SW_RESTORE 会激活窗口 → 抬到最上层并
            # 抢前台, 本该在底层的窗口因此"概率性"冒到最上层 (激活/还原竞态, 2026-08-15)。
            if win32gui.IsIconic(h):
                win32gui.ShowWindow(h, win32con.SW_SHOWNOACTIVATE)
            left, top, right, bottom = win32gui.GetWindowRect(h)
            if (left, top, right - left, bottom - top) == (rec.left, rec.top,
                                                           rec.width, rec.height):
                flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
            else:
                flags = win32con.SWP_SHOWWINDOW | win32con.SWP_NOACTIVATE
            win32gui.SetWindowPos(h, win32con.HWND_TOP, rec.left, rec.top,
                                  rec.width, rec.height, flags)
            ok += 1
        except Exception:
            # 单个窗口失败绝不中断整体恢复 (窗口中途销毁/受保护等)
            continue
    # 2026-08-14: 恢复后清扫 — 当前可见但不在快照中的窗口最小化 (窗口越积越多问题)
    minimize_strays(windows)
    return ok


def minimize_strays(snapshot_windows):
    """恢复后清扫: 当前可见但不在快照里的窗口 → 最小化。

    用户需求 (2026-08-14): 目标布局未提到的最小化窗口若正开着, 恢复不会动它,
    窗口越积越多。区分两类: 该打开的 (快照内, 保留) / 不该打开但已打开的
    (当前可见且不在快照, 最小化)。

    过滤与 capture 同规则 (可见/有标题/非最小化/非置顶/非排除类/非排除 exe),
    再加: 快照中匹配到的窗口 (含两级匹配) 保留。绝不最小化常驻系统窗
    (TextInputHost 等) 与自身预览窗。
    """
    keep = set()
    for rec in snapshot_windows:
        h = match_window(rec)
        if h:
            keep.add(h)

    def cb(h, _lp):
        try:
            if h in keep:
                return True
            if not win32gui.IsWindowVisible(h):
                return True
            if win32gui.IsIconic(h):
                return True
            if not _win_title(h):
                return True
            if win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST:
                return True
            if is_excluded_class(_win_class(h)):
                return True
            if _is_excluded_exe(_win_exe(_win_pid(h))):
                return True
            win32gui.ShowWindow(h, win32con.SW_MINIMIZE)
        except Exception:
            pass  # 受保护/已销毁窗口: 跳过, 绝不中断
        return True

    win32gui.EnumWindows(cb, 0)


def bring_foreground(windows, foreground_pid):
    """快照前台窗口恢复到前台 (绕过前台锁: AttachThreadInput + ShowWindow + SetForegroundWindow)。"""
    for rec in windows:
        if rec.pid != foreground_pid:
            continue
        h = find_window_by_pid(rec.pid, rec.cls, rec.title)
        if not h:
            return False
        try:
            fg = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[0]
        except Exception:
            fg = 0
        me = win32api_get_current_thread_id()
        try:
            target = win32process.GetWindowThreadProcessId(h)[0]
            win32process.AttachThreadInput(me, fg, True)
            win32process.AttachThreadInput(me, target, True)
            try:
                if win32gui.IsIconic(h):
                    win32gui.ShowWindow(h, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(h)
            finally:
                win32process.AttachThreadInput(me, fg, False)
                win32process.AttachThreadInput(me, target, False)
            return True
        except Exception:
            # UIPI 拒绝 (error 5) 等: 放弃该窗口的前台恢复, 不中断
            return False
    return False


def win32api_get_current_thread_id():
    import win32api
    return win32api.GetCurrentThreadId()
