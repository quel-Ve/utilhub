# UtilityHub — 统一快捷键管家

Windows 统一后台常驻 + 托盘工具，聚合多个独立功能模块（各自独立编号开发）：

- **zorder 窗口快照/恢复**（原 19zorder-snapshot，C++ 版已并入为 Python 模块）
- **任务栏排序**（复用 6TaskbarSortTool 的注入链路）
- **EQ 预设切换**（复用 21eq-switcher 的 Switcher 轮询）
- **EdgeDock 右缘 dock 条 + API/plan 余额**（复用 28edge-dock，2026-09-19 并入）

## 热键（右 Alt 特化体系）

| 按键 | 功能 |
|------|------|
| 右 Alt（按住） | 顶部预览面板（键位提示 + 3 槽缩略图 + EQ 参数表格），松开收起 |
| 右 Alt + `,` `.` `/` | 槽 1/2/3 — 短按 = 空槽快照 / 非空槽恢复；长按 ≥0.9s = 自动快照 |
| 右 Alt + `;` | 任务栏排序（防抖 0.5s） |
| 右 Alt + `'` | EQ 循环切换 |
| Esc（右 Alt 按住中） | 取消 |
| Pause | VoiceInput 开关（复用 11cc-voice-input） |

## 运行

```bash
python hub.py              # 控制台调试
pythonw hub.py --daemon    # 无窗口常驻
install.bat                # 注册计划任务 UtilityHub（登录自启 + 提权）
uninstall.bat              # 移除任务 + 结束进程
```

## 架构

`hub.py`（20ms QTimer 编排）→ `hotkeys.py`（pynput 状态机）→ `zorder/`（decision 纯逻辑 / windows 采集恢复 / slots JSON schema / audio 提示音）→ `preview.py`（PyQt6 顶部预览）→ `tray.py`（原生 Win32 ctypes 托盘 + Qt QMenu 暗夜菜单）。EQ 与 VoiceInput 子模块按绝对路径 importlib 加载（`_load_sibling_module`），避免多项目 main.py 冲突。

## EdgeDock 集成（2026-09-19）

`edgedock_host.py`：28edge-dock 的右缘 dock 条 + balances 在 hub 进程内托管
（sys.path 注入 `../28edge-dock` 只读复用，不改其源码，dist\edgedock.exe 保留备用）。

- **托盘子菜单「EdgeDock (右缘)」**：隐藏/显示 dock、退出/启动（恢复）、立即刷新、
  详情窗口、游戏模式（checkable）+ 各平台余额灰显行。dock 自身右键菜单的"退出"
  也接到这里（停止托管，不退 hub）。
- **互斥体接管**：每次 `start()`（含 hub 启动、托盘"退出→启动"的恢复）若独立
  edgedock.exe 在跑 → taskkill 结束之，然后持 `edgedock.tray.MUTEX_NAME` 互斥体
  ——之后手动双开独立 exe 会自行静默退出（打印"28edge-dock 托盘已在运行"）。
  托盘"退出 EdgeDock"放掉互斥体，独立 exe 恢复可用。
  ⚠ **互斥体名必须从 `edgedock.tray` import，不许在本仓库硬编码**：2026-09-19
  tray.py 为绕开提权僵尸把名字改成 `...-singleton-v2`，而本仓库留旧名 → 两侧互相
  看不见 → 接管双向失效，独立 dock 与 hub dock 两个 vis=1 窗口同 rect
  (2438,0 122x1440) 并存 4 天（用户报"两个进程叠在一起"）。回归测试
  `tests/test_edgedock_host.py::test_mutex_name_is_single_source_of_truth` 锁死。
- **start/stop 成对管互斥体**：`stop()` 放掉后 `start()` 必须重新获取（老写法只在
  `__init__` 拿一次 → 托盘重启后托管 dock 裸奔）。回归测试
  `test_host_restart_reacquires_mutex`。
- **隐藏语义**：`HubDock(DockStrip)` 加 `user_hidden` 旗——托盘"隐藏 dock"后
  全屏避让轮询（2s）不会把 dock 拉回来；隐藏期间 balances 照常刷，恢复即时。
- **双形态**（dock 右键 / 托盘子菜单可切，持久化 edgedock.json）：hand=胶囊
  hover 弹出（默认，展开后光标移走 350ms 内自动收纳）；edge=无 hand、dock 垂直
  居中、光标贴右缘 1.2s 弹出。
- **单元格启动器**（鼠标友好的项目入口，直接对接 ccproject 项目）：webproj 格
  单击 = 未启动先拉起 `25ru-recite`（8625）再开浏览器；mesh 格单击 = 拉起
  `23agent-mesh` agent_dashboard（8765）并打开仪表盘；dockhide 格（»）= 隐藏 dock。
  ⚠ spawn 必须 `CREATE_BREAKAWAY_FROM_JOB`——计划任务的 job 对象在任务结束时清整棵
  进程树，detached 子进程也会被连坐（hub 每次重启 ru-recite 就死）；子进程输出落
  `logs/ru-recite-child.log` / `logs/agent-mesh-child.log` 可验尸。托盘菜单
  `popup()` 非模态后须自持引用（`_menu_ref`），否则本地引用出作用域即被 GC、菜单刚弹即毁。
- **屏幕时间格**：focus 记账合成快照（1min 刷新），gauge 圆环 + 总时长 badge，
  Android 数字健康 / Apple Watch 活动环风格；日目标 `28edge-dock/config.toml
  [dock] screen_time_goal_min`（默认 360min）。
- 配置/状态文件仍归 28edge-dock 仓库根：`config.toml`、`edgedock.json`
  （dock 位置/形态/游戏模式持久化，dist 同款已迁移）。诊断开关 `diagnostic.disable_edgedock`。

## 托盘暗夜模式（2026-09-19）

托盘右键菜单从原生 `TrackPopupMenu`（无法着色）换成 Qt `QMenu`，app 级
`NIGHT_QSS`（tray.py，sunset 系：底 `#12090d`、字 `#D9CCD2`、勾选块 `#C95D81`）
统一染暗托盘菜单 + 全进程 `QToolTip` + dock 右键菜单。图标 + 静音气泡仍走原生
`Shell_NotifyIconW`（`NIIF_NOSOUND`，QSystemTrayIcon 做不到静音）。托盘图标悬停
tooltip 由 Explorer 绘制，跟系统主题走，无法 per-app 染暗（原生限制）。

## Focus 专注拦截（2026-08-30）

`focus/` 模块 + 配套 Chromium 扩展（`focus/extension/`，Edge/Chrome 各加载一次）：
工作时段 + 每站每日额度的浏览器内拦截、屏幕使用时间记账、托盘状态/暂停。
hosts 方案（22focus-blocker）拦不住 VPN/DoH 流量且无法分浏览器——浏览器内拦截以此为准。
详见 [docs/focus.md](docs/focus.md)。

## 任务栏彩虹自愈（2026-09-15）

`autosort.py`：explorer 崩溃/重启后任务栏必回默认序（排序是注入手术，不写
TaskbandStream），且连带 LiveWallpaper2 的 WorkerW 壁纸层消失。本模块双路
检测 shell 重启（托盘窗口收 `TaskbarCreated` 广播 + 2s 轮询 `Shell_TrayWnd`
属主 PID），触发后等 8s 落定 → 组数稳定探测（自启应用陆续挂图标防漏组）→
自动按当前活跃预设重排，静音气泡反馈。独立 daemon 线程 + `_sort_lock` 与手动
排序互斥；`_sort_core` sort 失败自动重试一轮（probe→sort 间容器变化是
order mismatch 常见根因）。诊断开关 `diagnostic.disable_autosort`。

## 测试

```bash
pytest tests/
```

崩溃看门狗 `watchdog.py`：hub 非正常退出自动 toast + 重启计划任务。
