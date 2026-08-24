# UtilityHub — 统一快捷键管家

Windows 统一后台常驻 + 托盘工具，聚合三个独立功能模块（各自独立编号开发）：

- **zorder 窗口快照/恢复**（原 19zorder-snapshot，C++ 版已并入为 Python 模块）
- **任务栏排序**（复用 6TaskbarSortTool 的注入链路）
- **EQ 预设切换**（复用 21eq-switcher 的 Switcher 轮询）

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

`hub.py`（20ms QTimer 编排）→ `hotkeys.py`（pynput 状态机）→ `zorder/`（decision 纯逻辑 / windows 采集恢复 / slots JSON schema / audio 提示音）→ `preview.py`（PyQt6 顶部预览）→ `tray.py`（原生 Win32 ctypes 托盘）。EQ 与 VoiceInput 子模块按绝对路径 importlib 加载（`_load_sibling_module`），避免多项目 main.py 冲突。

## 测试

```bash
pytest tests/
```

崩溃看门狗 `watchdog.py`：hub 非正常退出自动 toast + 重启计划任务。
