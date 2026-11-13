"""面板用的基础控件：卡片、键值行、开关行、状态点、滚动提示。

共同点：**自绘 + 只认 palette 字典**，不认主题名、不读全局状态。
这样同一个控件在明暗两套下都只需要 `set_palette_colors()` 一次。
"""
from __future__ import annotations
from .styles import *          # noqa: F401,F403
from .styles import _css, _label, _tooltip_html  # noqa: F401  (import * 不带下划线名)

import os
import threading
import time
from string import Template

from shiboken6 import isValid

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

    def __init__(self, title: str, pal: dict, parent=None, help_text: str = ""):
        super().__init__(parent)
        self.setObjectName("card")
        self.pal = pal
        self._help_text = help_text
        self.setStyleSheet(card_style(pal))
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 10)
        self.body.setSpacing(0)

        # 标题行是"标题 + 可选的说明按钮"。以前标题就是个带内边距的 QLabel，
        # 现在要往右边塞一个 ⓘ，所以改成一整行的横向布局；内边距从 QLabel
        # 挪到这一行的 contentsMargins 上，视觉位置不变（改动后逐页截图核对过）。
        self.header = QWidget()
        head = QHBoxLayout(self.header)
        head.setContentsMargins(16, 14, 16, 8)
        head.setSpacing(4)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(Template(
            "color: $muted; font-family: $font; font-size: 12px; border: none;"
        ).substitute(muted=pal["muted"], font=FONT))
        head.addWidget(self.title_lbl)
        self.help_btn = None
        if help_text:
            self.help_btn = QToolButton()
            self.help_btn.setObjectName("helpBtn")
            self.help_btn.setCursor(Qt.CursorShape.WhatsThisCursor)
            self.help_btn.setIcon(icons.qicon("info", pal["muted"], 12))
            # QToolTip 会按窗口宽度换行，但长句在 880 宽的窗口里会拉得很宽；
            # 用固定的富文本宽度把提示收成一块，读起来才像"说明"而不是横幅。
            # 行距交给 dsh_theme 的浮窗统一给：两层 div 各写一份 line-height
            # 会把段落间的空行撑开（2026-11-13 出图核对）。
            self.help_btn.setToolTip(
                '<div style="width:300px;">%s</div>'
                % _tooltip_html(help_text))
            self.help_btn.setStyleSheet(
                "QToolButton { border: none; background: transparent; padding: 0; }"
                "QToolButton:hover { background: transparent; }")
            head.addWidget(self.help_btn)
        head.addStretch(1)
        self.body.addWidget(self.header)

    def set_palette_colors(self, pal: dict) -> None:
        self.pal = pal
        self.setStyleSheet(card_style(pal))
        self.title_lbl.setStyleSheet(Template(
            "color: $muted; font-family: $font; font-size: 12px; border: none;"
        ).substitute(muted=pal["muted"], font=FONT))
        if self.help_btn is not None:
            self.help_btn.setIcon(icons.qicon("info", pal["muted"], 12))

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

class RemoteHostRow(QWidget):
    """一台远程主机：名称、地址、一个动作按钮。

    状态只有三种，而且**由主窗口推过来**（`set_state`），面板不自己去探测——
    连接是主窗口的事，面板只负责显示和发指令。
    """

    def __init__(self, host, pal: dict, on_toggle, on_edit, parent=None):
        super().__init__(parent)
        self.host = host
        self.pal = pal
        self._on_toggle = on_toggle

        self.name_lbl = QLabel(host.name)
        # 固定名字列宽，让地址从同一条竖线开始——两张表拼在一起的观感，
        # 和内存卡那次"数字一左一右"是同一类问题（2026-11-12）。
        self.name_lbl.setMinimumWidth(110)
        self.addr_lbl = QLabel(host.display_address)
        self.addr_lbl.setStyleSheet(Template(
            "color: $dim; font-family: $font; font-size: 12px;"
        ).substitute(dim=pal["dim"], font=FONT))

        self.btn = QPushButton("连接")
        self.btn.setObjectName("action")
        # 固定宽度：文案在"连接 / 连接中… / 断开"之间切换，用最小宽度会让
        # 每个状态下的按钮宽度都不一样，一列按钮左边缘参差（2026-11-12 截图核对）。
        self.btn.setFixedWidth(84)
        self.btn.clicked.connect(lambda: self._on_toggle(self.host))

        edit = QToolButton()
        edit.setObjectName("winBtn")
        # 图标库里没有铅笔，用齿轮：点开的就是这台主机的设置对话框，
        # 语义反而更准。`pencil` 不存在时会画出一个空白方块（已实测）。
        edit.setIcon(icons.qicon("settings", pal["dim"], 14))
        edit.setToolTip("编辑这台主机")
        edit.clicked.connect(lambda: on_edit(self.host))

        lay = QHBoxLayout(self)
        # 左右 16 与卡片内其它行对齐（KeyValueRow 也是 16）
        lay.setContentsMargins(16, 2, 16, 2)
        lay.setSpacing(8)
        lay.addWidget(self.name_lbl)
        lay.addWidget(self.addr_lbl, 1)
        lay.addWidget(edit)
        lay.addWidget(self.btn)
        self.setFixedHeight(40)

    def set_state(self, state: dict) -> None:
        """`state` 由主窗口给：connected / connecting / None。"""
        conn = bool(state.get("connected"))
        stage = state.get("stage") or ""
        if conn:
            self.btn.setText("断开")
            self.btn.setEnabled(True)
        elif stage:
            self.btn.setText("连接中…")
            self.btn.setEnabled(False)
        else:
            self.btn.setText("连接")
            self.btn.setEnabled(True)

    def set_palette_colors(self, pal: dict) -> None:
        self.pal = pal
        self.addr_lbl.setStyleSheet(Template(
            "color: $dim; font-family: $font; font-size: 12px;"
        ).substitute(dim=pal["dim"], font=FONT))

class RemoteHostDialog(QDialog):
    """添加/编辑一台远程主机。

    只收"连哪儿、怎么起服务、断开后留不留"。**不收密码**——认证交给系统 SSH
    （密钥 / agent / `~/.ssh/config`），我们不把凭据落到自己的配置文件里。
    """

    def __init__(self, pal: dict, host=None, aliases=None, parent=None):
        super().__init__(parent)
        self.pal = pal
        self.result_host = None
        self._origin = host
        self.setWindowTitle("编辑远程主机" if host else "添加远程主机")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        def field(label: str, value: str, hint: str = "") -> QLineEdit:
            cap = QLabel(label)
            cap.setStyleSheet(Template(
                "color: $dim; font-family: $font; font-size: 12px;"
            ).substitute(dim=pal["dim"], font=FONT))
            layout.addWidget(cap)
            edit = QLineEdit(value)
            edit.setPlaceholderText(hint)
            layout.addWidget(edit)
            return edit

        self.ed_name = field("名称（可留空）", host.name if host else "",
                             "比如「家的服务器」")
        self.ed_host = field("地址", host.host if host else "",
                             "主机名、IP，或 ~/.ssh/config 里的别名")
        row = QHBoxLayout()
        row.setSpacing(8)
        col_user = QVBoxLayout()
        col_user.setSpacing(4)
        cap = QLabel("用户（可留空）")
        cap.setStyleSheet(Template("color: $dim; font-family: $font; font-size: 12px;")
                          .substitute(dim=pal["dim"], font=FONT))
        col_user.addWidget(cap)
        self.ed_user = QLineEdit(host.user if host else "")
        self.ed_user.setPlaceholderText("留空则用 ssh 配置里的")
        col_user.addWidget(self.ed_user)
        col_port = QVBoxLayout()
        col_port.setSpacing(4)
        cap2 = QLabel("端口")
        cap2.setStyleSheet(Template("color: $dim; font-family: $font; font-size: 12px;")
                           .substitute(dim=pal["dim"], font=FONT))
        col_port.addWidget(cap2)
        self.ed_port = QLineEdit(str(host.port if host else 22))
        self.ed_port.setFixedWidth(80)
        col_port.addWidget(self.ed_port)
        row.addLayout(col_user, 1)
        row.addLayout(col_port)
        layout.addLayout(row)

        self.ed_cmd = field("远端启动命令", host.launch_command if host else "dsh web",
                            "dsh web")
        hint = QLabel("会自动追加 --host 127.0.0.1 --port 0 --no-open，"
                      "所以命令必须原样接受这三个参数。")
        hint.setWordWrap(True)
        hint.setStyleSheet(Template("color: $muted; font-family: $font; font-size: 11px;")
                           .substitute(muted=pal["muted"], font=FONT))
        layout.addWidget(hint)

        cap3 = QLabel("断开后远端服务")
        cap3.setStyleSheet(Template("color: $dim; font-family: $font; font-size: 12px;")
                           .substitute(dim=pal["dim"], font=FONT))
        layout.addWidget(cap3)
        self.cb_keep = QComboBox()
        for text, value in (("每次询问", remote.KEEP_ASK),
                            ("保留（服务器上跑长任务）", remote.KEEP_KEEP),
                            ("停止（临时连一下就断开）", remote.KEEP_STOP)):
            self.cb_keep.addItem(text, value)
        cur = host.keep_service if host else remote.KEEP_ASK
        self.cb_keep.setCurrentIndex(max(0, self.cb_keep.findData(cur)))
        layout.addWidget(self.cb_keep)

        aliases = [a for a in (aliases or []) if a]
        if aliases:
            self.cb_alias = QComboBox()
            self.cb_alias.addItem("从 ~/.ssh/config 选一台…", "")
            for a in aliases:
                self.cb_alias.addItem(a, a)
            self.cb_alias.currentIndexChanged.connect(self._pick_alias)
            layout.addWidget(self.cb_alias)
        else:
            self.cb_alias = None

        self.lbl_err = QLabel("")
        self.lbl_err.setWordWrap(True)
        self.lbl_err.setStyleSheet(Template("color: $bad; font-family: $font; font-size: 12px;")
                                   .substitute(bad=pal["bad"], font=FONT))
        layout.addWidget(self.lbl_err)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("action")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setObjectName("primary")
        ok.clicked.connect(self._save)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        layout.addLayout(btns)

        # dsh_theme 反过来 import 了本模块（它要用 FONT 和 palette），
        # 所以这里只能延迟导入——模块级导入会成环。
        from dsh_ui.theme import style_dialog
        style_dialog(self, pal)

    def _pick_alias(self, index: int) -> None:
        alias = self.cb_alias.itemData(index) if self.cb_alias else ""
        if alias:
            self.ed_host.setText(alias)
            if not self.ed_name.text().strip():
                self.ed_name.setText(alias)

    def _save(self) -> None:
        host = remote.SshHost(
            id=self._origin.id if self._origin else "",
            name=self.ed_name.text().strip(),
            host=self.ed_host.text().strip(),
            user=self.ed_user.text().strip(),
            port=self._port_value(),
            launch_command=self.ed_cmd.text().strip() or "dsh web",
            keep_service=self.cb_keep.currentData())
        try:
            host.validate()
        except remote.RemoteError as e:
            self.lbl_err.setText(str(e))
            return
        self.result_host = host
        self.accept()

    def _port_value(self) -> int:
        try:
            return int(self.ed_port.text().strip() or 22)
        except ValueError:
            return 22

class CheckResult:
    """一项检测的结论。`state` 只用 STATE_* 四个值。"""

    __slots__ = ("key", "name", "value", "state", "hint")

    def __init__(self, key: str, name: str, value: str,
                 state: str = STATE_UNKNOWN, hint: str = ""):
        self.key, self.name, self.value, self.state, self.hint = \
            key, name, value, state, hint

_valid = isValid

_HINT_WATCH = {int(QEvent.Type.Resize), int(QEvent.Type.Show)}


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
        # 整数比较 + 有效性判断，理由同 `dsh_ui/web/page.py` 的 PopupRepaintGuard。
        # 这里的过滤器挂在 viewport 上，窗口销毁阶段仍可能收到事件。
        if not _valid(self) or not _valid(obj):
            return False
        try:
            if int(ev.type()) in _HINT_WATCH:
                self._sync()
        except (RuntimeError, AttributeError, TypeError, SystemError):
            return False
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
