#!/usr/bin/env python3
"""Focus 本地服务: 只绑 127.0.0.1, 给配套浏览器扩展用。

GET  /config?browser=msedge|chrome  → 规则快照 + 当日用量 (扩展 1 分钟拉一次)
POST /usage                        → {"browser","entries":[{domain,seconds}]} 记账
GET  /stats                        → 托盘/人读: 工作时段 + 今日 top 用量
GET  /health                       → 存活探测

"暂停拦截 10 分钟" (托盘触发) 通过 /config 的 paused_until 字段下发,
扩展端到点自动恢复 — 服务重启即失效 (刻意的软退出阀, 不落盘)。
"""
import datetime
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    from rules import compile_for_client, in_worktime, parse_config, sites_for_browser
    from tracker import ScreenTimeTracker
except ImportError:
    # 作为 focus.server 包导入时 (hub): Python 3 无隐式相对导入, 走包内绝对路径。
    # 裸导入分支留给 `python focus/server.py` 单独调试。
    from focus.rules import (compile_for_client, in_worktime, parse_config,
                             sites_for_browser)
    from focus.tracker import ScreenTimeTracker

FOCUS_ROOT = os.path.dirname(os.path.abspath(__file__))


class FocusServer:
    def __init__(self, config_path=None, data_dir=None, port=None):
        self.config_path = config_path or os.path.join(FOCUS_ROOT, "config.ini")
        self.data_dir = data_dir or os.path.join(os.path.dirname(FOCUS_ROOT), "data", "focus")
        self.tracker = ScreenTimeTracker(self.data_dir)
        self._cfg = None
        self._cfg_loaded_at = 0.0
        self._cfg_lock = threading.Lock()
        self.paused_until = 0.0
        self.port = port
        self._httpd = None
        self._thread = None
        self.reload_config(force=True)
        if port is None:
            self.port = self._cfg["port"]

    def reload_config(self, force=False, max_age=5.0):
        """惰性重读 config.ini (5s 缓存; 托盘"打开配置"后用户改完文件自动生效)。"""
        now = time.time()
        with self._cfg_lock:
            if not force and now - self._cfg_loaded_at < max_age:
                return
            try:
                with open(self.config_path, encoding="utf-8") as f:
                    self._cfg = parse_config(f.read())
            except (OSError, ValueError) as e:
                if self._cfg is None:
                    raise
                # 保留旧配置, 服务不因配置写坏而停摆
            self._cfg_loaded_at = now

    @property
    def cfg(self) -> dict:
        self.reload_config()
        return self._cfg

    # ── Handlers ────────────────────────────────────────────────────

    def handle_config(self, browser: str) -> dict:
        snap = compile_for_client(self.cfg, browser or "chrome",
                                  self.tracker.combined_today(), datetime.datetime.now())
        snap["paused_until"] = int(self.paused_until * 1000)
        return snap

    def handle_usage(self, payload: dict) -> dict:
        browser = payload.get("browser", "chrome")
        for ent in payload.get("entries", []):
            self.tracker.add(browser, ent.get("domain", ""), float(ent.get("seconds", 0)))
        return {"ok": 1}

    def handle_stats(self) -> dict:
        cfg = self.cfg
        now = datetime.datetime.now()
        usage = self.tracker.combined_today()
        sites = sites_for_browser(cfg, "chrome")
        sites.update({k: v for k, v in sites_for_browser(cfg, "msedge").items()})
        top = []
        for d, sec in sorted(usage.items(), key=lambda kv: -kv[1])[:6]:
            budget = cfg["default_budget"] if d not in sites else sites[d]
            top.append({"domain": d, "minutes": round(sec / 60, 1),
                        "budget": budget, "blocked": sec >= budget * 60 and budget > 0})
        return {
            "enabled": cfg["enabled"],
            "in_worktime": in_worktime(now, cfg["days"], cfg["windows"]),
            "paused_for": max(0, int(self.paused_until - time.time())),
            "today": now.strftime("%Y-%m-%d"),
            "top": top,
        }

    # ── HTTP plumbing ───────────────────────────────────────────────

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                try:
                    u = urlparse(self.path)
                    if u.path == "/health":
                        self._send({"ok": 1})
                    elif u.path == "/config":
                        q = parse_qs(u.query)
                        self._send(server.handle_config((q.get("browser") or ["chrome"])[0]))
                    elif u.path == "/stats":
                        self._send(server.handle_stats())
                    else:
                        self._send({"error": "not found"}, 404)
                except Exception as e:  # noqa: BLE001 — 服务线程不许死
                    try:
                        self._send({"error": str(e)}, 500)
                    except Exception:
                        pass

            def do_POST(self):
                try:
                    if urlparse(self.path).path != "/usage":
                        self._send({"error": "not found"}, 404)
                        return
                    n = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(n) or b"{}")
                    self._send(server.handle_usage(payload))
                except Exception as e:  # noqa: BLE001
                    try:
                        self._send({"error": str(e)}, 500)
                    except Exception:
                        pass

            def log_message(self, *a):  # 静默 — hub.log 已够吵
                pass

        return Handler

    def start(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._make_handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="focus-server", daemon=True)
        self._thread.start()

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
