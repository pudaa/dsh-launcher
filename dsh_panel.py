# -*- coding: utf-8 -*-
"""自绘控制面板。

为什么不再靠托盘菜单
--------------------
托盘 menu 只能放"点一下就完事"的项。一旦涉及"看状态 / 填配置 / 读提示"，
它就撑不住——没有分组、没有滚动、放不下说明文字。

为什么不用 QtWebEngine 渲染面板
-------------------------------
用 HTML/CSS 确实能白拿 Web 的图标与动效生态，但**每多一个 WebEngine 实例要多
100~300MB 内存**，与"低占用"直接冲突（主窗口已经有一个实例在跑 DSH Web UI）。
所以面板走原生 QWidget。

配色与风格：与官方一致，不另起一套
----------------------------------
面板的明暗两套配色**逐条取自 DSH 官方前端的语义令牌**（`@deepseek-ai/dsh-client-ui-theme`
里的 `--dsw-alias-*` / `--dsw-specific-*`），不是我们自己调的色。这样面板和官方
界面放在一起不会有割裂感。每个色值后面都标了来源令牌名，便于上游改版时对表。

明暗由主窗口**标题栏取色的既有结果**（`dwm.is_dark(bg)`）驱动，面板不自己判断。

圆角与窗口
----------
自绘圆角 + 半透明背景，而不是等 DWM 给。理由是可验证：
DWM 圆角是窗口管理器合成的，`PrintWindow` 抓不到、屏幕抓图在部分环境下也不可靠，
换句话说"设成功但没生效"根本查不出来。自绘的圆角在离屏渲染里就能验角像素。
半径按官方《统一圆角规范》：窗口 R16、卡片 R12、控件 R8。

状态检测只报告、不修复
----------------------
按老大的决定：本阶段只如实报告每一项是否正常，不做一键修复。

图标
----
来自 `dsh_icons`（Lucide，内联 SVG，零文件依赖）。面板里**不出现 Emoji**。
"""
from __future__ import annotations

import os
from string import Template

from PySide6.QtCore import (QAbstractAnimation, QEvent, Property, QEasingCurve,
                            QPointF, QPropertyAnimation, QRectF, QSize, Qt,
                            QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QGuiApplication, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget)

import dsh_icons as icons
from dsh_host import compat, config, contract, dwm, updater
from dsh_host.version import HOST_REPO, HOST_VERSION

FONT = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'

PANEL_W, PANEL_H = 880, 600
TITLEBAR_H = 40
SIDEBAR_W = 208
STATUSBAR_H = 28

#: 圆角。按官方《统一圆角规范》：R28 是 Web 里的主面板容器，原生窗口取更克制
#: 的一档；内部卡片 R12、控件 R8，与官方尺度一致。
R_WINDOW = 16
R_CARD = 12
R_CONTROL = 8

#: 动效时长（毫秒）。集中一处，免得各处各写一个数。
MS_PAGE = 140          # 页面切换淡入（短一点，避免"文字浮动"的观感）
MS_COLOR = 180         # 状态色过渡
MS_PULSE = 1400        # 状态点脉冲一轮

STATE_OK, STATE_BAD, STATE_WARN, STATE_UNKNOWN = "ok", "bad", "warn", "unknown"
STATE_ICON = {
    STATE_OK: "circle-check",
    STATE_BAD: "circle-x",
    STATE_WARN: "triangle-alert",
    STATE_UNKNOWN: "circle-dashed",
}


def _css(v: str) -> str:
    """把 CSS 的 `#RRGGBBAA` 转成 Qt 样式表要的 `#AARRGGBB`。

    官方令牌给的是 CSS 写法（`#0000001a` = 10% 不透明的黑），而 Qt 样式表的
    八位十六进制是 **#AARRGGBB**，字节序相反。直接照抄会得到完全不同的颜色，
    而且肉眼不容易看出来——所以统一走这里转换，保证下面表里的值能和官方源码
    **逐字对上**。六位的 `#RRGGBB` 原样返回。
    """
    s = (v or "").strip()
    if s.startswith("#") and len(s) == 9:
        return "#" + s[7:9] + s[1:7]
    return s


#: 浅色主题。色值 = 官方 `body` 选择器下的令牌原值。
PALETTE_LIGHT = {
    "bg": _css("#ffffff"),          # --dsw-alias-bg-base
    "card": _css("#ffffff"),        # --dsw-alias-bg-layer-1（浅色下层次靠描边，不靠底色）
    "side": _css("#f9fafb"),        # --dsw-specific-sidebar-fill
    "text": _css("#0f1115"),        # --dsw-alias-label-primary
    "dim": _css("#61666b"),         # --dsw-alias-label-secondary
    "muted": _css("#81858c"),       # --dsw-alias-label-tertiary
    "caption": _css("#adb2b8"),     # --dsw-alias-label-caption
    "line": _css("#0000001a"),      # --dsw-alias-border-l2
    "line_soft": _css("#0000000a"),  # --dsw-alias-border-l1
    "hover": _css("#2631480f"),     # --dsw-alias-interactive-bg-hover
    "nav_active": _css("#ebeef2"),  # --dsw-specific-sidebar-nav-item-active
    "nav_hover": _css("#f1f3f5"),   # --dsw-specific-sidebar-nav-item-hover
    "accent": _css("#4176e6"),      # --dsw-alias-link
    "primary_fill": _css("#0f1115"),   # --dsw-alias-button-primary-fill
    "primary_hover": _css("#43454a"),  # --dsw-alias-button-primary-hover
    "primary_text": _css("#ffffff"),   # --dsw-alias-label-primary-foreground
    "btn_fill": _css("#ffffff"),       # --dsw-alias-button-floating-fill
    "btn_hover": _css("#f1f3f5"),      # --dsw-alias-button-floating-hover
    "ok": _css("#22c55e"),             # --dsw-alias-state-success-primary
    "bad": _css("#ec1313"),            # --dsw-alias-state-error-primary
    "warn": _css("#f59e0b"),           # --dsw-alias-state-warn-primary
    #: 滚动指示条。官方给了两档（bg / hover），这里取 **hover 档**：
    #: 官方滚动条是常驻的，bg 档（#e5e5e5）在白底上对比度只有 1.26:1 也够用；
    #: 我们这根是"滚动时浮现、停下就淡出"的浮层，淡到看不见就失去意义了。
    "scroll": _css("#d4d4d4"),         # --dsw-alias-scrollbar-hover-l1
}

#: 深色主题。色值 = 官方 `body[data-ds-dark-theme]` 下的令牌。
PALETTE_DARK = {
    "bg": _css("#151517"),          # --dsw-alias-bg-base
    "card": _css("#232324"),        # --dsw-alias-bg-layer-1
    "side": _css("#1b1b1c"),        # --dsw-specific-sidebar-fill
    "text": _css("#f9fafb"),        # --dsw-alias-label-primary
    "dim": _css("#cfd3d6"),         # --dsw-alias-label-secondary
    "muted": _css("#adb2b8"),       # --dsw-alias-label-tertiary
    "caption": _css("#81858c"),     # --dsw-alias-label-caption
    "line": _css("#ffffff1f"),      # --dsw-alias-border-l2
    "line_soft": _css("#ffffff0f"),  # --dsw-alias-border-l1
    "hover": _css("#ffffff14"),     # --dsw-alias-interactive-bg-hover
    "nav_active": _css("#43454a"),  # --dsw-specific-sidebar-nav-item-active
    "nav_hover": _css("#2c2c2e"),   # --dsw-specific-sidebar-nav-item-hover
    "accent": _css("#679efe"),      # --dsw-alias-link
    "primary_fill": _css("#f9fafb"),   # --dsw-alias-button-primary-fill
    "primary_hover": _css("#ebeef2"),  # --dsw-alias-button-primary-hover
    "primary_text": _css("#0f1115"),   # --dsw-alias-label-primary-foreground
    "btn_fill": _css("#2c2c2e"),       # --dsw-alias-button-floating-fill
    "btn_hover": _css("#353638"),      # --dsw-alias-button-floating-hover
    "ok": _css("#22c55e"),
    "bad": _css("#f25a5a"),
    "warn": _css("#f59e0b"),
    "scroll": _css("#545557"),         # --dsw-alias-scrollbar-hover-l1
}

PALETTES = {"light": PALETTE_LIGHT, "dark": PALETTE_DARK}


def palette(theme: str) -> dict:
    return PALETTES.get(theme) or PALETTE_DARK


def state_color(pal: dict, state: str) -> str:
    return {"ok": pal["ok"], "bad": pal["bad"],
            "warn": pal["warn"]}.get(state, pal["muted"])


# ------------------------------------------------------------------ 样式表
#
# 用 Template 而不是 f-string：Qt 样式表满屏花括号，f-string 里要写 {{ }}，
# 可读性会崩掉。

_STYLE = Template("""
QWidget            { background: transparent; }
QWidget#titlebar,
QWidget#sidebar,
QWidget#statusbar,
QWidget#content,
QWidget#page,
QWidget#pageContent { background: transparent; }
QScrollArea#pageScroll { background: transparent; border: none; }
QScrollArea#pageScroll > QWidget > QWidget { background: transparent; }

QLabel#panelTitle  { color: $text; font-family: $font; font-size: 13px; }
QLabel#statusText  { color: $muted; font-family: $font; font-size: 12px; }
QLabel#h1          { color: $text; font-family: $font; font-size: 19px; font-weight: 600; }
QLabel#h2          { color: $text; font-family: $font; font-size: 14px; font-weight: 600; }
QLabel#body        { color: $dim;  font-family: $font; font-size: 13px; }
QLabel#kv_k        { color: $dim;  font-family: $font; font-size: 13px; }
QLabel#kv_v        { color: $text; font-family: $font; font-size: 13px; }

QToolButton#winBtn { border: none; background: transparent; border-radius: 0px; }
QToolButton#winBtn:hover { background: $hover; }
QToolButton#winBtn[danger="true"]:hover { background: $bad; }

/* 按钮的最小尺寸不写在这里：QSS 的 min-height 是**内容区**下限，
   padding 与 border 会再叠上去，结果比预期高一截。改在 BasePage 里统一
   setMinimumHeight/Width，语义明确也可控。 */
QPushButton#action  {
    color: $text; background: $btn_fill; border: 1px solid $line;
    border-radius: ${r_ctrl}px; padding: 6px 14px;
    font-family: $font; font-size: 13px;
}
QPushButton#action:hover    { background: $btn_hover; }
QPushButton#action:disabled { color: $muted; }

QPushButton#primary {
    color: $primary_text; background: $primary_fill; border: none;
    border-radius: ${r_ctrl}px; padding: 7px 16px;
    font-family: $font; font-size: 13px; font-weight: 600;
}
QPushButton#primary:hover    { background: $primary_hover; }
QPushButton#primary:disabled { color: $muted; background: $hover; }
""")

_CARD_STYLE = Template("""
QFrame#card { background: $card; border: 1px solid $line; border-radius: ${r_card}px; }
QFrame#card QLabel { border: none; }
""")

_NAV_STYLE = Template("""
QPushButton {
    text-align: left; padding-left: 14px; border: none;
    color: $dim; font-family: $font; font-size: 13px;
    background: transparent; border-radius: ${r_ctrl}px;
}
QPushButton:hover   { color: $text; background: $nav_hover; }
QPushButton:checked { color: $text; background: $nav_active; font-weight: 600; }
""")


def build_style(pal: dict) -> str:
    return _STYLE.substitute(
        text=pal["text"], dim=pal["dim"], muted=pal["muted"], hover=pal["hover"],
        line=pal["line"], bad=pal["bad"], font=FONT, r_ctrl=R_CONTROL,
        btn_fill=pal["btn_fill"], btn_hover=pal["btn_hover"],
        primary_fill=pal["primary_fill"], primary_hover=pal["primary_hover"],
        primary_text=pal["primary_text"],
    )


def card_style(pal: dict) -> str:
    return _CARD_STYLE.substitute(card=pal["card"], line=pal["line"],
                                  r_card=R_CARD)


def nav_style(pal: dict) -> str:
    return _NAV_STYLE.substitute(dim=pal["dim"], text=pal["text"],
                                 nav_hover=pal["nav_hover"],
                                 nav_active=pal["nav_active"], font=FONT,
                                 r_ctrl=R_CONTROL)


# ------------------------------------------------------------------ 动效原语
#
# 只做三个。Qt 没有 Web 那种现成动效库，缓动曲线与时长都得自己定。

def fade_in(widget: QWidget, ms: int = MS_PAGE) -> QPropertyAnimation:
    """从全透明淡入。页面切换用。

    **不做"逐条错开淡入"**：那正是上一版观感问题的来源——切换页签时每一行依次
    浮现，看起来像文字在浮动。整页一次性短淡入才是稳的。
    """
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _cleanup() -> None:
        # 结束后撤掉 effect（常驻会让整棵子树每次重绘多走一层合成）。
        # **只撤自己那一个**：快速连点导航会让多个 fade_in 叠在同一控件上，
        # 先结束的那个若无脑 setGraphicsEffect(None)，会把后开始那个动画所依赖的
        # effect 一起摘走，透明度再也推不动——页面永远一片空白。
        if widget.graphicsEffect() is eff:
            widget.setGraphicsEffect(None)

    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def make_pulse(dot: "StatusDot") -> QPropertyAnimation:
    """状态点脉冲：相位 0→1 循环。InOutSine，像呼吸而不像闪烁。"""
    anim = QPropertyAnimation(dot, b"phase", dot)
    anim.setDuration(MS_PULSE)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.InOutSine)
    anim.setLoopCount(-1)
    return anim


def animate_color(dot: "StatusDot", to: str) -> None:
    """状态色平滑过渡。直接换样式表会"啪"地跳变，很廉价。"""
    anim = QPropertyAnimation(dot, b"rgb", dot)
    anim.setDuration(MS_COLOR)
    anim.setEasingCurve(QEasingCurve.Type.OutQuad)
    anim.setStartValue(dot.color())
    anim.setEndValue(QColor(to))
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


# ------------------------------------------------------------------ 基础控件

class StatusDot(QWidget):
    """状态点。`phase` 驱动呼吸光晕，`rgb` 做颜色过渡——两个自定义属性供动画用。"""

    def __init__(self, state: str = STATE_UNKNOWN, size: int = 14, pal=None,
                 parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self.pal = pal or PALETTE_DARK
        self._color = QColor(state_color(self.pal, state))
        self._pulse = None
        self._d = size
        #: 当前状态名。行控件靠它判断"值变了没有"，避免每次重放过渡动画。
        self.state_name = state
        self.setFixedSize(size, size)

    def set_palette_colors(self, pal: dict) -> None:
        self.pal = pal
        self.set_rgb(QColor(state_color(pal, self.state_name)))

    def get_phase(self) -> float:
        return self._phase

    def set_phase(self, v: float) -> None:
        self._phase = float(v)
        self.update()

    phase = Property(float, get_phase, set_phase)

    def get_rgb(self) -> QColor:
        return self._color

    def set_rgb(self, c: QColor) -> None:
        self._color = QColor(c)
        self.update()

    rgb = Property(QColor, get_rgb, set_rgb)

    def color(self) -> QColor:
        return QColor(self._color)

    def set_state(self, state: str, animated: bool = True) -> None:
        self.state_name = state
        target = state_color(self.pal, state)
        if animated:
            animate_color(self, target)
        else:
            self.set_rgb(QColor(target))
        # 只有"未检测"才呼吸。什么都正常时界面还在动，会让人以为没跑完。
        if state == STATE_UNKNOWN:
            self.start_pulse()
        else:
            self.stop_pulse()

    def start_pulse(self) -> None:
        if self._pulse is None:
            self._pulse = make_pulse(self)
        self._pulse.start()

    def stop_pulse(self) -> None:
        if self._pulse is not None:
            self._pulse.stop()
        self._phase = 0.0
        self.update()

    def paintEvent(self, _e) -> None:                             # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx = cy = self._d / 2.0
        r = self._d / 2.0 - 3
        if self._pulse is not None and self._pulse.state() == \
                QAbstractAnimation.State.Running:
            # 三角波 0→1→0，避免循环接缝处闪一下
            tri = 1.0 - abs(self._phase * 2.0 - 1.0)
            halo = QColor(self._color)
            halo.setAlpha(int(70 * tri))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo)
            p.drawEllipse(QPointF(cx, cy), r + 2 + 2 * tri, r + 2 + 2 * tri)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.end()


class NavItem(QPushButton):
    """左侧导航项。选中态用官方侧栏的 active 底色，而不是自己发明的左侧竖条。"""

    def __init__(self, icon_name: str, text: str, pal: dict, parent=None):
        super().__init__("   " + text, parent)
        self.icon_name = icon_name
        self.pal = pal
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(38)
        self.setIconSize(QSize(17, 17))
        self.setStyleSheet(nav_style(pal))
        self.toggled.connect(self._refresh_icon)
        self._refresh_icon(False)

    def set_palette_colors(self, pal: dict) -> None:
        self.pal = pal
        self.setStyleSheet(nav_style(pal))
        self._refresh_icon(self.isChecked())

    def _refresh_icon(self, on: bool) -> None:
        self.setIcon(icons.qicon(self.icon_name,
                                 self.pal["text"] if on else self.pal["dim"], 17))


class Card(QFrame):
    """内容分组卡片。

    浅色主题下**层次靠描边而不是底色**——官方令牌里 `bg-layer-1` 在浅色下就是纯白，
    跟着做才不会有割裂感；深色下才是 #232324 的浮起色。
    """

    def __init__(self, title: str, pal: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.pal = pal
        self.setStyleSheet(card_style(pal))
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 10)
        self.body.setSpacing(0)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(Template(
            "color: $muted; font-family: $font; font-size: 12px;"
            " padding: 14px 16px 8px 16px; border: none;"
        ).substitute(muted=pal["muted"], font=FONT))
        self.body.addWidget(self.title_lbl)

    def set_palette_colors(self, pal: dict) -> None:
        self.pal = pal
        self.setStyleSheet(card_style(pal))

    def add(self, w: QWidget) -> None:
        self.body.addWidget(w)

    def add_text(self, text: str, kind: str = "body") -> QLabel:
        """加一段说明文字。**卡片内的纯文本必须走这个方法。**

        为什么：`Card` 内部只有**标题**自带内边距，`KeyValueRow` / `CheckRow`
        这类行控件各自带自己的内边距，而**裸 QLabel 什么都不带**——
        直接 `add(_label(...))` 会让正文贴到卡片边框上（连接页、关于页都踩过，
        真机截图里一眼能看到）。

        与其在每个页面手动补，不如把"卡片内的纯文本要有左右 16px"这件事
        收在这一个出口上。
        """
        lbl = _label(text, kind)
        lbl.setContentsMargins(16, 3, 16, 3)
        self.body.addWidget(lbl)
        return lbl


class CheckRow(QWidget):
    """一行检测结果：状态点 + 名称 + 值。"""

    def __init__(self, key: str, name: str, pal: dict, parent=None):
        super().__init__(parent)
        self.key = key
        self.dot = StatusDot(STATE_UNKNOWN, 14, pal)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(Template(
            "color: $text; font-family: $font; font-size: 13px;"
        ).substitute(text=pal["text"], font=FONT))
        self.value_lbl = QLabel("未检测")
        self.value_lbl.setStyleSheet(Template(
            "color: $dim; font-family: $font; font-size: 12px;"
        ).substitute(dim=pal["dim"], font=FONT))
        self.value_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.value_lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
        self.value_lbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Preferred)

        row = QHBoxLayout(self)
        # 左右留白 16 而不是 14：文本贴边是上一版被指出的问题之一
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(10)
        row.addWidget(self.dot)
        row.addWidget(name_lbl)
        row.addWidget(self.value_lbl, 1)
        self.setFixedHeight(36)

    def set_palette_colors(self, pal: dict) -> None:
        self.dot.set_palette_colors(pal)

    def apply(self, res: "CheckResult") -> None:
        self.dot.state_name = res.state
        self.value_lbl.setText(res.value or "—")
        self.value_lbl.setToolTip(res.hint or "")
        self.dot.set_state(res.state)


class KeyValueRow(QWidget):
    """键值一行：左键右值，值可选中复制。"""

    def __init__(self, key: str, value: str, pal: dict, width: int = 128,
                 parent=None):
        super().__init__(parent)
        self.pal = pal
        k = QLabel(key)
        k.setObjectName("kv_k")
        k.setFixedWidth(width)
        self.v = QLabel(value or "—")
        self.v.setObjectName("kv_v")
        self.v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.v.setWordWrap(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(10)
        lay.addWidget(k)
        lay.addWidget(self.v, 1)
        self.setMinimumHeight(34)

    def set_palette_colors(self, pal: dict) -> None:
        self.pal = pal

    def set(self, value: str) -> None:
        self.v.setText(value or "—")


class ToggleRow(QWidget):
    """带说明的一行开关。

    用可勾选按钮而不是 QCheckBox：Qt 原生复选框在深色样式下很难配色一致，
    而按钮形态可以直接吃我们的样式表，也天然继承圆角与 hover。
    """

    def __init__(self, title: str, hint: str, checked: bool, pal: dict,
                 on_change, parent=None):
        super().__init__(parent)
        self.pal = pal
        self._on_change = on_change
        box = QVBoxLayout(self)
        box.setContentsMargins(16, 8, 16, 8)
        box.setSpacing(2)
        self.btn = QPushButton(title)
        self.btn.setCheckable(True)
        self.btn.setChecked(bool(checked))
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setStyleSheet(Template("""
            QPushButton { text-align: left; color: $text; background: transparent;
                border: none; padding: 0px; font-family: $font; font-size: 13px; }
            QPushButton:checked { color: $accent; font-weight: 600; }
        """).substitute(text=pal["text"], accent=pal["accent"], font=FONT))
        self.hint = QLabel(hint)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(Template(
            "color: $muted; font-family: $font; font-size: 12px;"
        ).substitute(muted=pal["muted"], font=FONT))
        box.addWidget(self.btn)
        box.addWidget(self.hint)
        self.btn.toggled.connect(self._changed)

    def _changed(self, on: bool) -> None:
        self._on_change(bool(on))

    def set_checked(self, on: bool) -> None:
        self.btn.blockSignals(True)
        self.btn.setChecked(bool(on))
        self.btn.blockSignals(False)

    def set_palette_colors(self, pal: dict) -> None:
        self.pal = pal


# ------------------------------------------------------------------ 检测

class CheckResult:
    """一项检测的结论。`state` 只用 STATE_* 四个值。"""

    __slots__ = ("key", "name", "value", "state", "hint")

    def __init__(self, key: str, name: str, value: str,
                 state: str = STATE_UNKNOWN, hint: str = ""):
        self.key, self.name, self.value, self.state, self.hint = \
            key, name, value, state, hint


#: 状态页的固定行。键与 local_checks 的产出对应。
CHECK_ROWS = (
    ("node", "Node.js 运行环境"),
    ("npm", "npm 包管理器"),
    ("dsh", "DeepSeek Harness"),
    ("home", "用户数据目录"),
    ("home_w", "数据目录可写"),
    ("svc", "后台服务"),
    ("channel", "官方版本通道"),
)

#: 本机项（不联网，打开页面就自动跑）
LOCAL_KEYS = ("node", "npm", "dsh", "home", "home_w", "svc")


def local_checks(host: "PanelHost") -> list[CheckResult]:
    """本机可立即得出的检测项——**不联网**，所以可以自动跑。"""
    out: list[CheckResult] = []
    inst = host.install()

    node = (inst.node if inst else None) or contract.find_node()
    if node:
        rc, ver, _ = contract.run_capture([node, "--version"], timeout=20)
        txt = ver.strip().splitlines()[0] if rc == 0 and ver.strip() else "无法读取版本"
        out.append(CheckResult("node", "Node.js 运行环境", txt,
                               STATE_OK if rc == 0 else STATE_WARN, node))
    else:
        out.append(CheckResult("node", "Node.js 运行环境", "未安装", STATE_BAD,
                               "DSH 需要 Node.js 才能运行。可到「本机部署」查看怎么装。"))

    if inst and inst.npm_cli:
        out.append(CheckResult("npm", "npm 包管理器", "就绪", STATE_OK, inst.npm_cli))
    else:
        out.append(CheckResult("npm", "npm 包管理器", "未找到", STATE_WARN,
                               "更新 DSH 需要 npm。"))

    if inst:
        out.append(CheckResult("dsh", "DeepSeek Harness",
                               f"{inst.version} · {inst.source}", STATE_OK,
                               inst.pkg_root))
    else:
        out.append(CheckResult("dsh", "DeepSeek Harness", "未安装", STATE_BAD,
                               "可到「本机部署」查看怎么装。"))

    home = contract.resolve_home()
    if home and os.path.isdir(home):
        fp = contract.home_fingerprint(home)
        out.append(CheckResult(
            "home", "用户数据目录",
            ("（内容较多，未全部统计）" if fp.truncated else fp.describe()),
            STATE_OK, home))
        writable = os.access(home, os.W_OK)
        out.append(CheckResult("home_w", "数据目录可写",
                               "可写" if writable else "不可写",
                               STATE_OK if writable else STATE_BAD,
                               "会话记录与登录状态存在这里"))
    else:
        out.append(CheckResult("home", "用户数据目录", home or "未定位",
                               STATE_WARN, "首次启动 DSH 时才会创建"))
        out.append(CheckResult("home_w", "数据目录可写", "未检测", STATE_UNKNOWN))

    h = host.handle()
    if h and h.port:
        alive = contract.tcp_open(h.host or "127.0.0.1", h.port, timeout=1.0)
        out.append(CheckResult("svc", "后台服务",
                               f"运行中 · 端口 {h.port}" if alive else "已停止",
                               STATE_OK if alive else STATE_WARN, h.url or ""))
    else:
        out.append(CheckResult("svc", "后台服务", "未启动", STATE_WARN,
                               "打开主窗口会自动启动"))
    return out


class ChecksWorker(QThread):
    """在后台跑检测。

    必须后台：`home_fingerprint()` 要遍历 DSH_HOME（实测可达三万多个文件），
    放界面线程会卡住整个面板。
    """
    done = Signal(list)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host

    def run(self):
        try:
            self.done.emit(local_checks(self.host))
        except Exception as e:                                   # noqa: BLE001
            self.done.emit([CheckResult("error", "检测过程出错",
                                        f"{type(e).__name__}: {e}", STATE_BAD)])


class ChannelWorker(QThread):
    """查官方三条版本通道。**联网**，所以只在用户点按钮时才跑。"""
    done = Signal(object)

    def __init__(self, install, parent=None):
        super().__init__(parent)
        self.install = install

    def run(self):
        try:
            tags = updater.fetch_dist_tags(self.install, timeout=20)
        except Exception as e:                                   # noqa: BLE001
            self.done.emit(CheckResult("channel", "官方版本通道",
                                       "查询失败", STATE_WARN, str(e)[:200]))
            return
        parts = [f"{t}={tags.get(t)}" for t in ("latest", "next", "alpha")
                 if tags.get(t)]
        cur = self.install.version if self.install else ""
        highest = cur
        for t in ("latest", "next", "alpha"):
            v = tags.get(t)
            if v and (not highest or compat.compare(v, highest) > 0):
                highest = v
        # 通道查得到就算正常。装了较旧的版本不判"异常"——那是用户的选择，
        # 该由「检查更新」去提示，不该在体检页把用户状态标红。
        self.done.emit(CheckResult(
            "channel", "官方版本通道", " · ".join(parts), STATE_OK,
            f"已安装 {cur or '未知'}；三条通道最高 {highest or '未知'}"))


# ------------------------------------------------------------------ 主机接口

class PanelHost:
    """面板与主窗口之间的**窄接口**。

    面板不直接引用 MainWindow——主窗口一改内部结构面板就跟着坏，而且面板会不自觉
    地伸手去拿更多东西。这里用回调把需要的能力逐条写清楚，面板也能脱离主窗口单测。
    """

    def __init__(self, install=None, handle=None, host_release=None,
                 theme=None, open_path=None, open_web=None, log_dir=None,
                 flag=None, set_flag=None, action=None):
        self._install = install
        self._handle = handle
        self._release = host_release
        self._theme = theme
        self.open_path = open_path or (lambda p: None)
        self.open_web = open_web or (lambda: None)
        self.log_dir = log_dir or (lambda: "")
        self._flag = flag or (lambda name: False)
        self._set_flag = set_flag or (lambda name, value: None)
        self.action = action or (lambda name: None)

    def install(self):
        return self._install() if callable(self._install) else self._install

    def handle(self):
        return self._handle() if callable(self._handle) else self._handle

    def host_release(self):
        return self._release() if callable(self._release) else self._release

    def theme(self) -> str:
        """当前主题名（"light" / "dark"）。

        来源是主窗口**标题栏取色的既有结果**——面板不自己判断主题，
        免得两处判断不一致。取不到时按深色。
        """
        t = self._theme() if callable(self._theme) else self._theme
        return t if t in PALETTES else "dark"

    def flag(self, name: str) -> bool:
        return bool(self._flag(name))

    def set_flag(self, name: str, value: bool) -> None:
        self._set_flag(name, bool(value))


def guide_links() -> dict:
    """引导用的网址与命令。

    刻意**不写死在界面里**——它们放在适配表（compat）里，可以外部覆盖、
    无需重新打包。上游改下载页、改安装命令时，我们改配置就能跟上。
    """
    prof = compat.load_profile(None)
    g = prof.get("guide") or {}
    return {
        "node_download": g.get("node_download", ""),
        "node_winget": g.get("node_winget", ""),
        "dsh_site": g.get("dsh_site", ""),
        "dsh_repo": g.get("dsh_repo", ""),
        "dsh_docs": g.get("dsh_docs", ""),
    }


# ------------------------------------------------------------------ 页面

def _label(text: str, kind: str = "body") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(kind)
    lbl.setWordWrap(True)
    return lbl


class ScrollHint(QWidget):
    """浮动滚动指示条。

    为什么不用原生 `QScrollBar`
    --------------------------
    原生滚动条**常驻可见、还要占掉内容宽度**，在固定尺寸的面板里就是一根多余
    的分割线（老大明确说"不喜欢有滚动条"）。改成浮在内容右边缘的小圆角条：
    滚动时浮现、停下约 1 秒后淡出，内容不需要滚动时根本不显示、也不占位。

    颜色用官方的 `scrollbar-bg-l1` 令牌，明暗各一套，和其他配色同源。

    两个实现要点：
      · 必须 `WA_TransparentForMouseEvents`——它是浮层，绝不能挡住下面的点击。
      · 滚动条策略设成 AlwaysOff 后**滚轮依然有效**（策略只影响条本身的显示，
        不影响视口的滚动行为），所以"隐藏原生条"不会带来滚动能力的损失。
    """

    BAR_W = 4
    MARGIN = 3
    MIN_H = 32
    IDLE_MS = 900          # 停止滚动后多久开始淡出
    FADE_MS = 220

    def __init__(self, area: QScrollArea, pal: dict, parent=None):
        super().__init__(area.viewport())
        self.area = area
        self.pal = pal
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedWidth(self.BAR_W)
        self._fade = 0.0
        self._anim = None
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(lambda: self._animate_to(0.0))

        bar = area.verticalScrollBar()
        bar.valueChanged.connect(self._on_scroll)
        bar.rangeChanged.connect(lambda *_: self._sync())
        area.viewport().installEventFilter(self)
        self.hide()

    # -- 淡入淡出由这个属性驱动 --
    def get_fade(self) -> float:
        return self._fade

    def set_fade(self, v: float) -> None:
        self._fade = float(v)
        # 可见性绑在淡入度上：动画把它推上去时才真正 show()。
        # 只靠 _sync() 里判断是不行的——滚动的第一瞬间 _fade 还是 0，
        # 那时 _sync 不会显示它，之后也没有人再显示，条子就永远看不见。
        self.setVisible(self._fade > 0.01 and self._scrollable())
        self.update()

    fade = Property(float, get_fade, set_fade)

    def _scrollable(self) -> bool:
        bar = self.area.verticalScrollBar()
        return bar.maximum() > bar.minimum()

    def set_palette_colors(self, pal: dict) -> None:
        self.pal = pal
        self.update()

    def _animate_to(self, target: float) -> None:
        if abs(self._fade - target) < 0.01:
            return
        anim = QPropertyAnimation(self, b"fade", self)
        anim.setDuration(self.FADE_MS)
        anim.setStartValue(self._fade)
        anim.setEndValue(float(target))
        anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._anim = anim

    def _on_scroll(self, *_a) -> None:
        self._sync()
        if self._fade < 0.99:
            self._animate_to(1.0)
        self._idle.start(self.IDLE_MS)

    def _sync(self) -> None:
        """按滚动位置与内容比例摆好条子；内容不需要滚动就整条收起来。"""
        bar = self.area.verticalScrollBar()
        vp = self.area.viewport()
        vh, total = vp.height(), vp.height() + bar.maximum() - bar.minimum()
        if self._scrollable() is False or total <= vh or vh <= 0:
            self.setVisible(False)
            return
        # 可用高度要扣掉上下外边距，否则滑到底时条子会越过视口下沿
        usable = max(1, vh - 2 * self.MARGIN)
        h = max(self.MIN_H, int(usable * vh / total))
        span = max(0, usable - h)
        rng = max(1, bar.maximum() - bar.minimum())
        pos = int(span * (bar.value() - bar.minimum()) / rng)
        self.setGeometry(vp.width() - self.BAR_W - self.MARGIN,
                         self.MARGIN + pos, self.BAR_W, h)
        self.raise_()
        if self._fade > 0.01:
            self.setVisible(True)

    def eventFilter(self, obj, ev) -> bool:                        # noqa: N802
        if ev.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._sync()
        return False

    def paintEvent(self, _e) -> None:                              # noqa: N802
        a = max(0.0, min(1.0, self._fade))
        if a <= 0.01:
            return
        c = QColor(self.pal["scroll"])
        c.setAlphaF(a)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        r = self.BAR_W / 2.0
        p.drawRoundedRect(QRectF(0, 0, self.BAR_W, self.height()), r, r)
        p.end()


class BasePage(QWidget):
    """页面基类。`on_show()` 在每次被切到时调用（含懒创建后的第一次）。

    **内容放在 QScrollArea 里**，这是被真机反馈逼出来的：
    窗口是固定 600 高的，页面内容一旦超出，QVBoxLayout 会把子项压到**低于它们
    的最小高度**——表现是按钮被压扁、文字被裁。加滚动区之后内容再多也只是出现
    滚动条，不会挤压任何控件。
    """

    def __init__(self, host: PanelHost, pal: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("pageScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 原生条一律关掉：常驻可见且占内容宽度，改用下面的浮动指示条。
        # 关掉只影响"条显不显示"，**滚轮照常能滚**。
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 视口默认会用调色板的 Base 色填底，在深色主题下会露出一块浅色。
        # 全局样式表里的 `QWidget { background: transparent }` 已经覆盖了它，
        # 这里再关掉自动填充，双保险。
        self.scroll.viewport().setAutoFillBackground(False)

        self.content = QWidget()
        self.content.setObjectName("pageContent")
        self.content.setAutoFillBackground(False)
        lay = QVBoxLayout(self.content)
        # 内容留白放大：上一版被指出"文本与容器边界太近"
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(14)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll)

        #: 浮动滚动指示条（浮在视口上，不占宽度）
        self.hint = ScrollHint(self.scroll, pal)

        self.layout_ = lay
        self.host = host
        self.pal = pal
        self._built = False

    def on_show(self) -> None:
        if not self._built:
            self._built = True
            self.build()
            self._fit_buttons()

    def _fit_buttons(self) -> None:
        """给按钮一个可靠的下限尺寸。

        真机反馈过"按钮大小不合理、文字被内边距遮盖"。根因是内容超出固定窗口
        高度时布局把子项压到低于最小高度——**加滚动区是主修**（见类文档），
        这里再给一道保险，顺带让同一排按钮高度一致，观感也整齐。

        （不用 QSS 的 min-height：那是内容区下限，padding 与 border 会再叠上去，
        算出来的总高比预期大一截，反而更难看。）
        """
        for b in self.findChildren(QPushButton):
            if b.objectName() in ("action", "primary"):
                b.setMinimumHeight(30)
                b.setMinimumWidth(76)

    def build(self) -> None:                                       # pragma: no cover
        pass

    def apply_palette(self, pal: dict) -> None:
        self.pal = pal
        self.hint.set_palette_colors(pal)

    def shutdown(self, timeout_ms: int = 8000) -> bool:
        """收尾钩子。有后台线程的页面覆盖它。返回是否收尾干净。"""
        return True


class OverviewPage(BasePage):
    """概览：状态一眼看清 + 更新与外观设置。

    托盘 menu 里那些**设置**性质的项搬到了这里，托盘只留必要动作。
    """

    FLAGS = (
        ("prerelease_opt_in", "加入预览计划",
         "跟随官方的候选与尝鲜版本线。官方明示预览版可能含破坏性变更。"),
        ("adaptive_titlebar", "标题栏跟随界面配色",
         "让窗口标题栏跟随 DSH 界面的主色。需要 Windows 11。"),
    )

    def build(self) -> None:
        self.layout_.addWidget(_label("概览", "h1"))

        inst = self.host.install()
        self.kv_shell = KeyValueRow("桌面壳版本", HOST_VERSION, self.pal)
        self.kv_dsh = KeyValueRow("DSH 版本",
                                  inst.version if inst else "未检测", self.pal)
        self.kv_src = KeyValueRow("DSH 来源", inst.source if inst else "—", self.pal)
        self.kv_svc = KeyValueRow("后台服务", "—", self.pal)
        c1 = Card("当前状态", self.pal)
        for w in (self.kv_shell, self.kv_dsh, self.kv_src, self.kv_svc):
            c1.add(w)
        self.layout_.addWidget(c1)

        c2 = Card("更新与外观", self.pal)
        self.toggles = {}
        for name, title, hint in self.FLAGS:
            if name == "adaptive_titlebar" and not dwm.available():
                continue
            row = ToggleRow(title, hint, self.host.flag(name), self.pal,
                            lambda on, n=name: self.host.set_flag(n, on))
            self.toggles[name] = row
            c2.add(row)
        self.layout_.addWidget(c2)

        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(16, 2, 16, 2)
        row.setSpacing(8)
        self.btn_update = QPushButton("检查 DSH 更新")
        self.btn_update.setObjectName("action")
        self.btn_update.clicked.connect(lambda: self.host.action("check_update"))
        self.btn_stable = QPushButton("回归稳定版")
        self.btn_stable.setObjectName("action")
        self.btn_stable.clicked.connect(lambda: self.host.action("go_stable"))
        self.btn_rollback = QPushButton("回滚到上一版本")
        self.btn_rollback.setObjectName("action")
        self.btn_rollback.clicked.connect(lambda: self.host.action("rollback"))
        for b in (self.btn_update, self.btn_stable, self.btn_rollback):
            row.addWidget(b)
        row.addStretch(1)
        self.layout_.addWidget(holder)

        holder2 = QWidget()
        acts = QHBoxLayout(holder2)
        acts.setContentsMargins(16, 0, 16, 0)
        acts.setSpacing(8)
        btn_web = QPushButton("打开 DSH 界面")
        btn_web.setObjectName("primary")
        btn_web.clicked.connect(lambda: self.host.open_web())
        btn_log = QPushButton("打开日志目录")
        btn_log.setObjectName("action")
        btn_log.clicked.connect(lambda: self.host.open_path(self.host.log_dir()))
        acts.addWidget(btn_web)
        acts.addWidget(btn_log)
        acts.addStretch(1)
        self.layout_.addWidget(holder2)
        self.layout_.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        inst = self.host.install()
        h = self.host.handle()
        self.kv_dsh.set(inst.version if inst else "未检测")
        self.kv_src.set(inst.source if inst else "—")
        if h and h.port:
            alive = contract.tcp_open(h.host or "127.0.0.1", h.port, timeout=1.0)
            self.kv_svc.set(f"运行中 · 端口 {h.port}" if alive
                            else f"已停止（端口 {h.port}）")
        else:
            self.kv_svc.set("未启动")
        for name, row in getattr(self, "toggles", {}).items():
            row.set_checked(self.host.flag(name))

    def on_show(self) -> None:
        if not self._built:
            super().on_show()
        else:
            self.refresh()

    def apply_palette(self, pal: dict) -> None:
        super().apply_palette(pal)
        if not self._built:
            return
        for w in (self.kv_shell, self.kv_dsh, self.kv_src, self.kv_svc):
            w.set_palette_colors(pal)
        for c in self.findChildren(Card):
            c.set_palette_colors(pal)
        for r in getattr(self, "toggles", {}).values():
            r.set_palette_colors(pal)


class DeployPage(BasePage):
    """本机部署：**引导**，不代劳。

    为什么从"帮你装"改成"告诉你怎么装"
    ----------------------------------
    替用户装 Node / 装 DSH，意味着我们要跟着上游的下载地址、包名、安装参数一起改，
    上游一动我们就得重新打包发版。改成引导后，网址与命令放在适配表里（可外部覆盖），
    我们只负责**判断每一步成没成**——那是我们自己的契约，不会因为上游换网址而失效。
    """

    def build(self) -> None:
        self.layout_.addWidget(_label("本机部署", "h1"))
        self.layout_.addWidget(_label(
            "这一步由你来做，我们只负责告诉你做没做成。"
            "每步做完回到「状态检测」页点一次重新检测，就能看到结果。"))

        links = guide_links()
        c1 = Card("第一步 · 安装 Node.js 运行环境", self.pal)
        c1.add(KeyValueRow("状态", "见「状态检测」页的 Node.js 一行", self.pal))
        c1.add(self._link_row("下载页", links["node_download"]))
        c1.add(self._copy_row("命令行安装", links["node_winget"]))
        self.layout_.addWidget(c1)

        c2 = Card("第二步 · 安装 DeepSeek Harness", self.pal)
        c2.add(self._copy_row("命令行安装", f"npm install -g {updater.PKG_NAME}"))
        c2.add(self._link_row("项目主页", links["dsh_site"]))
        c2.add(self._link_row("使用文档", links["dsh_docs"]))
        self.layout_.addWidget(c2)

        # 这条原本是托盘里的「准备运行环境…」。挪过来而不是删掉——能力还在，
        # 只是从"日常入口"降级成"不想自己装时的兜底"，且必须点进这一页才看得到。
        c3 = Card("不想自己装？", self.pal)
        c3.add_text("可以让桌面壳代劳：自动安装 Node.js 与 DeepSeek Harness。"
                    "需要联网，中途会弹出进度窗口。")
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(16, 4, 16, 10)
        btn = QPushButton("自动准备运行环境")
        btn.setObjectName("action")
        btn.clicked.connect(lambda: self.host.action("provision"))
        lay.addWidget(btn)
        lay.addStretch(1)
        c3.add(holder)
        self.layout_.addWidget(c3)
        self.layout_.addStretch(1)

    def _link_row(self, title: str, url: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(10)
        t = QLabel(title)
        t.setObjectName("kv_k")
        t.setFixedWidth(128)
        v = QLabel(url or "（未配置）")
        v.setObjectName("kv_v")
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.setWordWrap(True)
        btn = QPushButton("打开")
        btn.setObjectName("action")
        btn.setEnabled(bool(url))
        btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        lay.addWidget(t)
        lay.addWidget(v, 1)
        lay.addWidget(btn)
        return w

    def _copy_row(self, title: str, cmd: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(10)
        t = QLabel(title)
        t.setObjectName("kv_k")
        t.setFixedWidth(128)
        v = QLabel(cmd or "（未配置）")
        v.setObjectName("kv_v")
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.setWordWrap(True)
        btn = QPushButton("复制")
        btn.setObjectName("action")
        # 图标显式给尺寸：默认 iconSize 会取样式建议值，和 13px 文字放一起偏高
        btn.setIconSize(QSize(14, 14))
        btn.setIcon(icons.qicon("copy", self.pal["dim"], 14))
        btn.setEnabled(bool(cmd))
        btn.clicked.connect(lambda: self._copy(cmd, btn))
        lay.addWidget(t)
        lay.addWidget(v, 1)
        lay.addWidget(btn)
        return w

    def _copy(self, text: str, btn: QPushButton) -> None:
        QGuiApplication.clipboard().setText(text)
        # 就地反馈，不弹对话框——复制这类动作不该打断用户
        old = btn.text()
        btn.setText("已复制")
        QTimer.singleShot(1200, lambda: btn.setText(old))


class ConnectPage(BasePage):
    """连接（远程入口，占位）。"""

    def build(self) -> None:
        self.layout_.addWidget(_label("连接", "h1"))
        self.layout_.addWidget(_label("远程连接还在规划中，这里先说明我们打算怎么做。"))

        c1 = Card("规划中的形态", self.pal)
        # 用一段文本而不是三条独立 label：独立 label 之间的间距只能靠各自的
        # 内边距堆，看起来是散的；整段交给 QLabel 自己排版更稳。
        c1.add_text("\n".join((
            "· 把另一台设备上的 DSH 呈现在本机窗口里，多个设备各开一个窗口",
            "· 目标设备可以还没启动 DSH——由连接过程带着把它拉起来",
            "· 连接方式依托你已有的通道（SSH 隧道或组网），不额外占用端口",
        )))
        self.layout_.addWidget(c1)

        c2 = Card("一条不会变的底线", self.pal)
        c2.add_text(
            "我们不会让 DSH 监听外网，也不会改它的绑定设置。"
            "DSH 的接口上有执行命令的能力，官方正是因为这个才把监听范围锁在本机"
            "（它自己的启动器里写着：绑定到全网段等于把远程代码执行暴露到网络上）。"
            "绕过这条限制，等于替官方做它明确拒绝的安全决定，而且上游一加校验我们就得崩。")
        c2.add_text(
            "所以传输由我们这一侧负责：DSH 仍然只听本机，由隧道把它的本机端口借过来"
            "——对外只需要一条通道。")
        self.layout_.addWidget(c2)
        self.layout_.addStretch(1)


class StatusPage(BasePage):
    """状态检测：只报告，不修复。"""

    def build(self) -> None:
        head_w = QWidget()
        head = QHBoxLayout(head_w)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(_label("状态检测", "h1"))
        head.addStretch(1)
        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.setObjectName("primary")
        self.btn_recheck.setIcon(
            icons.qicon("refresh-cw", self.pal["primary_text"], 15))
        self.btn_recheck.clicked.connect(self.run_checks)
        self.btn_channel = QPushButton("查询官方通道")
        self.btn_channel.setObjectName("action")
        self.btn_channel.clicked.connect(self.run_channel)
        head.addWidget(self.btn_channel)
        head.addWidget(self.btn_recheck)
        self.layout_.addWidget(head_w)

        labels = dict(CHECK_ROWS)
        self.rows: dict[str, CheckRow] = {}
        self.cards: list[Card] = []
        for gname, keys in (("运行环境", ("node", "npm", "dsh")),
                            ("数据", ("home", "home_w")),
                            ("服务与版本", ("svc", "channel"))):
            card = Card(gname, self.pal)
            for k in keys:
                row = CheckRow(k, labels[k], self.pal)
                self.rows[k] = row
                card.add(row)
            self.cards.append(card)
            self.layout_.addWidget(card)
        self.layout_.addStretch(1)

        self._worker = None
        self._chan_worker = None

    def on_show(self) -> None:
        if not self._built:
            super().on_show()
        self.run_checks()

    def run_checks(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        for k in LOCAL_KEYS:
            self.rows[k].dot.set_state(STATE_UNKNOWN)
            self.rows[k].value_lbl.setText("检测中…")
        self.btn_recheck.setEnabled(False)
        self._worker = ChecksWorker(self.host, self)
        self._worker.done.connect(self._on_checks)
        self._worker.start()

    def _on_checks(self, results: list) -> None:
        for res in results:
            row = self.rows.get(res.key)
            if row is None:
                continue     # 例如 "error" 这种额外结果，界面暂不展示
            row.apply(res)
        self.btn_recheck.setEnabled(True)

    def run_channel(self) -> None:
        """查官方通道。联网，所以单独一个按钮，不跟着自动检测跑。"""
        if self._chan_worker is not None and self._chan_worker.isRunning():
            return
        row = self.rows["channel"]
        row.dot.set_state(STATE_UNKNOWN)
        row.value_lbl.setText("查询中…")
        self.btn_channel.setEnabled(False)
        self._chan_worker = ChannelWorker(self.host.install(), self)
        self._chan_worker.done.connect(self._on_channel)
        self._chan_worker.start()

    def _on_channel(self, res) -> None:
        self.rows["channel"].apply(res)
        self.btn_channel.setEnabled(True)

    def shutdown(self, timeout_ms: int = 8000) -> bool:
        """等后台线程跑完。

        为什么必须显式等：QThread 在解释器退出时还活着会让 Qt 直接 abort，
        而 abort 会**丢掉未刷出的 stdout 缓冲**——表现是"脚本没有任何输出、
        退出码非零"，排查时极具误导性（真因在 C++ 层）。CI 也会因此判失败。

        注意 `QThread.quit()` 只退事件循环，**打断不了 run() 里的同步代码**，
        所以这里用 `wait()` 等它自己跑完。
        """
        ok = True
        for attr in ("_worker", "_chan_worker"):
            w = getattr(self, attr, None)
            if w is not None and w.isRunning() and not w.wait(timeout_ms):
                ok = False
        return ok

    def apply_palette(self, pal: dict) -> None:
        super().apply_palette(pal)
        if not self._built:
            return
        for c in self.cards:
            c.set_palette_colors(pal)
        for r in self.rows.values():
            r.set_palette_colors(pal)
        self.btn_recheck.setIcon(icons.qicon("refresh-cw", pal["primary_text"], 15))


class AboutPage(BasePage):
    """关于。"""

    def build(self) -> None:
        self.layout_.addWidget(_label("关于", "h1"))
        inst = self.host.install()
        rel = self.host.host_release()

        c = Card("版本", self.pal)
        c.add(KeyValueRow("桌面壳", HOST_VERSION, self.pal))
        c.add(KeyValueRow("DSH", inst.version if inst else "未检测", self.pal))
        c.add(KeyValueRow("可更新到", rel or "（未查询）", self.pal))
        self.layout_.addWidget(c)

        c2 = Card("来源与许可", self.pal)
        c2.add(KeyValueRow("桌面壳仓库", HOST_REPO, self.pal))
        c2.add(KeyValueRow("DSH 依赖", updater.PKG_NAME, self.pal))
        c2.add_text("界面图标来自 Lucide（ISC 许可），已内联为源码，"
                    "运行时不读外部文件。")
        c2.add_text("配色取自 DSH 官方前端的语义令牌，跟随界面主题切换。")
        self.layout_.addWidget(c2)

        c3 = Card("目录", self.pal)
        c3.add(KeyValueRow("数据根", config.data_root(), self.pal))
        c3.add(KeyValueRow("DSH_HOME",
                           contract.resolve_home() or "（由 DSH 自行决定）", self.pal))
        self.layout_.addWidget(c3)
        self.layout_.addStretch(1)


# ------------------------------------------------------------------ 面板窗口

class TitleBar(QWidget):
    """自绘标题栏：标题 + 最小化 + 关闭，整条可拖动。

    无边框窗口只需实现拖动这一件事（尺寸固定，没有 resize/最大化/Snap 的问题）。
    `startSystemMove()` 走 Win32 的 SC_MOVE，保留系统行为——**不要自己算鼠标位移**，
    否则多显示器、DPI 变化、贴边之类都会出问题。
    """

    def __init__(self, title: str, pal: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("titlebar")
        self.setFixedHeight(TITLEBAR_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 0, 0)
        lay.setSpacing(0)
        lbl = QLabel(title)
        lbl.setObjectName("panelTitle")
        lay.addWidget(lbl)
        lay.addStretch(1)

        self.btn_min = _win_button("minus", "最小化", pal)
        self.btn_close = _win_button("x", "关闭", pal, danger=True)
        lay.addWidget(self.btn_min)
        lay.addWidget(self.btn_close)

    def set_palette_colors(self, pal: dict) -> None:
        self.btn_min.setIcon(icons.qicon("minus", pal["muted"], 16))
        self.btn_close.setIcon(icons.qicon("x", pal["muted"], 16))

    def mousePressEvent(self, e) -> None:                          # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(e)


def _win_button(icon_name: str, tip: str, pal: dict, danger: bool = False) -> QToolButton:
    """标题栏上的小按钮。

    两个按钮**共用 objectName `winBtn`**，只靠动态属性区分（关闭键悬停变红）。
    之前给关闭键单独起名 `winBtnClose`，结果样式表里只有它的 hover 规则、没有基础
    规则，它就被系统默认样式接管、渲染成一块浅色底——**只在悬停时才好看，平时是白块**。
    共用 objectName 能让基础规则必然命中。
    """
    b = QToolButton()
    b.setObjectName("winBtn")
    b.setProperty("danger", danger)
    b.setIcon(icons.qicon(icon_name, pal["muted"], 16))
    b.setIconSize(QSize(16, 16))
    b.setFixedSize(44, TITLEBAR_H)
    b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class ControlPanel(QWidget):
    """控制面板主窗口：无边框 + 固定尺寸 880×600 + 自绘圆角。

    为什么自绘圆角而不是等 DWM
    --------------------------
    DWM 的窗口圆角是窗口管理器合成的：`PrintWindow` 抓不到、屏幕抓图在部分环境下
    也不可靠，于是"设成功但没生效"根本查不出来。自绘的圆角在离屏渲染里就能验角像素，
    是可回归的。代价是丢掉系统阴影，但官方风格本来就是**平面 + 描边**而不是投影，
    所以这里用 1px 描边替代。
    """

    PAGES = (
        ("layout-dashboard", "概览", OverviewPage),
        ("package", "本机部署", DeployPage),
        ("plug", "连接", ConnectPage),
        ("activity", "状态检测", StatusPage),
        ("info", "关于", AboutPage),
    )

    def __init__(self, host: PanelHost, parent=None):
        super().__init__(parent, Qt.WindowType.Window
                         | Qt.WindowType.FramelessWindowHint)
        self.host = host
        self.setObjectName("panel")
        self.setWindowTitle("DSH Launcher 控制面板")
        self.setFixedSize(PANEL_W, PANEL_H)
        # 半透明底是自绘圆角的前提：四个角要能透出桌面
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._theme = host.theme()
        self.pal = palette(self._theme)
        self.setStyleSheet(build_style(self.pal))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.titlebar = TitleBar("DSH Launcher 控制面板", self.pal, self)
        self.titlebar.btn_min.clicked.connect(self.showMinimized)
        self.titlebar.btn_close.clicked.connect(self.close)
        root.addWidget(self.titlebar)

        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)

        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(SIDEBAR_W)
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(10, 6, 10, 0)
        side_lay.setSpacing(2)
        self.navs: list[NavItem] = []
        for i, (icon_name, title, _cls) in enumerate(self.PAGES):
            item = NavItem(icon_name, title, self.pal)
            item.clicked.connect(lambda _c=False, i=i: self.goto(i))
            self.navs.append(item)
            side_lay.addWidget(item)
        side_lay.addStretch(1)
        mid.addWidget(side)

        self.stack = QStackedWidget()
        self.stack.setObjectName("content")
        self.pages: dict[int, BasePage] = {}
        self._pending: dict[int, type] = {}
        for index, (_icon, _title, cls) in enumerate(self.PAGES):
            # 先放占位控件，切到该页时才真正构造——面板要开得快，
            # 而五个页面里有联网和遍历文件系统的，不该在打开面板时就全跑一遍。
            holder = QWidget()
            holder.setObjectName("content")
            self.stack.addWidget(holder)
            self._pending[index] = cls
        mid.addWidget(self.stack, 1)
        root.addLayout(mid, 1)

        self.statusbar = QWidget()
        self.statusbar.setObjectName("statusbar")
        self.statusbar.setFixedHeight(STATUSBAR_H)
        sb = QHBoxLayout(self.statusbar)
        sb.setContentsMargins(16, 0, 16, 0)
        sb.setSpacing(8)
        self.status_dot = StatusDot(STATE_UNKNOWN, 12, self.pal)
        self.status_lbl = QLabel("就绪")
        self.status_lbl.setObjectName("statusText")
        sb.addWidget(self.status_dot)
        sb.addWidget(self.status_lbl)
        sb.addStretch(1)
        root.addWidget(self.statusbar)

        self.current = -1
        self.goto(0)
        self.refresh_statusbar()

    # ---- 窗口外观 ----

    def paintEvent(self, _e) -> None:                             # noqa: N802
        """自绘圆角底 + 侧栏/标题栏/状态条色块。

        三块色带都在**圆角裁剪路径内**绘制，所以四个角能正确透出桌面。
        子控件一律透明底（见样式表），不会把角盖成直角——这一点很关键，
        漏一个就会看到直角把圆角盖住。
        """
        pal = self.pal
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        path = QPainterPath()
        # 0.5 偏移让 1px 描边落在像素中心，否则会糊成 2px
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0),
                            R_WINDOW, R_WINDOW)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(pal["bg"]))
        p.drawPath(path)

        p.save()
        p.setClipPath(path)
        side = QColor(pal["side"])
        p.fillRect(0, 0, SIDEBAR_W, h, side)
        p.fillRect(SIDEBAR_W, 0, w - SIDEBAR_W, TITLEBAR_H, side)
        p.fillRect(0, h - STATUSBAR_H, w, STATUSBAR_H, side)
        p.restore()

        p.setPen(QPen(QColor(pal["line"]), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()

    def apply_theme(self, theme: str) -> None:
        """切换到 light / dark。主窗口在标题栏色判定变化时调用。"""
        if theme not in PALETTES or theme == self._theme:
            return
        self._theme = theme
        self.pal = palette(theme)
        self.setStyleSheet(build_style(self.pal))
        self.titlebar.set_palette_colors(self.pal)
        for nav in self.navs:
            nav.set_palette_colors(self.pal)
        self.status_dot.set_palette_colors(self.pal)
        for page in self.pages.values():
            page.apply_palette(self.pal)
        # 自绘的底色不在样式表里，必须显式重绘
        self.update()

    def theme(self) -> str:
        return self._theme

    # ---- 页面切换 ----
    def _page(self, index: int) -> BasePage:
        page = self.pages.get(index)
        if page is None:
            cls = self._pending[index]
            page = cls(self.host, self.pal)
            holder = self.stack.widget(index)
            self.stack.removeWidget(holder)
            holder.deleteLater()
            self.stack.insertWidget(index, page)
            self.pages[index] = page
        return page

    def goto(self, index: int) -> None:
        if index < 0 or index >= len(self.PAGES):
            return
        page = self._page(index)
        self.stack.setCurrentIndex(index)
        for i, nav in enumerate(self.navs):
            nav.setChecked(i == index)
        self.current = index
        page.on_show()
        # 动效原语之一：整页短淡入（不做逐条错开——那会看起来像文字在浮动）
        fade_in(page, MS_PAGE)
        self.refresh_statusbar()

    # ---- 状态条 ----
    def refresh_statusbar(self) -> None:
        inst = self.host.install()
        h = self.host.handle()
        bits = [f"DSH {inst.version}" if inst else "DSH 未检测"]
        if h and h.port and contract.tcp_open(h.host or "127.0.0.1", h.port,
                                              timeout=1.0):
            bits.append(f"服务运行中 · 端口 {h.port}")
            self.status_dot.set_state(STATE_OK)
        else:
            bits.append("服务未运行")
            self.status_dot.set_state(STATE_WARN)
        self.status_lbl.setText("　·　".join(bits))

    def shutdown(self, timeout_ms: int = 8000) -> bool:
        """收尾：等所有已构建页面上的后台线程结束。

        退出应用前**必须**调用，否则线程还活着会让 Qt abort（见 StatusPage.shutdown）。
        """
        ok = True
        for page in list(self.pages.values()):
            if not page.shutdown(timeout_ms):
                ok = False
        return ok

    def keyPressEvent(self, e) -> None:                            # noqa: N802
        # Esc 关闭。面板是固定尺寸的浮层，Esc 是最自然的退出方式。
        if e.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)


# ------------------------------------------------------------------ 自测钩子

def selftest_checks() -> list:
    """给 tools/gui_selftest.py 用的纯逻辑断言，返回失败项列表。

    只验不依赖真实 DSH 的部分。其中几条是**像素级**的，因为那类问题
    （按钮底色退回系统默认、圆角没生效、切主题没重绘）用属性断言根本看不出来。
    """
    from PySide6.QtWidgets import QApplication
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                               ("  " + extra) if extra else ""))
        if not ok:
            failures.append(name)

    def loop_events(application, ms: int) -> None:
        """把事件循环推进 ms 毫秒——动画要真的跑，断言才有意义。"""
        import time as _time
        end = _time.monotonic() + ms / 1000.0
        while _time.monotonic() < end:
            application.processEvents()
            _time.sleep(0.01)

    app = QApplication.instance() or QApplication([])              # noqa: F841

    # --- 配色必须逐条等于官方令牌 ---
    # 这是"防割裂"的硬约束：面板配色是照抄官方语义令牌来的，一旦有人手改成
    # "看起来差不多"的颜色，两者放在一起就会露怯。所以把官方原值钉在这里。
    official = {
        "light": {"bg": "#ffffff", "side": "#f9fafb", "text": "#0f1115",
                  "dim": "#61666b", "accent": "#4176e6", "primary_fill": "#0f1115"},
        "dark": {"bg": "#151517", "side": "#1b1b1c", "text": "#f9fafb",
                 "dim": "#cfd3d6", "accent": "#679efe", "primary_fill": "#f9fafb"},
    }
    for theme, expect in official.items():
        pal = palette(theme)
        bad = ["%s=%s(应 %s)" % (k, pal[k], v) for k, v in expect.items()
               if pal[k].lower() != _css(v).lower()]
        check("%s 主题关键色等于官方令牌" % theme, not bad, "、".join(bad))

    check("CSS #RRGGBBAA 正确转成 Qt #AARRGGBB",
          _css("#0000001a") == "#1a000000", _css("#0000001a"))
    check("六位色值不被误改", _css("#ffffff") == "#ffffff")

    # 图标集完整性
    missing = icons.missing([i for i, _t, _c in ControlPanel.PAGES]
                            + list(STATE_ICON.values()))
    check("页面与状态图标齐全", not missing,
          "缺：" + "、".join(missing) if missing else "")

    bad_icons = []
    for name in icons.SVG:
        r = icons.ink_ratio(name, 48, "#000000")
        if not (0.02 < r < 0.60):
            bad_icons.append("%s=%.3f" % (name, r))
    check("全部图标渲染比例正常", not bad_icons, "、".join(bad_icons[:4]))

    host = PanelHost(install=None, handle=None, theme=lambda: "dark")
    panel = ControlPanel(host)
    panel.resize(PANEL_W, PANEL_H)
    panel.show()
    loop_events(app, 260)

    check("固定尺寸为 880x600",
          (panel.width(), panel.height()) == (PANEL_W, PANEL_H),
          "%sx%s" % (panel.width(), panel.height()))
    check("无边框", bool(panel.windowFlags() & Qt.WindowType.FramelessWindowHint))
    check("尺寸被锁定（不可缩放）",
          panel.minimumSize() == panel.maximumSize() == panel.size())
    check("开了半透明底（自绘圆角的前提）",
          panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
    check("页签数量与定义一致", len(panel.navs) == len(ControlPanel.PAGES),
          "%d 个" % len(panel.navs))
    titles = [n.text().strip() for n in panel.navs]
    check("页签名正确", titles == ["概览", "本机部署", "连接", "状态检测", "关于"],
          "、".join(titles))

    try:
        for i in range(len(ControlPanel.PAGES)):
            panel.goto(i)
        check("五个页面都能切换并完成构建",
              len(panel.pages) == len(ControlPanel.PAGES),
              "已构建 %d 个" % len(panel.pages))
    except Exception as e:                                       # noqa: BLE001
        check("五个页面都能切换并完成构建", False,
              "%s: %s" % (type(e).__name__, e))

    sp = panel.pages.get(3)
    check("状态检测页覆盖全部预期项",
          sp is not None and set(sp.rows.keys()) == {k for k, _n in CHECK_ROWS},
          "、".join(sp.rows.keys()) if sp is not None else "状态页未构建")

    # --- 圆角：像素级 ---
    # grab() 渲染的是控件自己的绘制（含透明通道），所以圆角外应当是**透明**的。
    # 这条能一次抓住三件事：忘了开半透明底、paintEvent 没画圆角、
    # 以及某个子控件拿不透明底把角盖成直角。
    panel.goto(0)
    loop_events(app, 300)
    shot = panel.grab().toImage()
    W, H = shot.width(), shot.height()
    corners = {"左上": (1, 1), "右上": (W - 2, 1),
               "左下": (1, H - 2), "右下": (W - 2, H - 2)}
    solid = []
    for name, (x, y) in corners.items():
        c = shot.pixelColor(x, y)
        if c.alpha() > 24:
            solid.append("%s a=%d %s" % (name, c.alpha(), c.name()))
    check("窗口四角是透明的（圆角生效）", not solid, "、".join(solid))
    check("窗口正中是不透明的（圆角没画过头）",
          shot.pixelColor(W // 2, H // 2).alpha() > 240)

    # --- 标题栏按钮底色必须是深色，不能是系统默认浅色 ---
    bad_light = []
    for btn in (panel.titlebar.btn_min, panel.titlebar.btn_close):
        g = btn.geometry()
        x, y = max(0, g.x() + 6), g.y() + g.height() // 2
        if x < W and y < H:
            c = shot.pixelColor(x, y)
            if c.lightness() > 120:
                bad_light.append("%s=%s" % (btn.toolTip(), c.name()))
    check("标题栏按钮底色是深色（不是系统默认浅色）", not bad_light,
          "、".join(bad_light))

    # --- 主题切换 ---
    panel.apply_theme("light")
    loop_events(app, 220)
    shot2 = panel.grab().toImage()
    mid = shot2.pixelColor(W // 2, H // 2)
    check("切到浅色主题后中央底色变浅", mid.lightness() > 200, mid.name())
    check("切主题后四角仍然是透明的",
          all(shot2.pixelColor(x, y).alpha() <= 24 for x, y in corners.values()))
    panel.apply_theme("dark")
    loop_events(app, 220)
    check("切回深色主题",
          panel.theme() == "dark"
          and panel.grab().toImage().pixelColor(W // 2, H // 2).lightness() < 80)

    # --- 快速连点导航后当前页必须真的可见 ---
    for i in (0, 1, 2, 3, 4, 0, 1):
        panel.goto(i)
    waited = 0
    cur = panel.pages.get(panel.current)
    while cur is not None and cur.graphicsEffect() is not None and waited < 2000:
        loop_events(app, 50)
        waited += 50
    eff = cur.graphicsEffect() if cur is not None else None
    check("快速连点导航后当前页恢复可见（淡入能收尾）",
          cur is not None and eff is None,
          "等待 %dms，effect=%s" % (waited, type(eff).__name__ if eff else "已撤掉"))

    # --- 真机反馈过的两个排版问题，逐页扫 ---
    #   ① 按钮被压扁、文字被内边距裁掉
    #   ② 卡片里的纯文本贴到卡片边框上
    # 这两条只能等布局真的跑完再量，所以逐页 goto + 推进事件循环。
    squeezed, touching = [], []
    for idx in range(len(ControlPanel.PAGES)):
        panel.goto(idx)
        loop_events(app, 280)
        page = panel.pages[idx]

        for b in page.findChildren(QPushButton):
            if b.objectName() not in ("action", "primary"):
                continue
            if b.height() < b.sizeHint().height():
                squeezed.append("第%d页 %r %d<%d"
                                % (idx, b.text()[:10], b.height(),
                                   b.sizeHint().height()))

        for card in page.findChildren(Card):
            for lbl in card.findChildren(QLabel):
                # 只看**直接挂在卡片上**的文本：行控件内部的 label 自己有边距
                if lbl.parentWidget() is not card:
                    continue
                cm = lbl.contentsMargins()
                if cm.left() < 12 and "padding" not in lbl.styleSheet():
                    touching.append("第%d页 %r 左边距=%d"
                                    % (idx, lbl.text()[:14], cm.left()))

    check("没有按钮被压到低于自身最小高度", not squeezed, "、".join(squeezed[:4]))
    check("卡片里的纯文本没有贴边框（左右 >= 12px）", not touching,
          "、".join(touching[:4]))

    # --- 内容超出固定窗口高度时必须可滚动，而不是挤压控件 ---
    #  这是上面第一条的**根因**：QVBoxLayout 在空间不足时会把子项压到低于最小值。
    scrolled = []
    for idx in range(len(ControlPanel.PAGES)):
        page = panel.pages[idx]
        if not isinstance(page.scroll, QScrollArea):
            scrolled.append("第%d页没有滚动区" % idx)
        elif not page.scroll.widgetResizable():
            scrolled.append("第%d页滚动区不可随窗口调整" % idx)
        elif page.scroll.verticalScrollBarPolicy() != \
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
            # 原生条常驻可见还占内容宽度，老大明确不要
            scrolled.append("第%d页原生滚动条没关掉" % idx)
    check("每页都有滚动区，且原生滚动条已关闭", not scrolled, "、".join(scrolled))

    # --- 浮动指示条：该出现时出现、该消失时消失、别越界 ---
    hint_bad = []
    for idx in range(len(ControlPanel.PAGES)):
        panel.goto(idx)
        loop_events(app, 300)
        page, bar = panel.pages[idx], panel.pages[idx].scroll.verticalScrollBar()
        vp = page.scroll.viewport()
        if bar.maximum() <= 0:
            # 内容不超高：条子必须完全不出现（也不占位）
            if page.hint.isVisible():
                hint_bad.append("第%d页内容不超高却显示了指示条" % idx)
            continue
        # 滚到底：条子应可见，且**完全落在视口内**（滑到底不能越过下沿）
        bar.setValue(bar.maximum())
        loop_events(app, 120)
        g = page.hint.geometry()
        if not page.hint.isVisible():
            hint_bad.append("第%d页滚动后指示条没出现" % idx)
        if not (0 <= g.top() and g.bottom() <= vp.height()
                and g.right() <= vp.width()):
            hint_bad.append("第%d页指示条越界 %s（视口 %dx%d）"
                            % (idx, g, vp.width(), vp.height()))
        # 浮层绝不能吃鼠标事件，否则会挡住它下面的按钮
        if not page.hint.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents):
            hint_bad.append("第%d页指示条会挡鼠标" % idx)
    check("浮动滚动指示条行为正确（不越界、不挡鼠标、不需要时不出现）",
          not hint_bad, "、".join(hint_bad[:4]))

    # --- 界面里不许出现 Emoji ---
    import re
    emoji = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
    hits = [n.text() for n in panel.navs if emoji.search(n.text())]
    for page in panel.pages.values():
        for lbl in page.findChildren(QLabel):
            if emoji.search(lbl.text()):
                hits.append(lbl.text()[:30])
    check("面板不含 Emoji", not hits, "、".join(hits[:3]))

    # --- 托盘搬过来的设置项必须在面板里存在 ---
    ov = panel.pages.get(0)
    toggles = getattr(ov, "toggles", {}) if ov is not None else {}
    check("概览页承载了从托盘搬来的开关",
          "prerelease_opt_in" in toggles, "、".join(toggles) or "无")

    check("后台检测线程能干净收尾", panel.shutdown(15000))

    panel.close()
    panel.deleteLater()
    return failures
