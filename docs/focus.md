# Focus 专注拦截 — 架构与使用

> 2026-08-30 引入 UtilityHub。浏览器内拦截（工作时段 + 每日额度）由配套 Chromium 扩展执行，
> UtilityHub 负责规则配置、屏幕使用时间记账与托盘可视。

## 为什么不是原来的 hosts 方案（22focus-blocker）

排查结论（2026-08-30）：

1. **计划任务从未安装**——`schtasks /tn FocusBlocker` 查无此项，守护进程根本没跑过（`--doctor` 自检可复现）。
2. **`extra_domains` 从未生效**——`main.py` 只把 `domains` 传给 hosts_guard，xiaohongshu/instagram 等全在死配置里（已修复）。
3. **hosts 方案的结构性天花板**（本模块要解决的）：
   - Chrome 挂 VPN / 浏览器开 DoH（安全 DNS）→ 流量绕过系统 DNS，hosts 完全失效；
   - 无法区分浏览器（msedge 与 chrome 只能一刀切）、无法"不影响其他操作"（整个域名都断）；
   - 无每站每日额度、无使用时长记账、状态不可见。

hosts 方案修完 bug 后仍可作为系统级兜底（`python main.py --doctor` 自检），
但浏览器内拦截以本模块为准。

## 架构

```
focus/config.ini ──(15s 惰性重读)──→ FocusServer (127.0.0.1:51730)
       │                                  ↑ GET /config   ↓ POST /usage
       │                          focus/extension (MV3 扩展, Edge/Chrome 各装一份)
       │                                  │ 活动标签页 30s 记账 + 执法 + 徽章
       ▼                                  ▼
  data/focus/YYYY-MM-DD.json  ←──── 屏幕使用时间 (两浏览器合计)
```

- **规则**：`focus/config.ini`（12win2blur 风格 INI），托盘改完即生效（≤5s + 扩展 1 分钟拉取）。
- **记账**：扩展每 30s 上报活动标签页停留时长（用户活跃 + 窗口聚焦才计）；
  服务端两浏览器合计，写当日 JSON。
- **执法**：工作时段内，站点额度用完（或配置为 0）→ 活动标签重定向到拦截页；
  **工作时段外全部放行**（只记账）。额度按"总曝光"计，两浏览器共享。
- **可见性**：扩展工具栏徽章 = 绿 `ON`/剩余分钟/`拦`（工作时段）、灰 `休`（下班）；
  托盘 → 专注 Focus 子菜单显示状态与今日 top 用量。

## 安装（一次性，两个浏览器各一次）

1. Edge：地址栏 `edge://extensions` → 打开"开发人员模式" → "加载解压缩的扩展" →
   选 `5UtilityHub/focus/extension/`。
2. Chrome：`chrome://extensions` → 同样操作（同一目录，扩展自动识别所在浏览器）。
3. UtilityHub 重启后日志应出现 `Focus 服务已启动: http://127.0.0.1:51730`。

> Chrome 137+ 已移除 `--load-extension` 命令行安装，加载解压缩必须手动一次。
> 装完后建议固定到工具栏（拼图图标 → Focus Guard → 图钉），徽章常驻可见。

## config.ini 速查

```ini
[General]
Enabled=1            ; 总开关
Port=51730           ; 与扩展默认端口一致

[Worktime]
Days=0,1,2,3,4       ; 周一=0 ... 周日=6
Windows=08:00-12:00,13:00-16:00,19:00-22:00
Default=5            ; 每站每日默认额度(分钟), 0=工作时段硬拦

[Sites]
bilibili.com=5       ; 子域自动匹配; 粘贴整条 URL 也能解析
pornhub.com=0        ; NSFW 建议全部填 0 (硬拦)

[Sites.msedge]       ; 可选: 按浏览器覆盖/追加
[Sites.chrome]
```

## 压力阀

托盘 → 专注 Focus → **暂停拦截 10 分钟**（内存态，重启 UtilityHub 即失效，不落盘——
刻意的软退出阀，避免"一次配置永久放空"）。

## 文件

| 文件 | 作用 |
|---|---|
| `focus/config.ini` | 用户规则（INI） |
| `focus/rules.py` | 解析 + 工作时段 + 决策（纯逻辑，tests/test_focus.py 覆盖） |
| `focus/tracker.py` | 屏幕使用时间记账（按天 JSON） |
| `focus/server.py` | 127.0.0.1 HTTP 服务（/config /usage /stats /health） |
| `focus/extension/` | Chromium 扩展（MV3）：sw.js + blocked.html + 图标 |
| `data/focus/` | 每日用量 JSON（历史即审计） |
