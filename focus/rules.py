#!/usr/bin/env python3
"""Focus 规则编译 (纯逻辑, 无 I/O 依赖 — 全部可单测)。

config.ini (12win2blur 风格 INI) → 决策函数 + 扩展配置 JSON:
    [General]  Enabled / Port
    [Worktime] Days / Windows
    [Sites]    domain=budget(分钟), 0 = 工作时段硬拦
    [Sites.msedge] / [Sites.chrome]  可选, 按浏览器追加/覆盖

时间语义:
    工作时段内   → 额度执法 (budget 分钟/天, 0 = 全程硬拦)
    工作时段外   → 全部放行 (只记账不拦)
"""
import configparser
import datetime

DEFAULT_PORT = 51730


def _strip_inline_comment(line: str) -> str:
    # configparser 不支持行内注释; 支持 ";" / ";;" / "#"
    for marker in (";;", ";", "#"):
        idx = line.find(marker)
        if idx != -1:
            line = line[:idx]
    return line.rstrip()


def _norm_domain(raw: str) -> str:
    """容错: 接受用户直接粘贴的 'https://www.bilibili.com/watch' 之类。"""
    d = raw.strip().lower()
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def _parse_windows(raw: str):
    out = []
    for w in raw.split(","):
        w = w.strip()
        if not w:
            continue
        s, e = w.split("-")
        sh, sm = s.strip().split(":")
        eh, em = e.strip().split(":")
        out.append((int(sh) * 60 + int(sm), int(eh) * 60 + int(em)))
    return out


def parse_config(text: str) -> dict:
    # 只认 "=" 分隔 — 默认的 ":" 分隔符会把 "https://" 键腰斩
    cp = configparser.ConfigParser(delimiters=("=",))
    cp.read_string("\n".join(_strip_inline_comment(l) for l in text.splitlines()))

    gen = cp["General"] if cp.has_section("General") else {}
    work = cp["Worktime"] if cp.has_section("Worktime") else {}

    def _sites(section: str) -> dict:
        out = {}
        if cp.has_section(section):
            for k, v in cp.items(section):
                d = _norm_domain(k)
                if d:
                    out[d] = int(v)
        return out

    global_sites = _sites("Sites")
    browser_sites = {b: _sites(f"Sites.{b}") for b in ("msedge", "chrome")
                     if cp.has_section(f"Sites.{b}")}

    return {
        "enabled": str(gen.get("Enabled", "1")).strip() not in ("0", "false", "False"),
        "port": int(str(gen.get("Port", DEFAULT_PORT)).strip() or DEFAULT_PORT),
        "days": [int(d) for d in str(work.get("Days", "0,1,2,3,4")).split(",") if d.strip() != ""],
        "windows": _parse_windows(str(work.get("Windows", "08:00-12:00,13:00-16:00,19:00-22:00"))),
        "default_budget": int(str(work.get("Default", "5")).strip() or 5),
        "global_sites": global_sites,
        "browser_sites": browser_sites,
    }


def in_worktime(now: datetime.datetime, days, windows) -> bool:
    if now.weekday() not in days:
        return False
    cur = now.hour * 60 + now.minute
    return any(s <= cur < e for s, e in windows)


def sites_for_browser(cfg: dict, browser: str) -> dict:
    """合并 [Sites] 与 [Sites.<browser>]: 后者覆盖同名域, 其余追加。"""
    merged = dict(cfg["global_sites"])
    merged.update(cfg["browser_sites"].get(browser, {}))
    return merged


def budget_of(cfg: dict, raw_budget) -> int:
    """域未写预算 → 用 [Worktime] Default。"""
    if raw_budget is None:
        return cfg["default_budget"]
    return int(raw_budget)


def decide(budget_min: int, used_seconds: int, worktime_now: bool) -> str:
    """扩展执法决策。工作时段外一律 allow (off-work liberated)。"""
    if not worktime_now:
        return "allow"
    if budget_min <= 0:
        return "block_always"
    if used_seconds >= budget_min * 60:
        return "block_budget"
    return "allow"


def compile_for_client(cfg: dict, browser: str, usage: dict, now: datetime.datetime) -> dict:
    """发给扩展的配置快照 (usage 为当日两浏览器合计秒数)。"""
    sites = {d: {"budget": budget_of(cfg, b)} for d, b in sites_for_browser(cfg, browser).items()}
    wt_now = in_worktime(now, cfg["days"], cfg["windows"])
    return {
        "enabled": cfg["enabled"],
        "day": now.strftime("%Y-%m-%d"),
        "browser": browser,
        "days": cfg["days"],
        "windows": [list(w) for w in cfg["windows"]],
        "sites": sites,
        "usage": usage,
        "in_worktime": wt_now,
        "server_time": int(now.timestamp() * 1000),
    }
