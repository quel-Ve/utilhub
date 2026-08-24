"""slots JSON 严格 schema 测试 — 移植 zorder tests/main.cpp test_json()。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from zorder.slots import parse_slot, serialize_slot, save_slot, load_slot, clear_slot, slot_has_snapshot
from zorder.windows import WinRecord


def test_roundtrip_chinese_and_escapes():
    in_slot = {
        "windows": [
            WinRecord(1234, "chrome.exe",
                      'GitHub - 引号"反斜杠\\换行\n回车\r制表\t测试\x01',
                      "Chrome_WidgetWin_1", 0, 0, 1920, 1040),
            WinRecord(5678, "Obsidian.exe", "Obsidian", "QMainWindow", -8, 0, 1280, 1440),
        ],
        "foreground_pid": 1234,
        "taken_at": "2026-08-08 15:30:00",
    }
    text = json.dumps(serialize_slot(in_slot["windows"], in_slot["foreground_pid"],
                                     in_slot["taken_at"]), ensure_ascii=False)
    out = parse_slot(text)
    assert out is not None
    assert out["foreground_pid"] == 1234
    assert out["taken_at"] == "2026-08-08 15:30:00"
    assert len(out["windows"]) == 2
    a, b = out["windows"]
    assert (a.pid, a.exe, a.title, a.cls, a.left, a.top, a.width, a.height) == \
           (1234, "chrome.exe", in_slot["windows"][0].title, "Chrome_WidgetWin_1", 0, 0, 1920, 1040)
    assert (b.pid, b.exe, b.title, b.cls, b.left, b.top, b.width, b.height) == \
           (5678, "Obsidian.exe", "Obsidian", "QMainWindow", -8, 0, 1280, 1440)


def test_empty_windows_list():
    obj = serialize_slot([], 0, "")
    out = parse_slot(json.dumps(obj))
    assert out is not None and out["windows"] == []


def test_corrupt_inputs_rejected():
    assert parse_slot("") is None
    assert parse_slot("{") is None
    assert parse_slot("null") is None
    assert parse_slot("[1,2,3]") is None
    assert parse_slot('{"windows":[]}') is None          # 缺 foreground_pid/taken_at
    assert parse_slot('{"windows":[],"foreground_pid":0,"taken_at":"\\u12"}') is None  # 非法 \u 转义
    # 类型错误也必须拒绝
    assert parse_slot('{"windows":[],"foreground_pid":"0","taken_at":"x"}') is None
    assert parse_slot('{"windows":[{"pid":1}],"foreground_pid":0,"taken_at":"x"}') is None
    assert parse_slot('{"windows":[],"foreground_pid":0,"taken_at":"x","extra":1}') is None


def test_file_roundtrip(tmp_path, monkeypatch):
    import zorder.slots as slots
    monkeypatch.setattr(slots, "DATA_DIR", str(tmp_path))
    d = [WinRecord(42, "a.exe", "标题", "C", 1, 2, 3, 4)]
    ok, png_ok = save_slot(1, d, 42, "2026-08-12 10:00:00")   # 无 Qt → png 尽力失败不阻断
    assert ok and not png_ok
    loaded = load_slot(1)
    assert loaded is not None and loaded["foreground_pid"] == 42
    assert len(loaded["windows"]) == 1
    assert clear_slot(1)
    assert not slot_has_snapshot(1)
