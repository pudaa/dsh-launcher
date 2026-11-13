"""面板窗口本体：自绘标题栏 + 侧栏导航 + 页面堆栈 + 底部状态条。"""
from __future__ import annotations
from .styles import *          # noqa: F401,F403
from .styles import _css, _label  # noqa: F401  (import * 不带下划线名)
from .widgets import *         # noqa: F401,F403
from .host import *            # noqa: F401,F403
from .pages import *           # noqa: F401,F403
from .workers import *         # noqa: F401,F403

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

from dsh_ui import icons
from dsh_host import (compat, config, contract, dwm, netenv, procmem, provision,
                      remote, storage, updater)
from dsh_host.logutil import host_log
from dsh_host.version import HOST_NAME, HOST_REPO, HOST_VERSION


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
        ("plug", "运行方式", RunModePage),
        ("package", "本机部署", DeployPage),
        ("settings", "常规", GeneralPage),
        ("activity", "状态检测", StatusPage),
        ("info", "关于", AboutPage),
    )

    def __init__(self, host: PanelHost, parent=None):
        super().__init__(parent, Qt.WindowType.Window
                         | Qt.WindowType.FramelessWindowHint)
        self.host = host
        self.setObjectName("panel")
        self.setWindowTitle(f"{HOST_NAME}控制面板")
        self.setFixedSize(PANEL_W, PANEL_H)
        # 半透明底是自绘圆角的前提：四个角要能透出桌面
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._theme = host.theme()
        self.pal = palette(self._theme)
        self.setStyleSheet(build_style(self.pal))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.titlebar = TitleBar(f"{HOST_NAME}控制面板", self.pal, self)
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
        if index != self.current:
            prev = self.pages.get(self.current)
            if prev is not None:
                # 被切走的页面要停掉自己的周期任务（目前只有概览页的内存采样）
                prev.on_hide()
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
    def refresh_remote(self) -> None:
        """主窗口推送远程状态时调用。转发给「运行方式」页。"""
        page = self.pages.get(1)
        if page is not None and hasattr(page, "refresh_remote"):
            page.refresh_remote()

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

    # ---- 显示 / 隐藏 ----
    #
    # 概览页的内存采样是**持续**花钱的（每 2 秒遍历一次全机进程表），所以
    # 它的生命周期必须跟"面板有没有开着"绑定：收起面板就停，
    # 重新打开再起。否则关掉面板后线程还在后台数进程，纯属白烧。

    def showEvent(self, e) -> None:                                # noqa: N802
        super().showEvent(e)
        page = self.pages.get(self.current)
        if page is not None:
            page.on_show()          # 各页的 on_show 都是幂等的

    def hideEvent(self, e) -> None:                                # noqa: N802
        page = self.pages.get(self.current)
        if page is not None:
            page.on_hide()
        super().hideEvent(e)
