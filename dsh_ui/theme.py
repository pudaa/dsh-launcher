# -*- coding: utf-8 -*-
"""DSH 官方美术在 Qt 侧的转译层。

为什么需要这一层
----------------
DSH 的美术规范是 **Web 的 CSS 令牌**（`--dsw-alias-*` / `--dsh-font-*` / `--dsw-radius-*`），
而 Qt 只认样式表与像素。直接照抄 CSS 会得到"颜色对了、但气质不对"的界面：

  · Web 靠 `box-shadow` + 多层背景做层次；Qt 里叠加多层要么写 custom paint，
    要么用 1px 描边 + 底色差（官方浅色本来就是描边分层，正好对得上）。
  · Web 的 `:focus-visible` 只在键盘操作时出现；Qt 的 `:focus` 默认画一圈系统虚线，
    在中文界面上非常刺眼 —— 这里一律去掉，按键焦点改用底色差表达。
  · Web 的 `transition: .2s cubic-bezier(.4,0,.2,1)`；Qt 要自己用 QPropertyAnimation，
    时长/缓动在 `MS` / `EASE` 里给出，不要各写各的。
  · 圆角单位是 CSS px，Qt 的样式表也认 px —— **统一用 px，不用 pt**（pt 会随 DPI 漂移）。

颜色不在这里定义：唯一来源是 `dsh_panel.PALETTE_*`（逐条抄自官方令牌）。
本模块只做"令牌 → 控件"的翻译，避免第二份色表。

完整规范与转译差异见 `docs/ui-guidelines.md`。
"""
from __future__ import annotations

import os
import re

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QRectF, Qt,
                            QTimer)
from PySide6.QtGui import (QColor, QCursor, QFont, QGuiApplication, QHelpEvent,
                           QPainter, QPainterPath, QPalette, QPen, QPixmap,
                           QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                               QDialog, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLabel, QMenu, QPushButton,
                               QTextBrowser, QToolTip, QVBoxLayout, QWidget)

from dsh_ui import icons
from dsh_host import config
from dsh_ui.panel.styles import FONT, palette

#: 字号阶梯（px）。取自官方组件里出现频率最高的几档：
#: 14/22 用于设置项与正文标题，13 用于常规正文，12/18 用于说明文字，11 用于角标。
TYPE = {
    "display": (20, 28, 600),
    "h1": (17, 24, 600),
    "h2": (14, 22, 600),
    "body": (13, 20, 400),
    "small": (12, 18, 400),
    "caption": (11, 16, 400),
}

#: 官方圆角（--dsw-radius-*）
RADIUS = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 20, "panel": 28}

#: 官方间距节奏：4 的倍数
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}

#: 官方动效（--ds-transition-duration*）
MS = {"fast": 100, "base": 200, "slow": 300}

#: 与官方 `cubic-bezier(.4, 0, .2, 1)` 最接近的 Qt 缓动
EASE = QEasingCurve.Type.OutCubic


def palette_now() -> dict:
    """当前主题的调色板。

    主题判定的唯一权威是主窗口标题栏取色（写入 `config.last_theme_dark`），
    这里只读，不自己判断深浅。
    """
    return palette("dark" if bool(config.get("last_theme_dark", True)) else "light")


# ------------------------------------------------------------------ 样式表

def combo_qss(pal: dict) -> str:
    """下拉框与它的弹出列表。

    为什么单独成段（2026-11-13 真机反馈）
    ------------------------------------
    「添加远程主机」里点开"断开后远端服务"，弹出来的是**一整块黑**。
    QComboBox 的 popup 是独立的顶层窗口（`QComboBoxPrivateContainer`），
    `QDialog { background: ... }` 管不到它，QWindows11Style 又按系统调色板
    去画——应用是浅色令牌、弹层是系统色，于是文字和底一起沉进黑里。

    所以三处都要给死：下拉框本体、列表视图、以及包着视图的那个顶层容器。
    颜色全部来自同一份 palette，不另起色表。
    """
    return """
QComboBox {
    background: %(card)s; color: %(text)s; border: 1px solid %(line)s;
    border-radius: %(r_sm)spx; padding: 7px 28px 7px 10px;
    font-family: %(font)s; font-size: 13px; min-height: 16px;
}
QComboBox:hover      { border-color: %(muted)s; }
QComboBox:focus      { border-color: %(accent)s; outline: none; }
QComboBox:disabled   { color: %(muted)s; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: top right;
    width: 26px; border: none; background: transparent;
}
/* 箭头由 style_combo() 挂的自绘子控件画，这里只给 drop-down 留出位置。
   Qt 的 ::down-arrow 写 border 会变成实心小方块、留空则什么都不画；
   image: url() 又依赖外部文件，打包成单文件后会静默失效。 */
QComboBox QAbstractItemView {
    background: %(card)s; color: %(text)s;
    border: 1px solid %(line)s; border-radius: %(r_sm)spx;
    padding: 4px; outline: none;
    selection-background-color: %(nav_hover)s; selection-color: %(text)s;
}
QComboBox QAbstractItemView::item {
    min-height: 26px; padding: 5px 8px;
    border-radius: %(r_xs)spx; background: transparent; color: %(text)s;
}
QComboBox QAbstractItemView::item:selected {
    background: %(nav_hover)s; color: %(text)s;
}
QComboBox QAbstractItemView::item:hover { background: %(hover)s; }
""" % {
        "card": pal["card"], "text": pal["text"], "dim": pal["dim"],
        "muted": pal["muted"], "line": pal["line"], "accent": pal["accent"],
        "hover": pal["hover"], "nav_hover": pal["nav_hover"],
        "font": FONT, "r_sm": RADIUS["sm"], "r_xs": RADIUS["xs"],
    }


def popup_edge(pal: dict) -> str:
    """弹层描边：把 `dim` 以 18% 压到 `card` 上，得到一条看得见的细灰线。

    全局 `line` 是 10% 黑（白底上 ≈ #e8e8e8），叠在白对话框上几乎分不出
    边界（老大 2026-11-13 指出下拉列表"缺少边框"）。这里只在弹层上加深
    一档，仍是同一套令牌派生，没有另起色表。
    """
    return _solid("#2e%s" % pal["dim"].lstrip("#"), pal["card"])


def combo_view_qss(pal: dict) -> str:
    """直接设在 `combo.view()` 上的样式：卡片底、细边框、圆角。

    popup 是顶层窗口，`QComboBox QAbstractItemView` 这类后代选择器未必能
    穿过它；把规则贴在视图本身，任何 Qt 风格下都有确定的底色与描边。
    """
    return """
QListView, QAbstractItemView {
    background: %(card)s; color: %(text)s;
    border: 1px solid %(edge)s; border-radius: %(r_sm)spx;
    padding: 4px; outline: none;
    selection-background-color: %(nav_hover)s; selection-color: %(text)s;
}
QListView::item, QAbstractItemView::item {
    min-height: 26px; padding: 5px 8px;
    border-radius: %(r_xs)spx; background: transparent; color: %(text)s;
}
QListView::item:selected, QAbstractItemView::item:selected {
    background: %(nav_hover)s; color: %(text)s;
}
QListView::item:hover, QAbstractItemView::item:hover { background: %(hover)s; }
/* 项多到需要滚动时也走令牌：系统滚动条的箭头/翻页区在浅色下会画成
   黑色方块（2026-11-13 老大实机截图里那一坨）。 */
QScrollBar:vertical { background: transparent; width: 6px; margin: 2px; }
QScrollBar::handle:vertical {
    background: %(scroll)s; border-radius: 3px; min-height: 24px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
""" % {
        "card": pal["card"], "text": pal["text"], "hover": pal["hover"],
        "nav_hover": pal["nav_hover"], "edge": popup_edge(pal),
        "scroll": pal["scroll"], "r_sm": RADIUS["sm"],
        "r_xs": RADIUS["xs"],
    }


class _ComboPopupGuard(QObject):
    """下拉弹层的边框与阴影守卫。

    初始化顺序有讲究：`apply()` 里的 `setWindowFlags()` 会重建本机窗口并
    触发 Resize/Polish 等事件，**可能在 `__init__` 返回前就回调到
    `eventFilter`**。真机 1 倍缩放时就是这样：字段还没设完，回调先到，
    `AttributeError` 被 Qt 吞掉（没有控制台），守卫初始化中断 —— 留白、
    投影、加高全都没装上，表现为系统滚动条 + 最后一项被截。所以这里
    先装过滤器、字段全部有类级默认值，再走 apply()。

    为什么需要（2026-11-13 老大审验反馈）
    ------------------------------------
    Qt 给 `QComboBoxPrivateContainer` 留的是 **DWM 系统窗口阴影**：右下
    一条实色硬边，和官方"温和底部投影"完全两种气质。它跟托盘菜单那次的
    `QGraphicsDropShadowEffect` 不是同一路径（探针实测此处 effect=None、
    `NoDropShadowWindowHint` 未设），但处理原则一样 —— 关掉系统阴影，
    自己按令牌画。

    容器改成透明 + 四周留白，view 自己画细边框/圆角/card 底；投影用低
    alpha 的 `QGraphicsDropShadowEffect` 装在容器上，留白就是它的绘制空间，
    不会再被窗口边缘硬裁。

    还有一个坑：Qt 按 view 的原始高度算 popup 尺寸，layout 一旦有上下
    margin，最后一项就会被吃掉（试验图里第三项只剩半行）。Show 时把容器
    加高 margin 的那一段，view 才能保持原高度。
    """

    MARGINS = (10, 6, 10, 12)       # 左 上 右 下；底部多留给投影
    #: 官方下拉是 CSS `0 4px 12px rgba(0,0,0,.08)` 那一档：很淡、范围小。
    #: 早先 blur 14 / alpha 34 在 1x 下能看出十几像素的灰雾，偏重。
    BLUR = 10
    OFFSET_Y = 2

    #: 类级默认值：eventFilter 可能早于 __init__ 完成被调用（见类说明）。
    _popup = None
    _base_h = None
    _pal = None
    _effect = None

    def __init__(self, popup: QWidget, pal: dict):
        super().__init__(popup)      # parent 是容器，随它一起回收
        self._popup = popup
        self._base_h = None
        self._pal = pal
        self._effect = None
        # 先装过滤器：apply() 里的 setWindowFlags 会触发窗口重建事件。
        popup.installEventFilter(self)
        self.apply(pal)

    def _effect_now(self) -> QGraphicsDropShadowEffect:
        """拿一个可用的投影对象；失效就重建。

        为什么不能缓存死（2026-11-13，PySide6 6.10.1 实测）：`setWindowFlags`
        重建本机窗口时，Qt 会把挂在窗口上的 effect 一起删掉，Python 侧
        引用还在。下一次 `apply()` 访问它就抛
        `RuntimeError: Internal C++ object already deleted`，异常被 Qt 吞掉，
        加高/描边整条链路静默失效 —— 表现就是系统滚动条 + 最后一项截断。
        6.11 不复现，所以之前三个解释器里只有 6.10.1 出问题。
        """
        eff = self._effect
        if eff is not None:
            try:
                eff.blurRadius()     # 探活：已删对象在这里抛 RuntimeError
                return eff
            except RuntimeError:
                pass
        eff = QGraphicsDropShadowEffect(self._popup)
        self._effect = eff
        return eff

    def _apply_palette(self) -> None:
        """把容器调色板按令牌上死，兜住"某种风格下 QSS 没穿透"的情形。

        `QComboBox::showPopup()` 内部会把容器 palette 重设成系统灰
        （实测 #efefef），所以 Show 事件里要再写一次；只设一次是无效的。
        """
        app = QApplication.instance()
        p = QPalette(app.palette() if app is not None else QPalette())
        card, text = QColor(self._pal["card"]), QColor(self._pal["text"])
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base,
                     QPalette.ColorRole.AlternateBase):
            p.setColor(role, card)
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                     QPalette.ColorRole.HighlightedText):
            p.setColor(role, text)
        p.setColor(QPalette.ColorRole.Highlight,
                   QColor(self._pal["nav_active"]))
        self._popup.setPalette(p)

    def apply(self, pal: dict) -> None:
        pop = self._popup
        self._pal = pal
        # 必须整组 OR 后写入：setWindowFlag(..., True) 对这类私有容器无效，
        # 托盘菜单的 apply_menu 里踩过同一个坑。
        want = (Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.NoDropShadowWindowHint)
        flags = pop.windowFlags()
        if (flags & want) != want:
            # setWindowFlags 会重建本机窗口，之前设过的 palette 会被冲掉。
            pop.setWindowFlags(flags | want)
        self._apply_palette()
        pop.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        pop.setAutoFillBackground(False)
        pop.setStyleSheet("QWidget { background: transparent; border: none; }")
        lay = pop.layout()
        if lay is not None:
            lay.setContentsMargins(*self.MARGINS)
        dark = QColor(pal["card"]).lightness() < 128
        eff = self._effect_now()
        eff.setBlurRadius(self.BLUR)
        eff.setOffset(0, self.OFFSET_Y)
        ink = QColor(0, 0, 0)
        ink.setAlpha(70 if dark else 26)
        eff.setColor(ink)
        if pop.graphicsEffect() is not eff:
            pop.setGraphicsEffect(eff)

    def _pad(self) -> None:
        pop = self._popup
        # 只在真正可见、且已按 view 摆好时记录基准高度：apply() 期间也会
        # 收到事件，那时容器还停在 100x30，拿它当基准会让每次加高都少一截。
        if pop is None or not pop.isVisible() or pop.height() < 60:
            return
        lay = pop.layout()
        extra = (lay.contentsMargins().top() + lay.contentsMargins().bottom()
                 if lay is not None else self.MARGINS[1] + self.MARGINS[3])
        if self._base_h is None:
            self._base_h = pop.height()
        target = self._base_h + extra
        if pop.height() != target:
            pop.resize(pop.width(), target)

    def eventFilter(self, obj, ev) -> bool:                    # noqa: N802
        if self._popup is None or obj is not self._popup:
            return False
        try:
            kind = int(ev.type())
            if kind in (int(QEvent.Type.Show), int(QEvent.Type.Polish)):
                # QWindows11Style 可能在 polish() 时装回自己的投影，覆盖回来。
                eff = self._effect_now()
                if self._popup.graphicsEffect() is not eff:
                    self._popup.setGraphicsEffect(eff)
                if kind == int(QEvent.Type.Show):
                    self._apply_palette()   # showPopup 把 palette 改成系统灰
                    self._pad()
                    # Qt 在 show 之后还可能按 view 的 sizeHint 再摆一次，
                    # 所以下一拍补一次加高（真机 1x 下这一步是必需的）。
                    QTimer.singleShot(0, self._pad_later)
        except (RuntimeError, AttributeError, TypeError, ValueError):
            return False
        return False

    def _pad_later(self) -> None:
        try:
            if self._popup is not None and self._popup.isVisible():
                self._pad()
        except RuntimeError:
            pass


def _combo_arrow_pixmap(pal: dict) -> QPixmap:
    """下拉箭头：自绘一个 filled triangle，ink 用 dim 令牌。

    不用 `::down-arrow` 的 border 三角（Qt 会画成实心小方块），也不用
    `image: url(...)`（打包成单文件后外部资源会静默失效）。这是一张运行时
    生成的位图，没有路径、没有文件、跟着传入的 palette 走。
    """
    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.moveTo(3.0, 5.5)
    path.lineTo(11.0, 5.5)
    path.lineTo(7.0, 9.5)
    path.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(pal["dim"]))
    p.drawPath(path)
    p.end()
    return pm


class _ComboArrow(QObject):
    """贴在下拉框右侧的箭头，随 combo 尺寸与主题更新。

    为什么用子控件：QSS 接管 QComboBox 之后，`::down-arrow` 无论写 border
    还是留空都画不出正常的三角（实测分别是灰方块和空白）。子 QLabel 完全
    受我们控制，且 `WA_TransparentForMouseEvents` 保证点击仍然落在 combo 上。
    """

    def __init__(self, combo: QComboBox, pal: dict):
        super().__init__(combo)          # parent 是 combo，随它一起回收
        self._combo = combo
        self._pal = pal
        self.label = QLabel(combo)
        self.label.setObjectName("comboArrow")
        self.label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.label.setStyleSheet("background: transparent; border: none;")
        self.label.setFixedSize(14, 14)
        self.set_palette(pal)
        combo.installEventFilter(self)
        self._place()

    def set_palette(self, pal: dict) -> None:
        self._pal = pal
        self.label.setPixmap(_combo_arrow_pixmap(pal))

    def _place(self) -> None:
        self.label.move(self._combo.width() - 24,
                        max(0, (self._combo.height() - 14) // 2))
        self.label.raise_()

    def _ensure_popup(self) -> None:
        """打开下拉后，确保当前容器真的挂上了守卫并完成加高。

        为什么不能只靠 Show 事件（2026-11-13 实测）：PySide6 **6.10.1**
        下弹层容器的 Show/Polish 不会到达我们装在它身上的过滤器，`_pad`
        一次都不执行 —— 表现就是系统滚动条 + 最后一项截断。6.11 正常。
        改成点击/按键之后主动补一次，跨版本都成立。
        """
        combo = self._combo
        view = combo.view()
        popup = view.parentWidget() if view is not None else None
        if popup is None or not popup.isVisible():
            return
        guard = getattr(combo, "_dsh_popup_guard", None)
        if guard is None or getattr(guard, "_popup", None) is not popup:
            guard = _ComboPopupGuard(popup, self._pal)
            combo._dsh_popup_guard = guard
        else:
            guard.apply(self._pal)
        guard._pad()
        # Qt 6.10 在 showPopup 收尾时还会按 sizeHint 再摆一次，隔一拍补。
        QTimer.singleShot(30, guard._pad_later)

    def eventFilter(self, obj, ev) -> bool:                    # noqa: N802
        if obj is not self._combo:
            return False
        kind = int(ev.type())
        if kind == int(QEvent.Type.Resize):
            self._place()
        elif kind in (int(QEvent.Type.MouseButtonPress),
                      int(QEvent.Type.KeyPress)):
            QTimer.singleShot(0, self._ensure_popup)
        return False


def style_combo(combo: QComboBox, pal: dict) -> None:
    """给一个下拉框套令牌，并接管它那个顶层弹层容器的外观。

    `view().parentWidget()` 是 `QComboBoxPrivateContainer`。配色按 palette
    上死（防 Win11 系统调色板把弹层画成全黑），边框/圆角/投影交给
    `_ComboPopupGuard`：容器透明，卡片由 view 自绘，所以不会出现"半透明
    又没自绘"那种黑块。
    """
    # combo 自己的文字/底色也要跟主题（下拉箭头另由自绘子控件负责）。
    cp = combo.palette()
    card, text = QColor(pal["card"]), QColor(pal["text"])
    cp.setColor(QPalette.ColorRole.Window, card)
    cp.setColor(QPalette.ColorRole.Base, card)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        cp.setColor(role, text)
    combo.setPalette(cp)

    arrow = getattr(combo, "_dsh_arrow", None)
    if arrow is None:
        combo._dsh_arrow = _ComboArrow(combo, pal)
    else:
        arrow.set_palette(pal)
        arrow._place()

    view = combo.view()
    if view is not None:
        view.setStyleSheet(combo_view_qss(pal))
        popup = view.parentWidget()
        if popup is not None:
            # 调色板、窗口标志、透明底、投影统一由守卫设置（顺序有讲究，
            # setWindowFlags 会重建窗口并把 palette 冲掉）。
            guard = getattr(combo, "_dsh_popup_guard", None)
            if guard is None:
                guard = _ComboPopupGuard(popup, pal)
                combo._dsh_popup_guard = guard
            else:
                guard.apply(pal)
            # 两边都留引用：popup 上的是自检/兼容用，combo 上的是"换容器后
            # 还能找回来"的那一份（Qt 6.10 的 Show 事件送不到守卫）。
            popup._dsh_popup_guard = guard


def dialog_qss(pal: dict) -> str:
    """对话框 / 提示窗的样式。

    字号一律 px；`QPushButton` 的默认形态是"次要按钮"，主操作由 `#primary` 指定，
    与官方「一个主按钮 + 若干幽灵按钮」的排布一致。
    """
    return """
QDialog { background: %(bg)s; }
QLabel { color: %(text)s; font-family: %(font)s; font-size: 13px; }
QLabel#dlgTitle { color: %(text)s; font-family: %(font)s;
                  font-size: 15px; font-weight: 600; }
QLabel#dlgBody { color: %(dim)s; font-family: %(font)s; font-size: 13px; }
QLabel#dlgHint { color: %(muted)s; font-family: %(font)s; font-size: 12px; }

QPushButton {
    color: %(text)s; background: %(btn_fill)s; border: 1px solid %(line)s;
    border-radius: %(r_sm)spx; padding: 6px 14px;
    font-family: %(font)s; font-size: 13px;
}
QPushButton:hover { background: %(btn_hover)s; }
QPushButton:disabled { color: %(muted)s; }
/* 官方在指针操作下不画焦点环（input-modality=pointer 时 outline 透明） */
QPushButton:focus { outline: none; }

QPushButton#primary {
    color: %(primary_text)s; background: %(primary_fill)s; border: none;
    border-radius: %(r_sm)spx; padding: 7px 16px;
    font-family: %(font)s; font-size: 13px; font-weight: 600;
}
QPushButton#primary:hover { background: %(primary_hover)s; }
QPushButton#primary:disabled { color: %(muted)s; background: %(hover)s; }

QPlainTextEdit, QLineEdit {
    background: %(card)s; color: %(text)s; border: 1px solid %(line)s;
    border-radius: %(r_sm)spx; padding: 8px;
    font-family: %(mono)s; font-size: 12px;
    selection-background-color: %(nav_active)s; selection-color: %(text)s;
}
QTextBrowser {
    background: %(card)s; color: %(text)s; border: 1px solid %(line)s;
    border-radius: %(r_sm)spx; padding: 8px;
    font-family: %(font)s; font-size: 13px;
    selection-background-color: %(nav_active)s; selection-color: %(text)s;
}
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: %(scroll)s; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
""" % {
        "bg": pal["bg"], "text": pal["text"], "dim": pal["dim"],
        "muted": pal["muted"], "line": pal["line"], "card": pal["card"],
        "hover": pal["hover"], "btn_fill": pal["btn_fill"],
        "btn_hover": pal["btn_hover"], "primary_fill": pal["primary_fill"],
        "primary_hover": pal["primary_hover"], "primary_text": pal["primary_text"],
        "nav_active": pal["nav_active"], "scroll": pal["scroll"],
        "font": FONT, "mono": '"Cascadia Mono", "Consolas", monospace',
        "r_sm": RADIUS["sm"],
    } + combo_qss(pal)


# ------------------------------------------------------------------ 更新说明

def _solid(argb: str, bg: str) -> str:
    """把带 alpha 的令牌合成到不透明底色上，返回 #RRGGBB。

    QTextDocument 的 HTML CSS 不认 Qt 的 #AARRGGBB：`#1a000000` 会被当成
    RGB 解析成近黑色，表格/描边因此变粗。富文本里统一用预混合的实色，
    与面板 QSS 的观感保持一致。
    """
    c, b = QColor(argb), QColor(bg)
    a = c.alphaF()
    return "#%02x%02x%02x" % (
        round(c.red() * a + b.red() * (1 - a)),
        round(c.green() * a + b.green() * (1 - a)),
        round(c.blue() * a + b.blue() * (1 - a)),
    )


def notes_css(pal: dict) -> str:
    """QTextDocument 的 HTML 样式（更新说明用）。

    QSS 作用在 QTextBrowser 的**控件**上，管不到文档里的 h1/p/li；这里用
    QTextDocument 的 defaultStyleSheet 补上元素样式。颜色全部取自同一套令牌，
    不另起色表。
    """
    return """
body { color: %(text)s; font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
       font-size: 13px; }
h1 { font-size: 17px; font-weight: 600; margin: 10px 0 6px 0; }
h2 { font-size: 15px; font-weight: 600; margin: 10px 0 6px 0; }
h3 { font-size: 14px; font-weight: 600; margin: 8px 0 4px 0; }
p { margin: 4px 0; }
ul, ol { margin: 4px 0 4px 18px; }
li { margin: 2px 0; }
a { color: %(accent)s; text-decoration: none; }
code { font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px;
       background-color: %(hover)s; }
pre { font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px;
      background-color: %(hover)s; border: 1px solid %(line)s; padding: 6px;
      white-space: pre-wrap; }
blockquote { color: %(dim)s; border-left: 3px solid %(line)s;
             margin: 6px 0 6px 0; padding-left: 10px; }
table { border-collapse: collapse; }
th, td { border: 1px solid %(line)s; padding: 4px 8px; }
th { background-color: %(hover)s; font-weight: 600; }
hr { border: none; border-top: 1px solid %(line)s; }
""" % {
        "text": pal["text"], "dim": pal["dim"],
        "line": _solid(pal["line"], pal["card"]),
        "hover": _solid(pal["hover"], pal["card"]),
        "accent": pal["accent"],
    }


def _clean_html(text: str) -> str:
    """去掉脚本 / 内嵌框架 / 远程图片。

    Atom feed 的 content 是 HTML，Qt 只认子集；script/style/iframe 不会执行，
    但 img 可能触发远端请求，这里直接剔除，避免打开说明窗口时联网。
    """
    text = re.sub(r"<\s*(script|style|iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>",
                  "", text or "", flags=re.I | re.S)
    text = re.sub(r"<\s*(script|style|iframe|object|embed|img)\b[^>]*/?>",
                  "", text, flags=re.I)
    return text


def _style_markdown_html(html: str, pal: dict) -> str:
    """把 Qt 的 markdown 输出改成可被 notes_css 接管的 HTML。

    Qt 会在生成的标签里塞内联 style（行距、缩进、Courier New、链接色），
    内联样式优先级高于 defaultStyleSheet，所以块级 style 要剥掉；
    span 上的 font-weight（粗体/标题）保留，只去掉标题的 font-size，
    让 h1/h2/h3 回到我们自己的字号阶梯。
    """
    html = re.sub(r'(<(?:p|h[1-6]|pre|blockquote|ul|ol|li|table|tr|td|th)\b[^>]*?)\s+style="[^"]*"',
                  r"\1", html, flags=re.I)
    # 标题 span 带 x-large/large；去掉字号，保留 font-weight。
    html = re.sub(r'(<h[1-6]\b[^>]*>\s*<span\b[^>]*?)\s*font-size:[^;"]+;?',
                  r"\1", html, flags=re.I)
    html = html.replace("Courier New", "Cascadia Mono")
    html = re.sub(r"<img\b[^>]*>", "", html, flags=re.I)
    # Qt 把代码块每一行都写成独立的 <pre>；合并成一个，底色才不会有横条缝隙。
    html = re.sub(r"</pre>\s*<pre\b[^>]*>", "\n", html, flags=re.I)
    html = re.sub(r"<table\b[^>]*>", "<table>", html, flags=re.I)

    def fix_link(match):
        return re.sub(r"color:\s*#[0-9a-fA-F]{3,8};?",
                      "color:%s;" % pal["accent"], match.group(0), flags=re.I)

    return re.sub(r"<a\b[^>]*>.*?</a>", fix_link, html, flags=re.I | re.S)


def _is_mono(font: QFont) -> bool:
    family = (font.family() or "").lower()
    return any(k in family for k in ("cascadia", "consolas", "courier", "mono"))


def _block_is_code(block) -> bool:
    """块内所有 fragment 都是等宽字体 → 这是一个代码块（不是行内代码）。"""
    it = block.begin()
    seen = False
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid() and frag.length():
            seen = True
            if not _is_mono(frag.charFormat().font()):
                return False
        it += 1
    return seen


def _polish_notes_document(doc, pal: dict) -> None:
    """渲染后的收尾：标题字号、代码块底色、行内代码底色。

    Qt 的 HTML importer 不认 defaultStyleSheet 里的 h* font-size，
    但会保留 blockFormat().headingLevel()；这里按层级合并字号。
    `pre` 的 CSS background 同样不画，改用 QTextBlockFormat 铺底色。
    """
    code_bg = QColor(_solid(pal["hover"], pal["card"]))
    heading_px = {1: 17, 2: 15, 3: 14}
    block = doc.begin()
    while block.isValid():
        level = block.blockFormat().headingLevel()
        if level:
            it = block.begin()
            if not it.atEnd():
                base = it.fragment().charFormat()
                font = QFont(base.font())
                font.setPixelSize(heading_px.get(level, 14))
                # headingLevel 会让 QTextDocument 再按 1.5x/1.17x 放大一次；
                # 清掉它，字号才完全由上面这张表决定。
                bf = block.blockFormat()
                bf.setHeadingLevel(0)
                bf.setTopMargin(8 if level <= 2 else 6)
                bf.setBottomMargin(3)
                cursor = QTextCursor(block)
                cursor.setBlockFormat(bf)
                # 不能从 base 复制：它带着 FontSize/pointSize 的 resolve
                # 标记，setFont() 里的 pixelSize 会被旧字号盖掉。这里只
                # 复制颜色与字重，字号用全新设置。
                fmt = QTextCharFormat()
                fmt.setForeground(base.foreground())
                fmt.setFontWeight(base.fontWeight())
                fmt.setFontItalic(base.fontItalic())
                fmt.setFont(font)
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                cursor.setCharFormat(fmt)
        else:
            if _block_is_code(block):
                bf = block.blockFormat()
                bf.setBackground(code_bg)
                # 一个 <pre> 里的多行会被 Qt 拆成多个 block；上下 margin
                # 必须为 0，否则每行之间会露出白缝，看起来像一条条横条。
                bf.setTopMargin(0)
                bf.setBottomMargin(0)
                bf.setLeftMargin(6)
                bf.setRightMargin(6)
                cursor = QTextCursor(block)
                cursor.setBlockFormat(bf)
            else:
                # 行内代码：只给等宽 fragment 铺底色，不整行铺
                it = block.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    if frag.isValid() and frag.length() and _is_mono(
                            frag.charFormat().font()):
                        cursor = QTextCursor(doc)
                        cursor.setPosition(frag.position())
                        cursor.setPosition(frag.position() + frag.length(),
                                           QTextCursor.MoveMode.KeepAnchor)
                        fmt = QTextCharFormat()
                        fmt.setBackground(code_bg)
                        cursor.mergeCharFormat(fmt)
                    it += 1
        block = block.next()


class NotesView(QTextBrowser):
    """更新说明 / 进度的富文本视图。

    说明可能是 GitHub 的 markdown（API）或 HTML（Atom 兜底），
    QPlainTextEdit 会把 `##` / `<p>` 原样打出来；这里统一渲染成
    DSH 的排版，再把进度行以纯文本追加进来。

    为什么用 QTextBrowser 而不是 QTextEdit：前者默认只读、支持链接点击。
    """

    def __init__(self, parent=None, pal: dict | None = None):
        super().__init__(parent)
        p = pal or palette_now()
        self._pal = p
        self._css = notes_css(p)
        self._font = QFont()
        self._font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"])
        self._font.setPixelSize(13)
        # QTextBrowser 的链接色取 palette，不取 HTML 的 a { color }
        link_palette = self.palette()
        link_palette.setColor(QPalette.ColorRole.Link, QColor(p["accent"]))
        self.setPalette(link_palette)
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDefaultStyleSheet(self._css)
        self.document().setDefaultFont(self._font)
        self.document().setDocumentMargin(6)
        self.setPlaceholderText("更新说明会显示在这里")

    def show_notes(self, text: str, fmt: str = "markdown") -> None:
        """渲染说明。fmt: markdown / html / text；空内容显示明确提示。"""
        text = (text or "").strip()
        doc = self.document()
        # setHtml / setPlainText 会重建文档内容，但保留 defaultStyleSheet/Font；
        # 这里每次再写一遍，避免调用方换过 document。
        doc.setDefaultStyleSheet(self._css)
        doc.setDefaultFont(self._font)
        if not text:
            self.setPlainText("该版本没有附带更新说明。")
        elif fmt == "html":
            self.setHtml(_clean_html(text))
            _polish_notes_document(self.document(), self._pal)
        elif fmt == "markdown":
            # setMarkdown 生成的是带内联样式的 HTML；转一道手，剥掉块级 style，
            # 让 notes_css 的字号/间距/代码底色真正生效。
            doc.setMarkdown(text)
            self.setHtml(_style_markdown_html(doc.toHtml(), self._pal))
            _polish_notes_document(self.document(), self._pal)
        else:
            self.setPlainText(text)
        self.verticalScrollBar().setValue(0)

    def append_line(self, text: str) -> None:
        """在文档末尾追加一行纯文本（进度行用）。

        为什么需要这个方法（2026-10-20 实测事故）
        ----------------------------------------
        这是 `QTextBrowser`，**没有** `QPlainTextEdit` 的 `appendPlainText()`。
        2.3.1 把更新对话框的日志控件从 `QPlainTextEdit` 换成富文本视图时，
        `self.log.appendPlainText` 的接线没跟着改：

            AttributeError: 'NotesView' object has no attribute 'appendPlainText'

        而这个调用发生在**按钮已经禁用之后**，异常又被 Qt 吞在槽里（无控制台），
        用户看到的就是"点了下载并更新→进入更新状态→一个字的进度都没有"。
        用 `hasattr` 自查过：QTextBrowser 只有 `append()`，且它会按 HTML 解析，
        进度文本里的 `%`、`<` 之类有被当标记吃掉的风险，所以不用它。
        """
        cur = QTextCursor(self.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        if not self.document().isEmpty():
            cur.insertBlock()
        fmt = QTextCharFormat()
        fmt.setFont(self._font)
        fmt.setForeground(QColor(self._pal["text"]))
        cur.insertText(text, fmt)
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


def menu_qss(pal: dict) -> str:
    """托盘菜单：官方下拉菜单的形态（卡片底 + 1px 描边 + 6px 内边距 + 圆角）。"""
    return """
QMenu {
    background: %(card)s; border: 1px solid %(line)s;
    border-radius: %(r_md)spx; padding: 6px;
    color: %(text)s; font-family: %(font)s; font-size: 13px;
}
QMenu::item {
    padding: 7px 22px 7px 12px; border-radius: %(r_sm)spx;
    color: %(text)s;
}
QMenu::item:selected { background: %(nav_hover)s; }
QMenu::item:disabled { color: %(muted)s; }
QMenu::separator { height: 1px; background: %(line)s; margin: 6px 10px; }
QMenu::indicator { width: 14px; height: 14px; }
""" % {
        "card": pal["card"], "line": pal["line"], "text": pal["text"],
        "muted": pal["muted"], "nav_hover": pal["nav_hover"],
        "font": FONT, "r_md": RADIUS["md"], "r_sm": RADIUS["sm"],
    }


def strip_shadow(widget) -> bool:
    """移除 QWindows11Style 给菜单装的、会被窗口裁切的投影。

    只处理 `QGraphicsDropShadowEffect`：效果是窗口内绘制的，窗口和菜单同大，
    所以 offset(3,3)+blur(3) 的阴影只会在右/下露出 3~6px 的硬边。
    返回是否真的移除了东西；没有效果时返回 False（不是错误）。
    """
    effect = widget.graphicsEffect()
    if isinstance(effect, QGraphicsDropShadowEffect):
        widget.setGraphicsEffect(None)
        return True
    return False


def apply_menu(menu, pal: dict | None = None) -> None:
    """给 QMenu 套上官方形态，并去掉 Windows 11 自带的裁切阴影。

    真正的阴影来源（2026-10-19 实测）
    --------------------------------
    不是 DWM，也不受 `NoDropShadowWindowHint` 控制，而是 **QWindows11Style
    在 `polish()` 里给每个 QMenu 装了一个 `QGraphicsDropShadowEffect`**
    （blur 3、offset 3,3、#3f3f3f/180）。效果画在窗口**内部**，而 QMenu 的
    窗口只和菜单一样大 —— 于是阴影在右侧/下侧被窗口边缘硬生生裁断，
    正是托盘菜单右下那条"被容器切割的阴影"。

    Qt 的 `polish()` 在 show 时才跑，并且 `setStyleSheet()` 也会触发
    repolish（等于每次套主题都会重新装上）。所以这里做三步：
      1. 标志位 + 半透明底（圆角前提，跨非 Win11 风格也成立）；
      2. 套完样式马上清一次（覆盖 repolish）；
      3. `aboutToShow` 再清一次（覆盖第一次 show 的 polish）。
    `dsh_theme.Menu` 还在 `showEvent` 里兜底，见该类说明。
    """
    pal = pal or palette_now()
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    # 注意：`setWindowFlag(NoDropShadowWindowHint, True)` 对 QMenu **无效**
    # （实测标志位没变），必须整组 OR 后用 setWindowFlags 写入。
    # 只在缺标志时写：重复 setWindowFlags 会重建窗口——菜单正开着的时候
    # 切主题会把它关掉。
    want = Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
    flags = menu.windowFlags()
    if (flags & want) != want:
        menu.setWindowFlags(flags | want)
    menu.setStyleSheet(menu_qss(pal))
    # setStyleSheet() 会触发 QWindows11Style::polish()，阴影当场被重新装上。
    strip_shadow(menu)

    if not getattr(menu, "_dsh_shadow_hooked", False):
        menu._dsh_shadow_hooked = True
        # aboutToShow 早于 show/paint，而 polish 已经在此前跑完 —— 直接清即可，
        # 不能再用 singleShot(0)：那一拍可能已经画完第一帧（闪一下）。
        menu.aboutToShow.connect(lambda m=menu: strip_shadow(m))


class Menu(QMenu):
    """托盘 / 右键菜单：创建即套主题，并保证每次显示都没有 Qt 的裁切阴影。

    为什么需要子类：`QWindows11Style::polish()` 在 **show 时**才装阴影，
    而 `apply_menu` 通常在 `__init__` 里就调完了。`showEvent` 在窗口已创建、
    第一次 paint 之前执行，是清掉它的确定时机；这样即便将来有人用
    `menu.show()`（不经过 `aboutToShow`）也不会把阴影带出来。
    """

    def __init__(self, parent=None, pal: dict | None = None):
        super().__init__(parent)
        apply_menu(self, pal)

    def showEvent(self, event) -> None:                     # noqa: N802 (Qt 命名)
        super().showEvent(event)
        strip_shadow(self)


def style_dialog(widget: QWidget, pal: dict | None = None) -> None:
    """给对话框套上统一样式。子控件靠 objectName 取到标题/正文/提示三种字级。

    下拉框要额外走一遍 `style_combo()`：它的弹层是顶层窗口，样式表不一定
    能穿透过去，必须把视图和容器都按当前 palette 上色。
    """
    p = pal or palette_now()
    widget.setStyleSheet(dialog_qss(p))
    for combo in widget.findChildren(QComboBox):
        style_combo(combo, p)


# ------------------------------------------------------------------ 工具提示
#
# 原生 QToolTip 是一块**独立顶层窗口**，不吃任何 QWidget 的样式表：Windows
# 上永远是浅灰底 + 1px 黑框 + 系统字体，和官方令牌完全没关系（老大截图里
# 「自动安装」旁边那块）。应用级 QSS 只能改它的底色和边框，圆角、行距、
# 富文本宽度都改不动。所以这里接管 QEvent.ToolTip，用一张自绘卡片显示，
# 颜色、圆角、字号全部取自同一份 palette，切主题时下一次显示就跟着变。
#
# 同一个坑在两个地方出现过：下拉列表弹层也是顶层窗口，被系统调色板接管
# 就变全黑（见 combo_qss）。处理原则一致 —— 不让弹层自己决定颜色。

_TIP_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_TIP_BR = re.compile(r"<br\s*/?>", re.I)

# 生命周期照抄 Qt 6.11 的 QTipLabel（qtbase/src/widgets/kernel/qtooltip.cpp）：
#   · MouseMove 移出锚点矩形 / Leave → hideTip()，即**单次 300 ms** 延迟隐藏
#   · 点击 / 滚轮 / 按键 → hideTipImmediately()
#   · 过期用**单次** 10000 + 40×(字数−100) ms
# 没有周期轮询。Tool 窗口首次 show 时宿主会收到一轮 Leave/FocusOut 误报，
# 靠「延迟到期时复核 QCursor.pos()」挡住，而不是靠忽略事件。
_TIP_LEAVE = int(QEvent.Type.Leave)
_TIP_MOUSE_MOVE = int(QEvent.Type.MouseMove)
_TIP_SOFT = frozenset(int(t) for t in (
    QEvent.Type.WindowDeactivate, QEvent.Type.FocusOut))
_TIP_DIRECT = frozenset(int(t) for t in (
    QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick, QEvent.Type.Wheel,
    QEvent.Type.KeyPress))
_TIP_IMMEDIATE = frozenset(int(t) for t in (
    QEvent.Type.Hide, QEvent.Type.Close))
_TOOLTIP_EVENTS = (_TIP_SOFT | _TIP_DIRECT | _TIP_IMMEDIATE
                   | {_TIP_LEAVE, _TIP_MOUSE_MOVE})


def _tip_html(text: str, pal: dict) -> str:
    """把提示文字包成带令牌颜色的富文本。

    纯文本要转义（说明里会出现 `<select>` 这种写法，不转义会被吞掉）；
    已经是富文本的（卡片说明自带 `width:300px` 的 div）原样保留，只在外层
    补颜色与字号——内联样式优先，宽度约束不会被冲掉。
    """
    s = (text or "").strip()
    if _TIP_TAG.search(s):
        inner = s
    else:
        inner = (s.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace("\n", "<br>"))
    # 换行是"另起一段"，不是"再空一行"：QTextDocument 里 `<br>` 会连同
    # line-height 撑出一整行空白，改成两个块 + 6px 段距，紧凑又分得开。
    inner = _TIP_BR.sub('</div><div style="margin-top:6px;">', inner)
    # FONT 里的字体名带 ASCII 双引号，直接塞进 HTML 的 style="..." 会把属性
    # 提前闭合，后面的 font-size / line-height 全被当成属性吃掉。用 &quot;
    # 让浏览器（QTextDocument）解出正确的 CSS 字符串。
    family = FONT.replace('"', "&quot;")
    return ('<div style="color:%s; font-family:%s; font-size:12px; '
            'line-height:140%%;">%s</div>' % (pal["dim"], family, inner))


class ToolTipCard(QWidget):
    """自绘的工具提示卡片：card 底 + line 描边 + 8px 圆角。

    与 ControlPanel 一样用 `WA_TranslucentBackground` + paintEvent 画圆角，
    而不是给顶层窗口套 QSS 的 border-radius —— 后者圆角外仍由系统画成直角。
    """

    PAD_X, PAD_Y = 12, 9
    MAX_TEXT_W = 320
    RADIUS = 8

    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._pal = palette_now()
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setMaximumWidth(self.MAX_TEXT_W)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(self.PAD_X, self.PAD_Y, self.PAD_X, self.PAD_Y)
        lay.addWidget(self.label)

    def set_content(self, text: str, pal: dict) -> None:
        self._pal = pal
        # 长说明给一块稳定的宽度：自适应会把「目录」「Harness」这类词从中间
        # 拆到两行；短提示（"最小化"）仍按内容收窄，不撑成一条横幅。
        plain = _TIP_TAG.sub("", text or "")
        self.label.setMinimumWidth(260 if len(plain) > 30 else 0)
        self.label.setStyleSheet(
            "QLabel { background: transparent; color: %s; font-family: %s;"
            " font-size: 12px; }" % (pal["dim"], FONT))
        self.label.setText(_tip_html(text, pal))
        self.label.adjustSize()
        self.adjustSize()
        self.update()

    def show_at(self, pos: QPoint) -> None:
        self.adjustSize()
        screen = (QGuiApplication.screenAt(pos)
                  or QGuiApplication.primaryScreen())
        if screen is None:
            self.move(pos + QPoint(14, 18))
            self.show()
            return
        area = screen.availableGeometry()
        x, y = pos.x() + 14, pos.y() + 18
        if x + self.width() > area.right():
            x = pos.x() - self.width() - 12
        if y + self.height() > area.bottom():
            y = pos.y() - self.height() - 12
        x = max(area.left() + 4,
                min(x, area.right() - self.width() - 4))
        y = max(area.top() + 4,
                min(y, area.bottom() - self.height() - 4))
        self.move(x, y)
        self.show()
        self.raise_()

    def paintEvent(self, _e) -> None:                           # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1.0,
                                   self.height() - 1.0),
                            self.RADIUS, self.RADIUS)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self._pal["card"]))
        p.drawPath(path)
        p.setPen(QPen(QColor(self._pal["line"]), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()


class _ToolTipManager(QObject):
    """全局 `QEvent.ToolTip` 过滤器。挂在 QApplication 上，覆盖所有窗口。

    生命周期与 Qt 的 `QTipLabel` 同构：两个**单次**定时器（延迟隐藏、过期），
    加事件驱动的隐藏判定。整个类里没有 interval 型定时器。
    """

    HIDE_DELAY_MS = 300          # Qt: QTipLabel::hideTip()
    BASE_EXPIRE_MS = 10000       # Qt: restartExpireTimer()
    PER_CHAR_MS = 40
    BASE_CHARS = 100

    def __init__(self, app: QApplication, palette_provider):
        super().__init__(app)
        self._provider = palette_provider
        self.card = ToolTipCard()
        self._anchor = None
        self._life = QTimer(self)
        self._life.setSingleShot(True)
        self._life.timeout.connect(self.hide)
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(self._on_hide_timeout)

    def hide(self) -> None:
        self._life.stop()
        self._hide.stop()
        self.card.hide()
        QToolTip.hideText()

    def _still_hovering(self) -> bool:
        """鼠标是否还停在触发提示的那个控件上（只在事件里调用）。"""
        try:
            anchor = self._anchor
            if anchor is None or not anchor.isVisible():
                return False
            return anchor.rect().contains(
                anchor.mapFromGlobal(QCursor.pos()))
        except (RuntimeError, AttributeError, TypeError):
            return False

    def _schedule_hide(self) -> None:
        """Qt 的 hideTip()：延迟一拍再收，给相邻控件之间的移动留余地。"""
        if not self._hide.isActive():
            self._hide.start(self.HIDE_DELAY_MS)

    def _on_hide_timeout(self) -> None:
        # 延迟到期再复核一次位置。Tool 窗口首次 show 时宿主发来的那轮
        # Leave 是合成的，鼠标其实还在 ⓘ 上 —— 这种情况不能真的收起。
        if self.card.isVisible() and not self._still_hovering():
            self.hide()

    def _expire_ms(self, text: str) -> int:
        plain = _TIP_TAG.sub("", text or "")
        return (self.BASE_EXPIRE_MS
                + self.PER_CHAR_MS * max(0, len(plain) - self.BASE_CHARS))

    def _from_anchor(self, obj) -> bool:
        """事件是否来自"触发这次提示的那个控件"或它所在的窗口。

        早先是对**任何**控件的 Leave/Hide/FocusOut 都收起浮窗：Tool 窗口
        一显示，宿主窗口就会收到一轮 WindowDeactivate/FocusOut，浮窗刚 show
        就被自己收掉（离屏探针里 visible=False 抓到了）。这类事件只认锚点。
        """
        try:
            anchor = self._anchor
            if anchor is None or obj is anchor:
                return obj is anchor
            win = anchor.window()
            return win is not None and obj is win
        except RuntimeError:
            return False

    def eventFilter(self, obj, ev) -> bool:                    # noqa: N802
        try:
            kind = int(ev.type())
            if kind == int(QEvent.Type.ToolTip):
                return self._on_tip(obj, ev)
            if (not self.card.isVisible() or obj is self.card
                    or self.card.isAncestorOf(obj)):
                return False
            if kind == _TIP_MOUSE_MOVE:
                # 回到锚点就撤销待隐藏，移出去就排一次延迟隐藏。
                if self._still_hovering():
                    self._hide.stop()
                else:
                    self._schedule_hide()
            elif kind == _TIP_LEAVE:
                if self._from_anchor(obj):
                    self._schedule_hide()
            elif kind in _TIP_DIRECT:
                self.hide()
            elif kind in _TIP_IMMEDIATE:
                if self._from_anchor(obj):
                    self.hide()
            elif kind in _TIP_SOFT and not self._still_hovering():
                # WindowDeactivate / FocusOut：鼠标还在锚点上多半是首次
                # show 的合成事件，交给 300 ms 延迟隐藏去复核。
                self._schedule_hide()
        except (RuntimeError, AttributeError, TypeError, ValueError):
            return False
        return False

    def _on_tip(self, obj, ev) -> bool:
        if not isinstance(obj, QWidget) or obj is self.card \
                or self.card.isAncestorOf(obj):
            return False
        if not obj.isVisible() or not obj.isEnabled():
            return False
        text = obj.toolTip() or ""
        pos = ev.globalPos() if isinstance(ev, QHelpEvent) else QCursor.pos()
        if not text:
            # 表格/列表项的提示存在 model 的 ToolTipRole 里，不在 widget 上；
            # 而且 Qt 通常把 QEvent.ToolTip 发给 **viewport**，不是 view 本身。
            # 两种接收者都要认，否则开发者窗口里的条目提示会漏回系统灰框。
            view = obj if isinstance(obj, QAbstractItemView) else obj.parentWidget()
            if isinstance(view, QAbstractItemView):
                index = view.indexAt(view.viewport().mapFromGlobal(pos))
                if index.isValid():
                    text = str(index.data(Qt.ItemDataRole.ToolTipRole) or "")
        if not text.strip():
            return False
        self._anchor = obj
        self._hide.stop()          # Qt: restartExpireTimer() 里 hideTimer.stop()
        QToolTip.hideText()
        self.card.set_content(text, self._provider() or palette_now())
        self.card.show_at(QPoint(pos))
        self._life.start(self._expire_ms(text))
        return True


_TOOLTIPS: _ToolTipManager | None = None
_PROVIDER = palette_now

#: 兜底开关。默认走自绘卡片；设为 1 时把提示交回 Qt 原生 QToolTip，
#: 由 Qt 自己管生命周期与位置。只在排障/验证时用，**不做自动降级**：
#: 自绘浮窗万一在某个平台上出问题，用户设一个环境变量就能继续用，
#: 不必等发版。与渲染后端开关（DSH_WEBENGINE_HW_ACCEL）同一套路。
NATIVE_TOOLTIP_ENV = "DSH_TOOLTIP_NATIVE"
_NATIVE_QSS_BEGIN = "/* dsh-native-tooltip */"
_NATIVE_QSS_END = "/* /dsh-native-tooltip */"


def native_tooltip_enabled() -> bool:
    return os.environ.get(NATIVE_TOOLTIP_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def native_tooltip_qss(pal: dict) -> str:
    """兜底模式的 QToolTip 样式。Qt 会把 QTipLabel 的 objectName 设成
    `qtooltip_label`（见 QToolTip::showText），两个选择器都写，防止某种
    风格下 `QToolTip` 伪类型选择器不生效。
    """
    return """%s
QToolTip, QLabel#qtooltip_label {
    background: %s; color: %s; border: 1px solid %s;
    border-radius: 8px; padding: 6px 9px;
    font-family: %s; font-size: 12px;
}
%s""" % (_NATIVE_QSS_BEGIN, pal["card"], pal["dim"], pal["line"],
         FONT, _NATIVE_QSS_END)


def _apply_native(app: QApplication, pal: dict) -> None:
    """把配色交给原生 QToolTip：调色板 + 字体 + 应用级 QSS。

    Qt 的 QTipLabel 用 ToolTipBase / ToolTipText 两个角色上色，字体走
    `QApplication::font("QTipLabel")`（即 QToolTip.setFont）。样式表必须设在
    QApplication 上 —— tooltip 是独立顶层窗口，窗口自己的 QSS 管不到它
    （这正是自绘方案存在的原因，见文件开头的说明）。
    """
    p = QPalette(app.palette())
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(pal["card"]))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(pal["dim"]))
    QToolTip.setPalette(p)
    font = QFont()
    font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"])
    font.setPixelSize(12)
    QToolTip.setFont(font)
    sheet = app.styleSheet() or ""
    start = sheet.find(_NATIVE_QSS_BEGIN)
    if start >= 0:
        end = sheet.find(_NATIVE_QSS_END, start)
        if end >= 0:
            sheet = sheet[:start] + sheet[end + len(_NATIVE_QSS_END):]
    app.setStyleSheet((sheet + "\n" + native_tooltip_qss(pal)).strip())


def install_tooltips(app: QApplication | None = None,
                     palette_provider=None) -> _ToolTipManager | None:
    """接管全应用的工具提示。在 QApplication 建好后调一次即可，重复调用安全。

    默认走自绘卡片；`DSH_TOOLTIP_NATIVE=1` 时改用 Qt 原生 QToolTip 兜底，
    此时不安装任何事件过滤器，返回 None。
    """
    global _TOOLTIPS, _PROVIDER
    application = app or QApplication.instance()
    if application is None:
        return None
    if palette_provider is not None:
        _PROVIDER = palette_provider
    provider = _PROVIDER
    if native_tooltip_enabled():
        # 从自绘模式切过来时，旧的全局过滤器必须摘掉，否则它会继续吞事件。
        if _TOOLTIPS is not None:
            try:
                application.removeEventFilter(_TOOLTIPS)
            except RuntimeError:
                pass
            _TOOLTIPS = None
        _apply_native(application, provider())
        return None
    if _TOOLTIPS is not None:
        try:
            if _TOOLTIPS.parent() is application:
                if palette_provider is not None:
                    _TOOLTIPS._provider = palette_provider
                return _TOOLTIPS
        except RuntimeError:
            _TOOLTIPS = None
    mgr = _ToolTipManager(application, provider)
    application.installEventFilter(mgr)
    _TOOLTIPS = mgr
    return mgr


def refresh_tooltips() -> None:
    """主题变化时刷新兜底模式的调色板与 QSS。

    自绘模式不用刷新：浮窗在每次显示的那一刻读 palette。原生模式的调色板
    是全局静态量，得重新写一次才会跟上明暗切换。
    """
    app = QApplication.instance()
    if app is not None and native_tooltip_enabled():
        _apply_native(app, _PROVIDER())


def hide_tooltip() -> None:
    """主动收起浮窗（切主题、关窗口时用）。"""
    if _TOOLTIPS is not None:
        try:
            _TOOLTIPS.hide()
        except RuntimeError:
            pass
    else:
        QToolTip.hideText()


def active_tooltip() -> ToolTipCard | None:
    """给自检用的当前浮窗；兜底模式或没安装时返回 None。"""
    return _TOOLTIPS.card if _TOOLTIPS is not None else None


def style_loading_page(page: QWidget, pal: dict | None = None) -> None:
    """启动加载页：底色与提示文字跟随主题，避免深色界面先闪一片白。"""
    p = pal or palette_now()
    page.setStyleSheet("""
QWidget#loadingPage { background: %(bg)s; }
QLabel#loadingTip { color: %(dim)s; font-family: %(font)s; font-size: 13px; }
QProgressBar { background: %(hover)s; border: none; }
QProgressBar::chunk { background: %(accent)s; }
""" % {"bg": p["bg"], "dim": p["dim"], "hover": p["hover"],
       "accent": p["accent"], "font": FONT})


# ------------------------------------------------------------------ 提示窗口

class MessageDialog(QDialog):
    """替代 QMessageBox 的应用内提示窗。

    为什么不用 QMessageBox：它的图标是系统位图、按钮是系统样式，配色与 DSH
    差得很远；而且 `QMessageBox` 的正文样式几乎无法通过 QSS 控制。这里用
    同样的模态语义重画一个，图标取自 Lucide（与面板同源）。
    """

    KINDS = {
        "info": ("circle-check", "ok"),
        "warn": ("triangle-alert", "warn"),
        "error": ("circle-x", "bad"),
        "about": ("circle-dashed", "accent"),
    }

    def __init__(self, parent, title: str, text: str, kind: str = "info",
                 ok_text: str = "知道了", cancel_text: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        pal = palette_now()
        self.pal = pal
        self.setMinimumWidth(420)
        self.setMaximumWidth(620)
        self.setStyleSheet(dialog_qss(pal))

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        icon_name, state_key = self.KINDS.get(kind, self.KINDS["info"])
        icon = QLabel()
        icon.setPixmap(icons.qicon(icon_name, pal[state_key], 18).pixmap(18, 18))
        icon.setFixedSize(18, 18)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(icon)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("dlgTitle")
        title_lbl.setWordWrap(True)
        head.addWidget(title_lbl, 1)
        root.addLayout(head)

        body = QLabel(text)
        body.setObjectName("dlgBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setMinimumHeight(20)
        root.addWidget(body)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.btn_cancel = None
        if cancel_text:
            self.btn_cancel = QPushButton(cancel_text)
            self.btn_cancel.clicked.connect(self.reject)
            row.addWidget(self.btn_cancel)
        self.btn_ok = QPushButton(ok_text)
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(self.btn_ok)
        root.addLayout(row)

        self._fit()

    def _fit(self) -> None:
        """宽度自适应正文：短提示不要撑成一条大横条。"""
        hint = self.findChild(QLabel, "dlgBody")
        if hint is None:
            return
        width = hint.fontMetrics().horizontalAdvance(
            max(hint.text().splitlines() or [""], key=len)) + 90
        self.setMinimumWidth(max(380, min(560, width)))

    # ---- 便捷入口（与 QMessageBox 的用法一一对应） ----
    #
    # 每个入口都会 `deleteLater()`（2026-10-20 体检）。原因：这些窗口以主窗口为
    # parent，C++ 归属在父对象手里——Python 侧执行完把引用丢掉**不会**释放它，
    # 于是每弹一次提示，主窗口的 children 里就永久多一个 QDialog（含图标位图、
    # 若干标签与布局）。用户点"知道了"几百次就是一个几百个对话框的主窗口。
    #
    # 用延迟删除而不是 `WA_DeleteOnClose`：调用方在 `exec()` 返回后还会读
    # `dlg.new_handle` 之类的成员，立刻销毁会变成访问已删对象。
    @classmethod
    def _run(cls, dlg) -> int:
        try:
            return dlg.exec()
        finally:
            dlg.deleteLater()

    @classmethod
    def info(cls, parent, title: str, text: str, ok_text: str = "知道了") -> int:
        return cls._run(cls(parent, title, text, "info", ok_text))

    @classmethod
    def warn(cls, parent, title: str, text: str, ok_text: str = "知道了") -> int:
        return cls._run(cls(parent, title, text, "warn", ok_text))

    @classmethod
    def error(cls, parent, title: str, text: str, ok_text: str = "知道了") -> int:
        return cls._run(cls(parent, title, text, "error", ok_text))

    @classmethod
    def about(cls, parent, title: str, text: str, ok_text: str = "关闭") -> int:
        return cls._run(cls(parent, title, text, "about", ok_text))

    @classmethod
    def ask(cls, parent, title: str, text: str,
            ok_text: str = "继续", cancel_text: str = "取消") -> bool:
        """是/否确认。危险操作（清空数据、切换来源）走这里。

        统一用 warn 图标：这些操作都不致命，但确实会改东西，
        用 info 的绿勾会让人以为"只是通知一下"。
        """
        return (cls._run(cls(parent, title, text, "warn", ok_text, cancel_text))
                == QDialog.DialogCode.Accepted)
