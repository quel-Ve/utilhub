// Focus Guard — UtilityHub 配套扩展 (MV3 service worker)。
// Edge / Chrome 共用同一目录: UA 含 "Edg/" 即 msedge, 否则 chrome。
// 配置与额度来自 UtilityHub 本地服务 (127.0.0.1); 服务失联时按最后缓存继续执法。

const DEFAULT_PORT = 51730;
const TICK_ALARM = "fg-tick";    // 30s: 记账 + 执法 + 徽章
const FETCH_ALARM = "fg-fetch";  // 60s: 拉 /config
const TICK_SECONDS = 30;
const BROWSER = navigator.userAgent.includes("Edg/") ? "msedge" : "chrome";

// ── storage helpers ───────────────────────────────────────────────

const st = async keys => chrome.storage.local.get(keys);
const stSet = obj => chrome.storage.local.set(obj);

// ── config sync ───────────────────────────────────────────────────

async function fetchConfig() {
    const { port, cfg: old } = await st(["port", "cfg"]);
    const p = port || DEFAULT_PORT;
    try {
        const res = await fetch(`http://127.0.0.1:${p}/config?browser=${BROWSER}`,
            { signal: AbortSignal.timeout(2500) });
        const data = await res.json();
        await stSet({ cfg: data, port: data.port || p, serverUp: true, cfgAt: Date.now() });
        return data;
    } catch (e) {
        // 服务失联: 用缓存配置继续执法 (工作时段判定在本地算, 不依赖服务在线)
        await stSet({ serverUp: false });
        return old || null;
    }
}

// ── rules (与 focus/rules.py 对应) ────────────────────────────────

// config.ini Days 用 周一=0 ... 周日=6 (Python weekday 约定); JS getDay 周日=0
function inWorktime(cfg, now = new Date()) {
    if (!cfg || cfg.enabled === false) return false;
    const pyDay = (now.getDay() + 6) % 7;
    if (!(cfg.days || []).includes(pyDay)) return false;
    const cur = now.getHours() * 60 + now.getMinutes();
    return (cfg.windows || []).some(([s, e]) => cur >= s && cur < e);
}

function matchSite(sites, hostname) {
    hostname = (hostname || "").toLowerCase();
    for (const d of Object.keys(sites || {})) {
        if (hostname === d || hostname.endsWith("." + d)) return d;
    }
    return null;
}

// 当日已用 = 服务端快照 + 本地未上报 buffer
async function usedSeconds(domain) {
    const { cfg, buffer } = await st(["cfg", "buffer"]);
    return (cfg?.usage?.[domain] || 0) + (buffer?.[domain] || 0);
}

async function evaluate(domain) {
    const { cfg } = await st(["cfg"]);
    if (!cfg || cfg.enabled === false) return { action: "allow" };
    // 服务端 /config 下发的 paused_until (epoch ms, 托盘"暂停拦截10分钟"): 到点自动恢复。
    // 曾读 storage 顶层 pausedUntil — 没人写过该 key, 暂停从未生效 (2026-09-15 修复)。
    if (cfg.paused_until && Date.now() < cfg.paused_until) return { action: "allow" };
    const wt = inWorktime(cfg);
    const rule = cfg.sites?.[domain];
    if (!rule) return { action: "allow", wt };
    if (!wt) return { action: "allow", wt };  // 下班后全部放行 (仍记账)
    const used = await usedSeconds(domain);
    if (rule.budget <= 0) return { action: "block", reason: "always", used };
    if (used >= rule.budget * 60) return { action: "block", reason: "budget", budget: rule.budget, used };
    return { action: "allow", wt, budget: rule.budget, used };
}

// ── enforcement ───────────────────────────────────────────────────

async function enforceTab(tab) {
    if (!tab?.url) return;
    // 暂停生效中: 把卡在 blocked 页的标签放回原站点 (否则暂停后原页不会自己回来)
    if (tab.url.startsWith(chrome.runtime.getURL("blocked.html"))) {
        const { cfg } = await st("cfg");
        if (!cfg?.paused_until || Date.now() >= cfg.paused_until) return;  // 不在暂停中 → 维持拦截页
        const domain = new URL(tab.url).searchParams.get("domain");
        if (domain) chrome.tabs.update(tab.id, { url: `https://${domain}` });
        return;
    }
    if (!/^https?:/i.test(tab.url)) return;
    const { cfg } = await st("cfg");
    const domain = matchSite(cfg?.sites, new URL(tab.url).hostname);
    if (!domain) return;
    let verdict = await evaluate(domain);
    if (verdict.action === "block") {
        // 执法前重拉一次配置: 托盘刚点"暂停拦截"/刚改额度时立即生效, 不等 ≤60s 轮询
        await fetchConfig();
        verdict = await evaluate(domain);
        if (verdict.action !== "block") { updateBadge(); return; }
    }
    if (verdict.action === "block") {
        const q = `?domain=${encodeURIComponent(domain)}&reason=${verdict.reason}` +
            `&budget=${verdict.budget ?? 0}&used=${Math.floor((verdict.used || 0) / 60)}`;
        chrome.tabs.update(tab.id, { url: chrome.runtime.getURL("blocked.html") + q });
        notifyBlocked(domain, verdict);
    }
    updateBadge();
}

async function notifyBlocked(domain, verdict) {
    const day = new Date().toISOString().slice(0, 10);
    const { notifyDay } = await st("notifyDay");
    if (notifyDay?.[domain] === day) return;
    await stSet({ notifyDay: { ...(notifyDay || {}), [domain]: day } });
    const why = verdict.reason === "always"
        ? "工作时段硬拦截 (额度设为 0)"
        : `今日 ${verdict.budget} 分钟额度已用完`;
    chrome.notifications.create(`fg-${domain}-${day}`, {
        type: "basic", iconUrl: "icon128.png", title: "Focus Guard",
        message: `${domain} — ${why}`,
    });
}

// ── 30s tick: 记账 → 上报 → 执法 → 徽章 ───────────────────────────

async function tick() {
    const { cfg } = await st("cfg");
    // 1) 记账: 仅当 用户活跃 + 窗口聚焦 + 活动标签命中追踪站点
    let focusedTab = null;
    const idle = await chrome.idle.queryState(30);
    if (idle === "active") {
        try {
            [focusedTab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        } catch { /* no focused window */ }
    }
    if (focusedTab?.url && /^https?:/i.test(focusedTab.url) && cfg?.sites) {
        const domain = matchSite(cfg.sites, new URL(focusedTab.url).hostname);
        if (domain) {
            const { buffer } = await st("buffer");
            const buf = { ...(buffer || {}) };
            buf[domain] = (buf[domain] || 0) + TICK_SECONDS;
            await stSet({ buffer: buf });
        }
    }
    await postUsage();
    if (focusedTab) await enforceTab(focusedTab);
    await updateBadge();
}

async function postUsage() {
    const { buffer, port } = await st(["buffer", "port"]);
    if (!buffer || Object.keys(buffer).length === 0) return;
    const entries = Object.entries(buffer).map(([domain, seconds]) => ({ domain, seconds }));
    try {
        const res = await fetch(`http://127.0.0.1:${port || DEFAULT_PORT}/usage`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ browser: BROWSER, entries }),
            signal: AbortSignal.timeout(2500),
        });
        if (res.ok) await stSet({ buffer: {} });  // 服务端确认后才清 buffer
    } catch { /* 保留 buffer, 下个 tick 重试 */ }
}

// ── badge: 让"在不在管"一眼可见 ───────────────────────────────────

async function updateBadge() {
    const { cfg } = await st("cfg");
    if (!cfg || cfg.enabled === false) {
        chrome.action.setBadgeText({ text: "" });
        return;
    }
    if (cfg.paused_until && Date.now() < cfg.paused_until) {
        chrome.action.setBadgeBackgroundColor({ color: "#f9a825" });
        chrome.action.setBadgeText({ text: "⏸" });
        return;
    }
    if (!inWorktime(cfg)) {
        chrome.action.setBadgeBackgroundColor({ color: "#9e9e9e" });
        chrome.action.setBadgeText({ text: "休" });
        return;
    }
    chrome.action.setBadgeBackgroundColor({ color: "#2e7d32" });
    let text = "ON";
    try {
        const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        if (tab?.url && /^https?:/i.test(tab.url) && cfg.sites) {
            const d = matchSite(cfg.sites, new URL(tab.url).hostname);
            if (d) {
                const used = await usedSeconds(d);
                const b = cfg.sites[d].budget;
                text = b <= 0 ? "拦" : String(Math.max(0, Math.ceil(b - used / 60)));
            }
        }
    } catch { /* keep ON */ }
    chrome.action.setBadgeText({ text });
}

// ── wiring ────────────────────────────────────────────────────────

function setupAlarms() {
    chrome.alarms.create(TICK_ALARM, { periodInMinutes: 0.5 });
    chrome.alarms.create(FETCH_ALARM, { periodInMinutes: 1 });
    fetchConfig().then(updateBadge);
}

chrome.runtime.onInstalled.addListener(setupAlarms);
chrome.runtime.onStartup.addListener(setupAlarms);
chrome.alarms.onAlarm.addListener(al => {
    if (al.name === TICK_ALARM) void tick();
    else if (al.name === FETCH_ALARM) void fetchConfig().then(updateBadge);
});
chrome.tabs.onActivated.addListener(({ tabId }) => {
    chrome.tabs.get(tabId, tab => { if (!chrome.runtime.lastError) void enforceTab(tab); });
});
chrome.tabs.onUpdated.addListener((tabId, info) => {
    if (info.status === "complete") {
        chrome.tabs.get(tabId, tab => { if (!chrome.runtime.lastError) void enforceTab(tab); });
    }
});
chrome.tabs.onRemoved.addListener(() => updateBadge());

// 首次被唤醒 (无事件残留时) 也拉一次配置, 防止 SW 冷启动后配置过期
fetchConfig().then(updateBadge);
