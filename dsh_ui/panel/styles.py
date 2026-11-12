"""面板的视觉常量与样式生成。所有颜色、尺寸、动效时长的**唯一来源**。

为什么单独成文件：以前这些散在 3000 行的面板文件开头，任何一处想改配色都要
先滚到第 120 行。拆出来之后，"改主题"变成改一个文件的事。
"""

from __future__ import annotations
import os
import threading
import time
from string import Template

from PySide6.QtCore import (QAbstractAnimation, QEvent, Property, QEasingCurve,
                            QPointF, QPropertyAnimation, QRectF, QSize, Qt,
                            QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QGuiApplication, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame,
                               QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

import dsh_icons as icons
from dsh_host import (compat, config, contract, dwm, netenv, procmem, provision,
                      remote, storage, updater)
from dsh_host.logutil import host_log
from dsh_host.version import HOST_NAME, HOST_REPO, HOST_VERSION


PANEL_W, PANEL_H = 880, 600

FONT = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'

TITLEBAR_H = 40

SIDEBAR_W = 208

STATUSBAR_H = 28

R_WINDOW = 16

R_CARD = 12

R_CONTROL = 8

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

MEM_SAMPLE_MS = 2000

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


def human_mem(mb: float) -> str:
    """MB 数值 → 界面显示值。

    超过 1 GB 换单位：1645 MB 这种读数在界面上没有意义，
    而精确到 0.1 GB（1.6 GB）足够判断"是不是又涨了"。
    """
    try:
        v = float(mb)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "0 MB"
    if v >= 1024:
        return "%.1f GB" % (v / 1024.0)
    return "%.0f MB" % v


def palette(theme: str) -> dict:
    return PALETTES.get(theme) or PALETTE_DARK

def state_color(pal: dict, state: str) -> str:
    return {"ok": pal["ok"], "bad": pal["bad"],
            "warn": pal["warn"]}.get(state, pal["muted"])

def build_style(pal: dict) -> str:
    return _STYLE.substitute(
        text=pal["text"], dim=pal["dim"], muted=pal["muted"], hover=pal["hover"],
        line=pal["line"], bad=pal["bad"], font=FONT, r_ctrl=R_CONTROL,
        r_win=R_WINDOW,
        btn_fill=pal["btn_fill"], btn_hover=pal["btn_hover"],
        primary_fill=pal["primary_fill"], primary_hover=pal["primary_hover"],
        primary_text=pal["primary_text"], bg=pal["bg"],
    )

def card_style(pal: dict) -> str:
    return _CARD_STYLE.substitute(card=pal["card"], line=pal["line"],
                                  r_card=R_CARD)

def nav_style(pal: dict) -> str:
    return _NAV_STYLE.substitute(dim=pal["dim"], text=pal["text"],
                                 nav_hover=pal["nav_hover"],
                                 nav_active=pal["nav_active"], font=FONT,
                                 r_ctrl=R_CONTROL)

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

def _tooltip_html(text: str) -> str:
    """把卡片说明转成 QToolTip 能吃的富文本。

    只做转义和换行——提示框里塞不下复杂排版。转义是必须的：说明文字里会出现
    `<select>` 这类写法，不转义会被当成 HTML 标签吞掉。
    """
    esc = (text.strip().replace("&", "&amp;").replace("<", "&lt;")
           .replace(">", "&gt;"))
    return esc.replace("\n", "<br>")

def _label(text: str, kind: str = "body") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(kind)
    lbl.setWordWrap(True)
    return lbl


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

/* 兜底：不带 objectName 的 QLabel 也必须跟主题走。
   少了这一条，暗色下会出现"黑底黑字"——控件回落系统默认色（黑），
   而背景是深色；浅色下偶发"白底白字"则是控件自带白底没被覆盖。
   具名规则（#kv_k 等）优先级更高，仍然按需覆盖（2026-10-20 老大反馈）。 */
QLabel             { color: $text; font-family: $font; font-size: 13px; }
QTextBrowser, QTextEdit, QPlainTextEdit {
    color: $text; background: transparent; border: none; }

QLabel#panelTitle  { color: $text; font-family: $font; font-size: 13px; }
QLabel#statusText  { color: $muted; font-family: $font; font-size: 12px; }
QLabel#h1          { color: $text; font-family: $font; font-size: 19px; font-weight: 600; }
QLabel#h2          { color: $text; font-family: $font; font-size: 14px; font-weight: 600; }
QLabel#body        { color: $dim;  font-family: $font; font-size: 13px; }
QLabel#kv_k        { color: $dim;  font-family: $font; font-size: 13px; }
QLabel#kv_v        { color: $text; font-family: $font; font-size: 13px; }
/* 只加字重、不改尺寸：合计要突出，但不能破坏与分项的同一条左对齐线 */
QLabel#kv_v_strong { color: $text; font-family: $font; font-size: 13px; font-weight: 600; }

QToolButton#winBtn { border: none; background: transparent; border-radius: 0px; }
QToolButton#winBtn:hover { background: $hover; }
/* 关闭键贴在窗口右上角：它的红色悬停底必须自己带一个同半径的圆角，
   否则那块矩形会盖在窗口自绘的圆角之上——子控件不会被父控件的
   paintEvent 裁剪路径约束，"溢出"就是这么来的。 */
QToolButton#winBtn[danger="true"] { border-top-right-radius: ${r_win}px; }
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
