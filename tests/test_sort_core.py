"""_sort_core 重试编排测试 (2026-09-15) — probe→sort 失败自动重试一轮。

不碰真注入: InjectorManager / ConfigManager / sort_windows_by_rules 全部
monkeypatch。Hub 用 __new__ 构造 (跳过 __init__ 的 Qt/热键/托盘)。
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "6TaskbarSortTool", "python"))

import hub
from taskbar_sorter.injector import InjectorManager


class FakeMgr:
    """可编程 probe/sort 结果序列。"""

    def __init__(self, probe_items, sort_results):
        self._probe_items = probe_items
        self._sort_results = list(sort_results)
        self.probe_calls = 0
        self.sort_calls = 0

    def probe(self):
        self.probe_calls += 1
        return {"items": self._probe_items}

    def sort(self, ordered):
        self.sort_calls += 1
        return self._sort_results.pop(0)


@pytest.fixture
def fresh_hub(monkeypatch):
    h = hub.Hub.__new__(hub.Hub)
    h._sort_lock = threading.Lock()
    return h


def _patch(monkeypatch, mgr):
    monkeypatch.setattr(hub, "InjectorManager", lambda: mgr)
    monkeypatch.setattr(hub, "ConfigManager", lambda: type("C", (), {
        "get_active_preset": staticmethod(lambda: {"rules": []})}))
    monkeypatch.setattr(hub, "sort_windows_by_rules", lambda items, rules: items)


def _items():
    return [{"title": "a", "process": "app.exe", "target_index": 0,
             "current_index": i, "hwnd": 0} for i in range(3)]


def test_sort_core_success_first_try(fresh_hub, monkeypatch):
    mgr = FakeMgr(_items(), [{"success": True, "message": "verified 3 groups"}])
    _patch(monkeypatch, mgr)
    ok, msg = fresh_hub._sort_core()
    assert ok and "verified 3 groups" in msg
    assert mgr.sort_calls == 1  # 不重试


def test_sort_core_retries_once_on_failure(fresh_hub, monkeypatch):
    """sort 失败 → 重新 probe + 重排 + 再 sort 一轮, 第二次成功。"""
    mgr = FakeMgr(_items(), [
        {"success": False, "error": "order mismatch at position 2"},
        {"success": True, "message": "verified 3 groups, 3 moves"},
    ])
    _patch(monkeypatch, mgr)
    ok, msg = fresh_hub._sort_core()
    assert ok
    assert mgr.probe_calls == 2 and mgr.sort_calls == 2  # 真的重新 probe 过


def test_sort_core_reports_after_second_failure(fresh_hub, monkeypatch):
    mgr = FakeMgr(_items(), [
        {"success": False, "error": "container changed during sort (3 -> 4)"},
        {"success": False, "error": "container changed during sort (4 -> 3)"},
    ])
    _patch(monkeypatch, mgr)
    ok, msg = fresh_hub._sort_core()
    assert not ok
    assert "container changed" in msg
    assert mgr.sort_calls == 2  # 只重试一轮, 不无限循环


def test_sort_core_probe_empty_raises_then_gives_up(fresh_hub, monkeypatch):
    """probe 恒空 → 两轮异常 → (False, 原因)。"""
    mgr = FakeMgr([], [])
    _patch(monkeypatch, mgr)
    ok, msg = fresh_hub._sort_core()
    assert not ok
    assert "没有任务栏分组" in msg


def test_sort_core_real_import_intact():
    """hub 命名空间里的 InjectorManager 仍是真实现 (monkeypatch 不外泄)。"""
    assert hub.InjectorManager is InjectorManager
