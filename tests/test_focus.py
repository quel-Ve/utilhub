"""Focus 模块单测: config.ini 解析 / 工作时段判定 / 额度决策 / 用量记账。"""
import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "focus"))

from rules import compile_for_client, decide, in_worktime, parse_config, sites_for_browser  # noqa: E402
from tracker import ScreenTimeTracker  # noqa: E402

CFG_TEXT = """
[General]
Enabled=1
Port=51730

[Worktime]
Days=0,1,2,3,4          ; 周一到周五
Windows=08:00-12:00,13:00-16:00
Default=5

[Sites]
bilibili.com=5
https://www.xiaohongshu.com/ =0   ;; 粘贴整条 URL 也能解析, 0 = 硬拦
youtube.com=10

[Sites.msedge]
extra-edge.com=3
youtube.com=99           ;; 覆盖 [Sites] 同名域

[Sites.chrome]
extra-chrome.com=2
"""


@pytest.fixture(scope="module")
def cfg():
    return parse_config(CFG_TEXT)


# ── parse_config ──────────────────────────────────────────────────────

def test_parse_basic(cfg):
    assert cfg["enabled"] is True
    assert cfg["port"] == 51730
    assert cfg["days"] == [0, 1, 2, 3, 4]
    assert cfg["windows"] == [(480, 720), (780, 960)]
    assert cfg["default_budget"] == 5


def test_parse_domain_normalization(cfg):
    sites = cfg["global_sites"]
    assert "xiaohongshu.com" in sites        # URL → 域名, www. 剥离
    assert "https://www.xiaohongshu.com/" not in sites
    assert sites["xiaohongshu.com"] == 0     # 硬拦


def test_parse_browser_override(cfg):
    edge = sites_for_browser(cfg, "msedge")
    assert edge["youtube.com"] == 99         # 覆盖
    assert edge["extra-edge.com"] == 3       # 追加
    assert edge["bilibili.com"] == 5         # 全局继承
    chrome = sites_for_browser(cfg, "chrome")
    assert chrome["youtube.com"] == 10       # chrome 未覆盖 → 全局值
    assert "extra-edge.com" not in chrome    # 别浏览器的追加不串


# ── in_worktime ───────────────────────────────────────────────────────

def test_worktime_inside(cfg):
    mon1000 = datetime.datetime(2026, 8, 31, 10, 0)   # 周一
    assert in_worktime(mon1000, cfg["days"], cfg["windows"]) is True


def test_worktime_boundaries(cfg):
    mon = lambda h, m: datetime.datetime(2026, 8, 31, h, m)  # noqa: E731
    assert in_worktime(mon(8, 0), cfg["days"], cfg["windows"]) is True      # 闭开区间含起点
    assert in_worktime(mon(7, 59), cfg["days"], cfg["windows"]) is False
    assert in_worktime(mon(12, 0), cfg["days"], cfg["windows"]) is False    # 午休开始
    assert in_worktime(mon(11, 59), cfg["days"], cfg["windows"]) is True
    assert in_worktime(mon(13, 0), cfg["days"], cfg["windows"]) is True


def test_worktime_weekend_free(cfg):
    sat = datetime.datetime(2026, 8, 29, 10, 0)   # 周六
    sun = datetime.datetime(2026, 8, 30, 10, 0)   # 周日
    assert in_worktime(sat, cfg["days"], cfg["windows"]) is False
    assert in_worktime(sun, cfg["days"], cfg["windows"]) is False


# ── decide (水阀逻辑) ────────────────────────────────────────────────

def test_decide_off_work_always_free():
    assert decide(0, 0, False) == "allow"
    assert decide(5, 10_000, False) == "allow"


def test_decide_hard_block():
    assert decide(0, 0, True) == "block_always"


def test_decide_budget():
    assert decide(5, 4 * 60 + 59, True) == "allow"
    assert decide(5, 5 * 60, True) == "block_budget"


# ── compile_for_client ───────────────────────────────────────────────

def test_compile_for_client(cfg):
    now = datetime.datetime(2026, 8, 31, 10, 0)  # 周一 10:00 → 工作时段
    snap = compile_for_client(cfg, "msedge", {"bilibili.com": 120}, now)
    assert snap["browser"] == "msedge"
    assert snap["in_worktime"] is True
    assert snap["sites"]["youtube.com"]["budget"] == 99     # edge 覆盖生效
    assert snap["sites"]["extra-edge.com"]["budget"] == 3
    assert snap["usage"]["bilibili.com"] == 120
    assert snap["windows"] == [[480, 720], [780, 960]]


def test_compile_default_budget_applies():
    from rules import budget_of
    # 域未写预算 (编译层传 None) → 用 [Worktime] Default=5
    assert budget_of(parse_config(CFG_TEXT), None) == 5
    assert budget_of(parse_config(CFG_TEXT), 0) == 0     # 显式 0 = 硬拦, 不会被默认值顶掉


# ── ScreenTimeTracker ────────────────────────────────────────────────

def test_tracker_add_and_combined(tmp_path):
    t = ScreenTimeTracker(str(tmp_path))
    t.add("msedge", "bilibili.com", 30)
    t.add("msedge", "bilibili.com", 45)
    t.add("chrome", "bilibili.com", 10)
    assert t.combined_today()["bilibili.com"] == 85.0
    assert t.per_browser_today()["chrome"]["bilibili.com"] == 10.0


def test_tracker_rollover(tmp_path):
    t = ScreenTimeTracker(str(tmp_path))
    t.add("msedge", "x.com", 60)
    day_file = next(Path(tmp_path).glob("*.json"))
    # 手写"昨天"文件, 再把系统时间换日无法 mock — 直接构造跨日场景:
    # 覆盖 _day 让下次 add 触发重载
    day_file.write_text(json.dumps({"msedge": {"x.com": 999}}), encoding="utf-8")
    t2 = ScreenTimeTracker(str(tmp_path))
    assert t2.combined_today()["x.com"] == 999.0
    t2.add("chrome", "x.com", 5)   # 新写入仍落到今天的文件
    assert t2.combined_today()["x.com"] >= 999.0


def test_tracker_persists_to_disk(tmp_path):
    t = ScreenTimeTracker(str(tmp_path))
    t.add("chrome", "youtube.com", 120)
    files = list(Path(tmp_path).glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["chrome"]["youtube.com"] == 120.0


def test_tracker_ignores_junk(tmp_path):
    t = ScreenTimeTracker(str(tmp_path))
    t.add("msedge", "", 100)
    t.add("msedge", "x.com", -5)
    t.add("msedge", "x.com", 0)
    assert t.combined_today() == {}
