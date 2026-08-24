"""槽位持久化 — slotN.json + slotN.png, schema 与 zorder C++ 完全同构。

data/ 位于仓库根 (便携工具风格, 同 zorder 的 exe 同目录 data/)。
"""
import json
import os
import time

from .windows import WinRecord

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

_REC_KEYS = {"pid", "exe", "title", "class", "left", "top", "width", "height"}


def data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def slot_json_path(slot):
    return os.path.join(data_dir(), f"slot{slot}.json")


def slot_png_path(slot):
    return os.path.join(data_dir(), f"slot{slot}.png")


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _rec_to_dict(r):
    return {"pid": r.pid, "exe": r.exe, "title": r.title, "class": r.cls,
            "left": r.left, "top": r.top, "width": r.width, "height": r.height}


def _dict_to_rec(d):
    return WinRecord(d["pid"], d["exe"], d["title"], d["class"],
                     d["left"], d["top"], d["width"], d["height"])


def serialize_slot(windows, foreground_pid, taken_at):
    return {"windows": [_rec_to_dict(w) for w in windows],
            "foreground_pid": foreground_pid, "taken_at": taken_at}


def parse_slot(text):
    """strict schema 解析, 与 C++ json.cpp 同级严格 (坏数据必须拒绝)。

    要求: 合法 JSON; windows 数组元素含全部 8 字段且类型正确; 无多余字段;
    foreground_pid 非负整数; taken_at 字符串。任何偏差返回 None。
    """
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if not isinstance(obj, dict) or set(obj) != {"windows", "foreground_pid", "taken_at"}:
        return None
    windows, fg, taken = obj["windows"], obj["foreground_pid"], obj["taken_at"]
    if not isinstance(windows, list) or not isinstance(taken, str):
        return None
    if not isinstance(fg, int) or isinstance(fg, bool) or fg < 0:
        return None
    recs = []
    for w in windows:
        if not isinstance(w, dict) or set(w) != _REC_KEYS:
            return None
        try:
            recs.append(_dict_to_rec(w))
        except (TypeError, KeyError, ValueError):
            return None
    return {"windows": recs, "foreground_pid": fg, "taken_at": taken}


def save_slot(slot, windows, foreground_pid, taken_at):
    """写 JSON + 尽力截图。返回 (json_ok, png_ok); 截图失败不阻断快照。"""
    text = json.dumps(serialize_slot(windows, foreground_pid, taken_at),
                      ensure_ascii=False, indent=1)
    try:
        with open(slot_json_path(slot), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        return False, False
    return True, capture_screenshot(slot_png_path(slot))


def load_slot(slot):
    try:
        with open(slot_json_path(slot), "r", encoding="utf-8") as f:
            return parse_slot(f.read())
    except OSError:
        return None


def clear_slot(slot):
    for p in (slot_json_path(slot), slot_png_path(slot)):
        try:
            os.remove(p)
        except OSError:
            pass
    return not slot_has_snapshot(slot)


def slot_has_snapshot(slot):
    return os.path.exists(slot_json_path(slot))


def capture_screenshot(png_path, width=480):
    """主屏抓图 → 480px 宽 PNG (PyQt6 QScreen, 等价 GDI+ BitBlt 缩放)。"""
    try:
        from PyQt6.QtGui import QGuiApplication
        from PyQt6.QtCore import Qt
        app = QGuiApplication.instance()
        screen = app.primaryScreen() if app else None
        if screen is None:
            return False
        img = screen.grabWindow(0).toImage()
        scaled = img.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        return scaled.save(png_path, "PNG")
    except Exception:
        return False
