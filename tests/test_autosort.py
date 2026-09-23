"""TaskbarAutoSort 测试 — explorer 重启检测/节流/稳定探测/自愈排序编排 (2026-09-15)。

fake hub 提供 _sort_lock / _sort_core / _post / tray, 不碰真注入。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import autosort
from autosort import TaskbarAutoSort, tray_pid

# hub.py 运行时注入的兄弟项目路径 (taskbar_sorter 在 6TaskbarSortTool 下)
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "6TaskbarSortTool", "python"))


class FakeTray:
    def __init__(self):
        self.notes = []

    def notify(self, text):
        self.notes.append(text)


class FakeHub:
    def __init__(self, core_results=None):
        self._sort_lock = threading.Lock()
        self._core_results = list(core_results or [(True, "verified 3 groups, 0 moves, unmatched 0")])
        self.core_calls = 0
        self.tray = FakeTray()
        self.posted = []

    def _sort_core(self):
        self.core_calls += 1
        if self._core_results:
            return self._core_results.pop(0)
        return (False, "no scripted result")

    def _post(self, fn):
        self.posted.append(fn)


@pytest.fixture
def fast_gaps(monkeypatch):
    """把所有等待压到毫秒级。"""
    monkeypatch.setattr(autosort, "STABLE_GAP_S", 0.02)
    monkeypatch.setattr(autosort, "TRIGGER_DEDUP_S", 60.0)


# ── tray_pid ──────────────────────────────────────────────────────────

def test_tray_pid_returns_explorer_pid():
    pid = tray_pid()
    assert pid > 0  # 测试机 explorer 必在


# ── 节流 ──────────────────────────────────────────────────────────────

def test_trigger_dedup(fast_gaps):
    hub = FakeHub()
    auto = TaskbarAutoSort(hub, settle_wait_s=0.02)
    auto._trigger("广播")
    auto._trigger("轮询")  # 节流窗口内 → 不再 spawn
    assert auto._heal_thread is not None
    auto._heal_thread.join(timeout=5)
    assert hub.core_calls == 1


def test_trigger_after_window_rearms(fast_gaps, monkeypatch):
    hub = FakeHub()
    auto = TaskbarAutoSort(hub, settle_wait_s=0.02)
    auto._trigger("广播")
    auto._heal_thread.join(timeout=5)
    auto._last_trigger -= 120.0  # 手动拨回节流窗口外
    auto._trigger("二次重启")
    auto._heal_thread.join(timeout=5)
    assert hub.core_calls == 2


# ── _heal 编排 ────────────────────────────────────────────────────────

def test_heal_success_notifies(fast_gaps, monkeypatch):
    hub = FakeHub()
    auto = TaskbarAutoSort(hub, settle_wait_s=0.02)
    monkeypatch.setattr(auto, "_probe_count", lambda: 3)
    auto._heal()
    assert hub.core_calls == 1
    assert len(hub.posted) == 1
    hub.posted[0]()  # 执行 _post 的闭包
    assert "任务栏已自动恢复排序" in hub.tray.notes[0]


def test_heal_waits_for_stable_group_count(fast_gaps, monkeypatch):
    """组数不稳 (自启应用陆续上栏) → 继续探测, 稳定后才排序。"""
    hub = FakeHub()
    auto = TaskbarAutoSort(hub, settle_wait_s=0.02)
    counts = iter([2, 3, 3])
    monkeypatch.setattr(auto, "_probe_count", lambda: next(counts))
    auto._heal()
    assert hub.core_calls == 1  # 第 3 轮稳定后排序


def test_heal_gives_up_when_probe_fails(fast_gaps, monkeypatch):
    hub = FakeHub()
    auto = TaskbarAutoSort(hub, settle_wait_s=0.02)
    monkeypatch.setattr(auto, "_probe_count", lambda: None)
    auto._heal()
    assert hub.core_calls == 0  # 排序未执行


def test_heal_skips_when_manual_sort_running(fast_gaps, monkeypatch):
    hub = FakeHub()
    auto = TaskbarAutoSort(hub, settle_wait_s=0.02)
    monkeypatch.setattr(auto, "_probe_count", lambda: 3)
    assert hub._sort_lock.acquire()
    try:
        auto._heal()
    finally:
        hub._sort_lock.release()
    assert hub.core_calls == 0


def test_heal_failure_logs_without_notify(fast_gaps, monkeypatch):
    hub = FakeHub(core_results=[(False, "order mismatch at position 0")])
    auto = TaskbarAutoSort(hub, settle_wait_s=0.02)
    monkeypatch.setattr(auto, "_probe_count", lambda: 3)
    auto._heal()
    assert hub.core_calls == 1
    assert hub.tray.notes == []  # 失败不弹气泡


def test_heal_release_lock_on_probe_failure(fast_gaps, monkeypatch):
    """probe 失败提前 return 也必须释放 _sort_lock (否则手动排序永久被挡)。"""
    hub = FakeHub()
    auto = TaskbarAutoSort(hub, settle_wait_s=0.02)
    monkeypatch.setattr(auto, "_probe_count", lambda: None)
    auto._heal()
    assert hub._sort_lock.acquire(blocking=False)  # 锁已归还
    hub._sort_lock.release()
