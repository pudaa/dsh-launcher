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

from PySide6.QtCore import QEasingCurve, Qt
from PySide6.QtWidgets import (QDialog, QGraphicsDropShadowEffect, QHBoxLayout,
                               QLabel, QMenu, QPushButton, QVBoxLayout, QWidget)

import dsh_icons as icons
from dsh_host import config
from dsh_panel import FONT, palette

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

QPlainTextEdit, QTextEdit, QLineEdit {
    background: %(card)s; color: %(text)s; border: 1px solid %(line)s;
    border-radius: %(r_sm)spx; padding: 8px;
    font-family: %(mono)s; font-size: 12px;
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
    }


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
    """给对话框套上统一样式。子控件靠 objectName 取到标题/正文/提示三种字级。"""
    widget.setStyleSheet(dialog_qss(pal or palette_now()))


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

    @classmethod
    def info(cls, parent, title: str, text: str, ok_text: str = "知道了") -> int:
        return cls(parent, title, text, "info", ok_text).exec()

    @classmethod
    def warn(cls, parent, title: str, text: str, ok_text: str = "知道了") -> int:
        return cls(parent, title, text, "warn", ok_text).exec()

    @classmethod
    def error(cls, parent, title: str, text: str, ok_text: str = "知道了") -> int:
        return cls(parent, title, text, "error", ok_text).exec()

    @classmethod
    def about(cls, parent, title: str, text: str, ok_text: str = "关闭") -> int:
        return cls(parent, title, text, "about", ok_text).exec()
