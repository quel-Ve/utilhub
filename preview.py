"""顶部预览窗 (PyQt6) — zorder preview.cpp 移植: 3 槽缩略图 + 布局示意 + 进度条 + 提示行。

窗口属性对应: WS_EX_TOPMOST|TOOLWINDOW|LAYERED|TRANSPARENT → Qt WindowStaysOnTopHint
|Tool|FramelessWindowHint|WindowTransparentForInput (点击穿透), 不抢焦点。
"""
from PyQt6.QtCore import QEasingCurve, QParallelAnimationGroup, QPoint, QPointF
from PyQt6.QtCore import QPropertyAnimation, QRect, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QCursor, QFont, QImage, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

import ctypes
import os
import re
from ctypes import wintypes

import winreg

# ---- W1 Acrylic 亚克力 (2026-08-14, P1) ----
class _ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", wintypes.DWORD), ("AnimationId", ctypes.c_int)]

class _WCA_DATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]

def _enable_acrylic(hwnd):
    """Win10 1803+ SetWindowCompositionAttribute 亚克力 (未公开 API)。

    GradientColor 0xF21F1F1F = AABBGGRR (α242 深灰 tint, 与 _BG 同源)。
    失败返回 False → 调用方保持纯色填充。HUB_ACRYLIC=0 环境变量强制关闭。
    """
    try:
        if not ctypes.windll.user32.IsWindow(wintypes.HWND(hwnd)):
            return False   # 无效句柄 (offscreen 测试平台等) — 绝不裸调用户32
        accent = _ACCENT_POLICY(4, 0, 0xF21F1F1F, 0)   # 4 = ACRYLICBLURBEHIND
        data = _WCA_DATA(19, ctypes.addressof(accent), ctypes.sizeof(accent))  # 19 = WCA_ACCENT_POLICY
        return bool(ctypes.windll.user32.SetWindowCompositionAttribute(
            wintypes.HWND(hwnd), ctypes.byref(data)))
    except Exception:
        return False

EQ_TABLE_W = 575    # 右侧 EQ 预设表格 (等宽列布局, 2026-08-13 扩 15px 显示完整)
EQ_ROW_H = 40
SIDE_HINT_W = 100   # 左侧 ` 排序键帽区 (2026-08-13 加大)
W, H = SIDE_HINT_W + 14 + 3 * (272 + 8) + 14 + EQ_TABLE_W, 220
THUMB_W, THUMB_H = 272, 153
THUMB_GAP = 8
PROGRESS_H = 8
TOP_MARGIN = (H - THUMB_H) // 2   # 2026-08-14 列居中: 槽位块垂直居中 (220-153)//2=33
_FONT = "Microsoft YaHei UI"   # Win10 UI 字体
_BG = QColor(31, 31, 31, 242)  # Win10 暗色模式背景
_BORDER = QColor(58, 58, 58)
PINK = QColor(255, 105, 180)   # hub 粉色强调 (2026-08-13 用户指定)
_EQ_SHORT = {"electronic": "elec"}   # 显示名简写 (2026-08-13 用户要求)

# ---- 2026-08-14 Win10 美化方案 P0 常量 (集中在此, 不散落魔法数字) ----
BORDER_CLR = QColor(255, 255, 255, 60)      # W2 外圈 1px 亮边 (与桌面切开)
BORDER_SHADOW = QColor(0, 0, 0, 160)        # W2 底部 1px 暗线 (面板厚度感)
KEY_TOP_HILITE = QColor(255, 255, 255, 30)  # K1 键帽顶部拟物高光
KEY_BOTTOM_EDGE = QColor(0, 0, 0, 110)      # K1 键帽底部深边 (厚度)
ARMED_INNER = QColor(255, 255, 255, 50)     # K2 armed 框 1px 内亮边
DIVIDER_ALPHA = 46                           # D1 分隔线中部 α
THUMB_BORDER = QColor(255, 255, 255, 28)    # S1 缩略图内描边
EMPTY_DASH = QColor(255, 255, 255, 70)       # S3 空槽虚线框
EMPTY_ICON = QColor(255, 255, 255, 90)       # S3 相机图标
EMPTY_TITLE = QColor(255, 255, 255, 110)     # S3 "空槽" 标题
EMPTY_HINT = QColor(255, 255, 255, 70)       # S3 引导文案

# ---- 2026-08-14 Win10 美化方案 P1 常量 ----
_BG_TINT = QColor(31, 31, 31, 90)    # W1: 亚克力启用时 Qt 侧只叠色 (模糊由 DWM 出)
_FONT_FAMILIES = ["Segoe UI", "Microsoft YaHei UI"]   # W4: 英文数字走 Segoe UI, 中文回退雅黑


def _font(size, weight=None, families=None):
    """W4 字体栈: 默认 Segoe UI → YaHei UI (英文数字 Win10 原生, 中文回退)。"""
    f = QFont()
    f.setFamilies(families or _FONT_FAMILIES)
    f.setPointSize(size)
    if weight is not None:
        f.setWeight(weight)
    return f


def _fmt_freq(fc):
    if fc >= 1000:
        return f"{fc / 1000:g}k"   # 1.6k / 10k / 11k — :g 去尾零
    return str(int(fc))


def preset_summary_lines(text):
    """预设文件 → 紧凑参数摘要: Preamp + 各 filter 的「频率+增益」数字。

    例: Filter 1: ON LSC Fc 50 Hz Gain 2 dB → "50+2"; 2200Hz → "2.2k+2"。
    2026-08-13 可读性改进: 去掉类型字母前缀 (L/P/H), 纯频点+分贝更直观。
    """
    preamp = ""
    bands = []
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith("preamp"):
            m = re.search(r"[-+]?\d+(\.\d+)?", s)
            if m:
                preamp = m.group(0)
        elif s.startswith("Filter ") and " ON " in s:
            m_fc = re.search(r"Fc\s+(\d+(?:\.\d+)?)\s*Hz", s)
            m_g = re.search(r"Gain\s+([-+]?\d+(?:\.\d+)?)\s*dB", s)
            if m_fc and m_g:
                g = float(m_g.group(1))
                gs = f"{g:+.1f}".rstrip("0").rstrip(".")
                bands.append(f"{_fmt_freq(float(m_fc.group(1)))}{gs}")
    # 等宽列填充 (2026-08-13 用户要求竖向列对齐): Consolas 等宽字体下
    # 每列固定字符宽, 各行同一列从相同 x 位置开始
    head = f"-{preamp.lstrip('-')}dB" if preamp else ""
    cols = [f"{head:<5}"] if head else []
    cols += [f"{b:<9}" for b in bands]   # 最宽 band 8 字符 (如 1.6k+1.5) + 1 空隙
    return "".join(cols).rstrip()


def _accent_color():
    """Win 主题强调色 (注册表 DWM\\AccentColor, AABBGGRR 字节序)。

    2026-08-13 教训: 0xFF7550BD 若按 AARRGGBB 解析得紫色, 实际主题是
    #BD5075 玫瑰粉 (RR=BD GG=50 BB=75) — 用户系统实测确认。
    解析失败回退 PINK。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\DWM") as k:
            v, _ = winreg.QueryValueEx(k, "AccentColor")
        if v:
            a = (v >> 24) & 0xFF
            b = (v >> 16) & 0xFF   # AABBGGRR
            g = (v >> 8) & 0xFF
            r = v & 0xFF
            return QColor(r, g, b, a)
    except OSError:
        pass
    return PINK


class PreviewWindow(QWidget):
    def __init__(self):
        super().__init__(None,
                         Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(W, H)
        screen = QApplication.primaryScreen()
        self.move((screen.availableGeometry().width() - W) // 2, 0)
        self._active = 0            # 当前按下槽 1..3, 0=无
        self._hold = 0
        self._msg = ""
        self._eq_entries = []       # [(name, summary)] EQ 表格数据
        self._eq_current = ""       # 当前预设名 (高亮)
        self._sort_armed = False    # Alt+; 按住中: 左块灰色框+弥散 (2026-08-14)
        self._anim = None           # W3 显隐动画句柄 (2026-08-14)
        self._acrylic_done = False  # W1: 亚克力只装一次
        self._acrylic_active = False
        self._session_visible = False  # 热键会话驱动显示 (右 Alt/Ctrl+Alt 按住)
        self._thumbs = [None, None, None]     # QImage or None (drawPixmap 在 PyQt6.11 硬崩 → 用 QImage 管线)
        self._data = [None, None, None]       # parse_slot 结果 or None
        self._reload_slots()
        self.hide()

    def set_eq_entries(self, entries, current):
        """右侧 EQ 表格: [(name, summary)] + 当前预设名 (hub 注入)。"""
        self._eq_entries = entries
        self._eq_current = current or ""
        self.update()

    @property
    def session_visible(self):
        return self._session_visible

    # ---------- 数据 ----------

    def _reload_slots(self):
        from zorder import slots
        for i in range(3):
            slot = i + 1
            self._data[i] = slots.load_slot(slot) if slots.slot_has_snapshot(slot) else None
            img = QImage(slots.slot_png_path(slot))
            self._thumbs[i] = img if not img.isNull() else None

    def refresh_slots(self):
        self._reload_slots()
        self.update()

    # ---------- 对外 API (对应 preview.cpp) ----------

    def show_preview(self, active_slot):
        self._session_visible = True
        self._active = active_slot
        self._hold = 0
        self._msg = ""
        if not self.isVisible():
            self._fade_in()
        self.update()

    def show_temp(self):
        """非会话临时显示 (EQ 切换/F12 单按): 不置 session 标志, 可被 later 隐藏。"""
        if not self.isVisible():
            self._fade_in()
        self.update()

    def hide_preview(self):
        self._session_visible = False
        self._active = 0
        self._hold = 0
        self._msg = ""
        if self.isVisible():
            self._fade_out()
        else:
            self.hide()

    def set_hold(self, slot, hold_ms):
        self._active = slot
        self._hold = hold_ms
        self.update()

    def set_message(self, msg):
        self._msg = msg
        self.update()

    def set_sort_armed(self, on):
        """Alt+; 按住中: 左块灰色圆角框 + 深灰弥散, ; 键帽居中 (2026-08-14)。"""
        self._sort_armed = bool(on)
        self.update()

    # ---------- W3 显隐动画 (2026-08-14) ----------

    def _fade_in(self):
        """120ms 淡入 + 8px 下滑 (只在显隐瞬间跑, 绝不在 set_hold 刷新里起动画)。"""
        if self._anim is not None:
            self._anim.stop()
        # P1 多屏: 面板出现在光标所在屏 (贴该屏顶居中), 回退主屏
        scr = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        geo = scr.availableGeometry()
        cx = geo.x() + (geo.width() - W) // 2
        self.move(cx, geo.y() - 8)
        self.show()
        g = QParallelAnimationGroup(self)
        fade = QPropertyAnimation(self, b"windowOpacity")
        fade.setDuration(120)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        slide = QPropertyAnimation(self, b"pos")
        slide.setDuration(120)
        slide.setStartValue(QPoint(cx, geo.y() - 8))
        slide.setEndValue(QPoint(cx, geo.y()))
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        g.addAnimation(fade)
        g.addAnimation(slide)
        g.start()
        self._anim = g

    def _fade_out(self):
        """80ms 淡出后隐藏。"""
        if self._anim is not None:
            self._anim.stop()
        a = QPropertyAnimation(self, b"windowOpacity")
        a.setDuration(80)
        a.setStartValue(self.windowOpacity())
        a.setEndValue(0.0)
        a.finished.connect(self._on_fade_done)
        a.start()
        self._anim = a

    def _on_fade_done(self):
        self.hide()
        self.setWindowOpacity(1.0)
        self._anim = None

    def showEvent(self, event):
        """W1: 首次显示后装亚克力 (仅一次, 失败回退纯色; HUB_ACRYLIC=0 强制关)。"""
        super().showEvent(event)
        if not self._acrylic_done:
            self._acrylic_done = True
            if os.environ.get("HUB_ACRYLIC", "1") != "0":
                self._acrylic_active = _enable_acrylic(int(self.winId()))

    # ---------- 绘制 ----------

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 半透明黑底
        p.fillRect(QRect(0, 0, W, H), _BG if not self._acrylic_active else _BG_TINT)   # W1: 亚克力时只叠色
        self._draw_side_hints(p)
        for i in range(3):
            self._draw_slot(p, i)
        self._draw_eq_table(p)
        # D1: 三区垂直分隔线, 两端渐隐 (排序键帽 | 槽区 | EQ 表格)
        slot_x = SIDE_HINT_W + 6
        eq_x = (W - EQ_TABLE_W) - 1
        for div_x in (slot_x, eq_x):
            grad = QLinearGradient(div_x, 14, div_x, 192)
            grad.setColorAt(0.0, QColor(255, 255, 255, 0))
            grad.setColorAt(0.12, QColor(255, 255, 255, DIVIDER_ALPHA))
            grad.setColorAt(0.88, QColor(255, 255, 255, DIVIDER_ALPHA))
            grad.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillRect(QRect(div_x, 14, 1, 178), QBrush(grad))
        if self._msg:
            # W5: 消息限定 x 0–967 居中 (不压 EQ 表第 5 行); M1: 成功类 ✓ 语义色 + 1px 文字阴影
            success = any(k in self._msg for k in ("done", "OK", "已", "完成"))
            msg = ("✓ " if success else "") + self._msg
            p.setFont(_font(12))
            p.setPen(QColor(0, 0, 0, 150))
            p.drawText(QRectF(1, H - 29, 967, 24), Qt.AlignmentFlag.AlignCenter, msg)
            p.setPen(QColor(108, 203, 95) if success else QColor(255, 255, 255, 230))
            p.drawText(QRectF(0, H - 30, 967, 24), Qt.AlignmentFlag.AlignCenter, msg)
        # W2: 外圈 1px 亮边 + 底部 1px 暗线 (内描边不扩窗口, 0.5 偏移压准像素)
        p.setPen(QPen(BORDER_CLR, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(0.5, 0.5, W - 1, H - 1))
        p.setPen(QPen(BORDER_SHADOW, 1))
        p.drawLine(QPointF(1, H - 1.5), QPointF(W - 2, H - 1.5))
        p.end()

    def _draw_side_hints(self, p: QPainter):
        """左侧键位提示: [;] + 排序 — Win10 直角键帽 (2026-08-13 改 ; 为排序键)。

        排序 armed (Alt+; 按住中, 2026-08-14 用户设计): 整块灰色圆角框 +
        深灰弥散, ; 键帽放大居中。
        """
        if self._sort_armed:
            self._draw_sort_armed(p)
            return
        lx = (SIDE_HINT_W - 44) // 2
        self._draw_keycap(p, lx, self._session_visible)
        p.setFont(_font(10))
        p.setPen(QColor(190, 190, 190, 195))
        p.drawText(QRectF(0, 140, SIDE_HINT_W, 22), Qt.AlignmentFlag.AlignHCenter, "排序")

    def _draw_keycap(self, p: QPainter, lx: int, pressed: bool):
        """K1 键帽 (normal/armed 共用同一绘制路径, 保证位置像素级一致)。

        键帽垂直居中于左列 (列高 H=220 → 键帽 88..132, 中心 110)。
        拟物两笔: 顶 1px 亮线 + 底 2px 深边; 按下态 (右 Alt 会话中):
        填充压暗 #262626、整体下移 1px、底部深边减为 1px。
        """
        dy = 1 if pressed else 0
        p.fillRect(QRect(lx, 88 + dy, 44, 44),
                   QColor(38, 38, 38) if pressed else QColor(45, 45, 45, 255))
        p.setPen(QColor(90, 90, 90))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(lx, 88 + dy, 44, 44)
        p.fillRect(QRect(lx + 1, 89 + dy, 42, 1), KEY_TOP_HILITE)
        p.fillRect(QRect(lx + 1, 130 + dy, 42, 1 if pressed else 2), KEY_BOTTOM_EDGE)
        p.setFont(_font(16))
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRectF(lx, 88 + dy, 44, 44), Qt.AlignmentFlag.AlignCenter, ";")

    def _draw_sort_armed(self, p: QPainter):
        """Alt+; 按住中: 灰色圆角框 + 深灰弥散盖住整块, ; 键帽居中。

        区域 = 最左侧竖分隔线左边的「; 排序」块 (4..SIDE_HINT_W-4, 14..H-14)。
        """
        block = QRectF(4, 14, SIDE_HINT_W - 8, H - 28)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(90, 90, 90, 120))            # 深灰弥散
        p.drawRect(block)
        p.setPen(QPen(QColor(158, 158, 158, 240), 2))  # 灰色直角框 (K2: 圆角→直角)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(block.left() + 0.5, block.top() + 0.5,
                          block.width() - 1, block.height() - 1))
        p.setPen(QPen(ARMED_INNER, 1))                 # K2: 1px 内亮边
        p.drawRect(QRectF(block.left() + 2.5, block.top() + 2.5,
                          block.width() - 5, block.height() - 5))
        # ; 键帽与常态共用同一绘制路径 (K2 修正: 进排序模式不跳位)
        self._draw_keycap(p, (SIDE_HINT_W - 44) // 2, self._session_visible)
        # armed 标签同位 (y=140), 提亮
        p.setFont(_font(10))
        p.setPen(QColor(255, 255, 255, 230))           # K2: armed 标签提亮
        p.drawText(QRectF(0, 140, SIDE_HINT_W, 22),
                   Qt.AlignmentFlag.AlignHCenter, "松开执行排序")

    def _draw_eq_table(self, p: QPainter):
        """右侧 EQ 预设表格 (与预览窗拼接): 当前项粉色高亮 + 频率分贝摘要。

        无表头 (2026-08-13 用户要求); 行间细分隔线; Win10 风格直角。
        """
        rx = W - EQ_TABLE_W
        if not self._eq_entries:
            p.setPen(QColor(150, 150, 150))
            p.setFont(_font(10))
            p.drawText(QRectF(rx + 14, (H - 60) // 2, EQ_TABLE_W - 28, 60),
                       Qt.AlignmentFlag.AlignVCenter, "EQ 未启用")
            return
        accent = _accent_color()
        header_h = 16                                       # E1: 表头留白 (含在居中块内)
        top = (H - (header_h + len(self._eq_entries) * EQ_ROW_H)) // 2 + header_h
        # E1: 小表头 "EQ PRESET" (随表块居中, 字距 1.5)
        p.setFont(_font(9))
        fh = p.font()
        fh.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        p.setFont(fh)
        p.setPen(QColor(255, 255, 255, 100))
        p.drawText(QRectF(rx + 18, top - header_h, EQ_TABLE_W - 36, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "EQ PRESET")
        for i, (name, summary) in enumerate(self._eq_entries):
            y = top + i * EQ_ROW_H
            current = (name == self._eq_current)
            if current:
                # 粉色高亮 + 左侧 3px 指示条 (Win10 风格)
                p.fillRect(QRect(rx + 6, y + 2, EQ_TABLE_W - 12, EQ_ROW_H - 4),
                           QColor(255, 105, 180, 190))
                p.fillRect(QRect(rx + 6, y + 2, 3, EQ_ROW_H - 4),
                           QColor(255, 140, 200, 255))
                p.setPen(QColor(255, 255, 255))
                p.setFont(_font(10, QFont.Weight.DemiBold))   # E2: 当前行预设名加粗
            else:
                p.setPen(QColor(220, 220, 220))
                p.setFont(_font(10))
            short = _EQ_SHORT.get(name, name)
            p.drawText(QRectF(rx + 18, y, 64, EQ_ROW_H),
                       Qt.AlignmentFlag.AlignVCenter, short)
            # 摘要: 等宽 9pt, 列对齐 (int 字号 — float 传 QFont 崩 Qt6Core)
            p.setFont(QFont("Consolas", 9))
            p.setPen(QColor(255, 255, 255, 250) if current else QColor(210, 210, 210))
            p.drawText(QRectF(rx + 84, y, EQ_TABLE_W - 96, EQ_ROW_H),
                       Qt.AlignmentFlag.AlignVCenter, summary)
            if current:
                # E2: 行尾 ● 指示点 (与左指示条呼应)
                p.setFont(_font(10))
                p.setPen(QColor(255, 140, 200, 255))
                p.drawText(QRectF(rx + EQ_TABLE_W - 32, y, 20, EQ_ROW_H),
                           Qt.AlignmentFlag.AlignCenter, "●")
            # D1: 行间分隔线水平渐隐 (最后一行不画)
            if i < len(self._eq_entries) - 1:
                gx = rx + 14
                grad = QLinearGradient(gx, 0, rx + EQ_TABLE_W - 14, 0)
                grad.setColorAt(0.0, QColor(255, 255, 255, 0))
                grad.setColorAt(0.1, QColor(255, 255, 255, 26))
                grad.setColorAt(0.9, QColor(255, 255, 255, 26))
                grad.setColorAt(1.0, QColor(255, 255, 255, 0))
                p.fillRect(QRect(gx, y + EQ_ROW_H - 1, EQ_TABLE_W - 28, 1), QBrush(grad))

    def _draw_slot(self, p: QPainter, idx):
        x = SIDE_HINT_W + 14 + idx * (THUMB_W + THUMB_GAP)
        y = TOP_MARGIN
        thumb, data = self._thumbs[idx], self._data[idx]
        if thumb:
            p.drawImage(QRectF(x, y, THUMB_W, THUMB_H), thumb)
            # S1: 缩略图 1px 内描边, 与面板底色切开
            p.setPen(QPen(THUMB_BORDER, 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(x + 0.5, y + 0.5, THUMB_W - 1, THUMB_H - 1))
        else:
            # S3: 空槽 — 虚线框 + 相机图标 + 引导文案 (Win10 拖拽框风格)
            p.setPen(QPen(EMPTY_DASH, 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(x + 8, y + 8, THUMB_W - 16, THUMB_H - 16))
            p.setFont(_font(20, families=["Segoe MDL2 Assets"]))
            p.setPen(EMPTY_ICON)
            p.drawText(QRectF(x, y + 38, THUMB_W, 38), Qt.AlignmentFlag.AlignCenter, "")
            p.setFont(_font(13))
            p.setPen(EMPTY_TITLE)
            p.drawText(QRectF(x, y + 78, THUMB_W, 26), Qt.AlignmentFlag.AlignCenter, "空槽")
            p.setFont(_font(9))
            p.setPen(EMPTY_HINT)
            p.drawText(QRectF(x, y + 106, THUMB_W, 20), Qt.AlignmentFlag.AlignCenter, "短按 , . / 保存布局")
        if data:
            self._draw_overlay(p, data, x, y)
        # 槽内数字提示 (右下角): 1/2/3 — Win10 直角键帽
        p.fillRect(QRect(x + THUMB_W - 30, y + THUMB_H - 24, 24, 18), QColor(20, 20, 20, 190))
        p.setPen(QColor(80, 80, 80))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(x + THUMB_W - 30, y + THUMB_H - 24, 24, 18)
        p.setFont(_font(10, QFont.Weight.Bold))
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRectF(x + THUMB_W - 30, y + THUMB_H - 24, 24, 18),
                   Qt.AlignmentFlag.AlignCenter, str(idx + 1))
        if idx + 1 == self._active:
            # S4: 激活槽 2px 实心粉框 + 外侧 1px 光晕 (Win10 2px 选中框惯例)
            p.setPen(QPen(QColor(255, 105, 180, 90), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(x - 1.5, y - 1.5, THUMB_W + 3, THUMB_H + 3))
            p.setPen(QPen(PINK, 2))
            p.drawRect(QRectF(x - 0.5, y - 0.5, THUMB_W + 1, THUMB_H + 1))
            self._draw_progress(p, x, y + THUMB_H + 6, THUMB_W)

    def _draw_overlay(self, p: QPainter, data, x, y):
        """布局示意: 按窗口记录比例画半透明矩形 + 标题。"""
        screen = QApplication.primaryScreen()
        sw, sh = screen.geometry().width(), screen.geometry().height()
        if sw <= 0 or sh <= 0:
            return
        # S2: 覆盖层用系统 Accent 色 (与 Win10 桌面拖拽选择框同源); 标题垫底色块保可读
        ac = _accent_color()
        p.setFont(_font(8))
        for r in data["windows"]:
            rx = x + r.left * THUMB_W / sw
            ry = y + r.top * THUMB_H / sh
            rw = r.width * THUMB_W / sw
            rh = r.height * THUMB_H / sh
            if rw < 4 or rh < 4:
                continue
            p.fillRect(QRectF(rx, ry, rw, rh),
                       QColor(ac.red(), ac.green(), ac.blue(), 50))
            p.setPen(QPen(QColor(ac.red(), ac.green(), ac.blue(), 130), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(rx + 0.5, ry + 0.5, rw - 1, rh - 1))
            t = QRectF(rx + 2, ry + 2, rw - 4, 14)
            p.fillRect(t, QColor(0, 0, 0, 150))
            p.setPen(QColor(255, 255, 255))
            p.drawText(t, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, r.title)

    def _draw_progress(self, p: QPainter, x, y, w):
        """进度条 = 0→LONG_HOLD_MS 全程线性映射, 40ms 起显示 (2026-08-13 调整)。

        原 600→900ms 映射: 600ms 时整条才出现, 无渐进感。
        现: hold=40ms 时前沿恰在 w*40/900 处 (非最左), 随按住平滑推进至满。
        """
        from zorder.decision import LONG_HOLD_MS
        p.fillRect(QRect(x, y, w, PROGRESS_H), QColor(255, 255, 255, 90))
        if self._hold >= 40:
            frac = min(1.0, self._hold / LONG_HOLD_MS)
            fw = int(w * frac)
            if fw > 0:
                p.fillRect(QRect(x, y, fw, PROGRESS_H), QColor(255, 105, 180, 230))


# ---------------------------------------------------------------------------
# EQ 预设面板 — 四行无边框表格, 系统色高亮当前项 + 参数摘要 (2026-08-13 用户设计)
