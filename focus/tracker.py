#!/usr/bin/env python3
"""屏幕使用时间记账: 按天 JSON 持久化 (data/focus/YYYY-MM-DD.json)。

结构: {"msedge": {domain: sec}, "chrome": {domain: sec}, "updated": epoch}
线程安全 (HTTP 线程并发写入); 每次写入即落盘 (文件很小, 崩溃安全优先)。
"""
import json
import os
import threading
import time


class ScreenTimeTracker:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._day = None
        self._data = {}

    def _path(self, day: str) -> str:
        return os.path.join(self.data_dir, f"{day}.json")

    def _load_day(self, day: str) -> None:
        path = self._path(day)
        try:
            with open(path, encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, ValueError):
            self._data = {}
        self._data.setdefault("msedge", {})
        self._data.setdefault("chrome", {})
        self._day = day

    def add(self, browser: str, domain: str, seconds: float) -> None:
        if seconds <= 0 or not domain:
            return
        day = time.strftime("%Y-%m-%d")
        with self._lock:
            if day != self._day:
                self._load_day(day)
            per = self._data.setdefault(browser, {})
            per[domain] = round(per.get(domain, 0) + seconds, 1)
            self._data["updated"] = time.time()
            try:
                with open(self._path(day), "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=1)
            except OSError:
                pass

    def combined_today(self) -> dict:
        """domain → 两浏览器合计秒数 (额度按总曝光计)。"""
        with self._lock:
            day = time.strftime("%Y-%m-%d")
            if day != self._day:
                self._load_day(day)
            out = {}
            for per in (self._data.get("msedge", {}), self._data.get("chrome", {})):
                for d, s in per.items():
                    out[d] = round(out.get(d, 0) + s, 1)
            return out

    def per_browser_today(self) -> dict:
        with self._lock:
            day = time.strftime("%Y-%m-%d")
            if day != self._day:
                self._load_day(day)
            return {
                "msedge": dict(self._data.get("msedge", {})),
                "chrome": dict(self._data.get("chrome", {})),
            }
