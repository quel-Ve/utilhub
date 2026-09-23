// sw.js 暂停逻辑离线验证: mock chrome.* 后加载 sw.js, 断言三条路径。
// 用法: node tests/sw_pause_logic_test.js
"use strict";
const assert = require("assert");
const path = require("path");

const NOW = Date.now();
const store = {};
let lastTabUpdate = null;

const listenerSlots = { onInstalled: [], onStartup: [], onAlarm: [], onActivated: [], onUpdated: [], onRemoved: [] };
Object.defineProperty(global, "navigator", { value: { userAgent: "Mozilla/5.0 Chrome/144.0" }, configurable: true });  // 不含 Edg/ → chrome
global.chrome = {
    runtime: {
        getURL: p => `chrome-extension://fakeid/${p}`,
        onInstalled: { addListener: f => listenerSlots.onInstalled.push(f) },
        onStartup: { addListener: f => listenerSlots.onStartup.push(f) },
    },
    storage: {
        local: {
            get: async keys => {
                const out = {};
                for (const k of [].concat(keys)) if (k in store) out[k] = store[k];
                return out;
            },
            set: async obj => Object.assign(store, obj),
        },
    },
    alarms: { onAlarm: { addListener: f => listenerSlots.onAlarm.push(f) }, create: () => {} },
    tabs: {
        onActivated: { addListener: f => listenerSlots.onActivated.push(f) },
        onUpdated: { addListener: f => listenerSlots.onUpdated.push(f) },
        onRemoved: { addListener: f => listenerSlots.onRemoved.push(f) },
        query: async () => [],
        get: async () => ({}),
        update: async (id, props) => { lastTabUpdate = props; },
    },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
    idle: { queryState: async () => "active" },
    notifications: { create: async () => {} },
};

// worktime: 全天全周, bilibili 额度 5 分钟, 今日已用 330s (超了)
store.cfg = {
    enabled: true, days: [0, 1, 2, 3, 4, 5, 6], windows: [[0, 1440]],
    sites: { "bilibili.com": { budget: 5 } }, usage: { "bilibili.com": 330 },
};
store.buffer = {};

// 顶层函数声明要留在全局作用域才能拿到, 用全局 eval 而非 require (模块闭包会吞掉)
const src = require("fs").readFileSync(
    path.join(__dirname, "..", "focus", "extension", "sw.js"), "utf8");
(0, eval)(src);

(async () => {
    const future = NOW + 600_000, past = NOW - 1_000;

    // 1) 暂停生效中: evaluate 必须放行
    store.cfg = { ...store.cfg, paused_until: future };
    assert.strictEqual((await evaluate("bilibili.com")).action, "allow", "P1: 暂停中 evaluate 应 allow");

    // 2) THE FIX — 卡在 blocked.html 的标签, 暂停中必须被放回原站
    lastTabUpdate = null;
    await enforceTab({ id: 7, url: chrome.runtime.getURL("blocked.html") + "?domain=bilibili.com" });
    assert.deepStrictEqual(lastTabUpdate, { url: "https://bilibili.com" },
        "P2: 暂停中 blocked 标签应恢复到原站");

    // 3) 未暂停/已过期: blocked 标签维持拦截页
    lastTabUpdate = null;
    store.cfg = { ...store.cfg, paused_until: past };
    await enforceTab({ id: 7, url: chrome.runtime.getURL("blocked.html") + "?domain=bilibili.com" });
    assert.strictEqual(lastTabUpdate, null, "P3: 过期后应维持拦截页");

    // 4) 过期后正常执法恢复: evaluate 重新 block
    assert.strictEqual((await evaluate("bilibili.com")).action, "block", "P4: 过期后 evaluate 应 block");

    console.log("PASS: 4/4 — 暂停放行/恢复标签/过期维持/过期重拦 全部符合预期");
})().catch(e => { console.error("FAIL:", e.message); process.exit(1); });
