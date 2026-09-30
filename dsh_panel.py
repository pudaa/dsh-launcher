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

为什么固定尺寸 + 无边框
-----------------------
`setFixedSize()` 之后，窗口行为只需要实现**拖动**一件：
不需要 resize、不需要最大化、不需要 Snap Layouts、不需要跨屏 DPI 迁移，
也不必为面板保留 DWM 标题栏取色那套逻辑（面板自己决定配色）。
这是用"放弃可缩放"换掉大量窗口行为代码，很划算。
**主窗口不动**，仍用原生标题栏——它承载 DSH Web UI，用户需要最大化和贴边。

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

from PySide6.QtCore import (QAbstractAnimation, Property, QEasingCurve, QPointF,
                            QPropertyAnimation, QSize, Qt, QThread, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QLabel, QPushButton, QSizePolicy, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

import dsh_icons as icons
from dsh_host import compat, config, contract, updater
from dsh_host.version import HOST_REPO, HOST_VERSION

FONT = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'

PANEL_W, PANEL_H = 880, 600
TITLEBAR_H = 40
SIDEBAR_W = 208
STATUSBAR_H = 28

#: 动效时长（毫秒）。集中一处，免得各处各写一个数。
MS_PAGE = 220          # 页面切换淡入
MS_COLOR = 180         # 状态色过渡
MS_PULSE = 1400        # 状态点脉冲一轮
MS_STAGGER = 35        # 列表逐条淡入的间隔

STATE_OK, STATE_BAD, STATE_WARN, STATE_UNKNOWN = "ok", "bad", "warn", "unknown"
STATE_ICON = {
    STATE_OK: "circle-check",
    STATE_BAD: "circle-x",
    STATE_WARN: "triangle-alert",
    STATE_UNKNOWN: "circle-dashed",
}
STATE_COLOR = {
    STATE_OK: icons.COLOR_OK,
    STATE_BAD: icons.COLOR_BAD,
    STATE_WARN: icons.COLOR_WARN,
    STATE_UNKNOWN: icons.COLOR_MUTED,
}

_STYLE = Template("""
QWidget#panel       { background: $panel; }
QWidget#titlebar    { background: $sidebar; }
QWidget#sidebar     { background: $sidebar; }
QWidget#content     { background: $panel; }
QWidget#statusbar   { background: $sidebar; }

QLabel#panelTitle   { color: $text; font-family: $font; font-size: 13px; }
QLabel#statusText   { color: $dim;  font-family: $font; font-size: 12px; }
QLabel#h1           { color: $text; font-family: $font; font-size: 20px; font-weight: 600; }
QLabel#h2           { color: $text; font-family: $font; font-size: 14px; font-weight: 600; }
QLabel#body         { color: $dim;  font-family: $font; font-size: 13px; }
QLabel#kv_k         { color: $dim;  font-family: $font; font-size: 13px; }
QLabel#kv_v         { color: $text; font-family: $font; font-size: 13px; }

QToolButton#winBtn  { border: none; background: transparent; }
QToolButton#winBtn:hover { background: #2a3038; }
QToolButton#winBtn[danger="true"]:hover { background: #d64545; }

QPushButton#action  {
    color: $text; background: #262c36; border: 1px solid $line;
    border-radius: 6px; padding: 7px 14px;
    font-family: $font; font-size: 13px;
}
QPushButton#action:hover    { background: #2d343f; }
QPushButton#action:disabled { color: $muted; background: #1e232b; }
QPushButton#primary {
    color: #ffffff; background: $accent; border: none;
    border-radius: 6px; padding: 7px 14px;
    font-family: $font; font-size: 13px;
}
QPushButton#primary:hover    { background: #5f7bff; }
QPushButton#primary:disabled { background: #3a4459; color: #9aa3b2; }
""").substitute(
    panel=icons.COLOR_PANEL, sidebar=icons.COLOR_SIDEBAR, line=icons.COLOR_LINE,
    text=icons.COLOR_TEXT, dim=icons.COLOR_TEXT_DIM, muted=icons.COLOR_MUTED,
    accent=icons.COLOR_ACCENT, font=FONT,
)

# 面板与主窗口共用的根样式表：把默认字体和背景定下来，子控件再各自覆盖。
ROOT_STYLE = _STYLE


# ------------------------------------------------------------------ 动效原语
#
# 只做四个。Qt 没有 Web 那种现成动效库，缓动曲线与时长都得自己定；
# 这四个覆盖了面板的全部需要。

def fade_in(widget: QWidget, ms: int = MS_PAGE) -> QPropertyAnimation:
    """从全透明淡入。页面切换与列表项都用它。"""
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _cleanup() -> None:
        # 结束后撤掉 effect：常驻的 QGraphicsOpacityEffect 会让整棵子树每次重绘都
        # 多走一层合成，在动画多的页面上是实打实的开销，而它只在淡入那一瞬有用。
        #
        # **只撤自己那一个**：快速连点导航会让多个 fade_in 叠在同一个控件上，
        # 先结束的那个如果无脑 setGraphicsEffect(None)，会把后开始那个动画所依赖的
        # effect 一起摘走，于是新动画再也推不动透明度——页面永远停在透明，
        # 表现就是"页面一片空白"。这个守卫不能省。
        if widget.graphicsEffect() is eff:
            widget.setGraphicsEffect(None)

    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def stagger_in(widgets: list[QWidget], ms: int = MS_PAGE,
               step: int = MS_STAGGER) -> None:
    """列表逐条淡入，每条比上一条晚 step 毫秒。"""
    for i, w in enumerate(widgets):
        QTimer.singleShot(i * step, lambda w=w: fade_in(w, ms))


def make_pulse(dot: "StatusDot") -> QPropertyAnimation:
    """状态点脉冲：相位 0→1 循环。用 InOutSine，像呼吸而不像闪烁。"""
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
    """状态点。正常/异常/警告是实心圆；"未检测"额外带一圈呼吸光晕。

    `phase` 驱动光晕，`rgb` 做颜色过渡——两个自定义属性专供动画使用。
    """

    def __init__(self, state: str = STATE_UNKNOWN, size: int = 14, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self._color = QColor(STATE_COLOR[state])
        self._pulse = None
        self._d = size
        #: 当前状态名。行控件靠它判断"值变了没有"，避免每次都重放过渡动画。
        self.state_name = state
        self.setFixedSize(size, size)

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
        target = STATE_COLOR.get(state, icons.COLOR_MUTED)
        if animated:
            animate_color(self, target)
        else:
            self.set_rgb(QColor(target))
        # 只有"未检测"才呼吸。什么都正常的时候界面还在动，会让人以为没跑完。
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
    """左侧导航项。选中时左侧亮一条指示条、文字提亮。"""

    def __init__(self, icon_name: str, text: str, parent=None):
        super().__init__("   " + text, parent)
        self.icon_name = icon_name
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(40)
        self.setIconSize(QSize(18, 18))
        self.setStyleSheet(Template("""
            QPushButton {
                text-align: left; padding-left: 15px; border: none;
                color: $dim; font-family: $font; font-size: 13px;
                border-left: 3px solid transparent;
            }
            QPushButton:hover { color: $text; background: #20252e; }
            QPushButton:checked {
                color: $text; background: #232936;
                border-left: 3px solid $accent;
            }
        """).substitute(dim=icons.COLOR_TEXT_DIM, text=icons.COLOR_TEXT,
                        accent=icons.COLOR_ACCENT, font=FONT))
        self.toggled.connect(self._refresh_icon)
        self._refresh_icon(False)

    def _refresh_icon(self, on: bool) -> None:
        self.setIcon(icons.qicon(self.icon_name,
                                 icons.COLOR_TEXT if on else icons.COLOR_TEXT_DIM, 18))


class Card(QFrame):
    """内容分组卡片。

    用 1px 边框而不是投影——投影在深色面板上只会糊成一团，
    而老大的审美偏好是"干净"，不是"有层次感"。
    """

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setStyleSheet(Template(
            "Card { background: #20252e; border: 1px solid $line;"
            " border-radius: 8px; }").substitute(line=icons.COLOR_LINE))
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 8)
        self.body.setSpacing(0)
        if title:
            lbl = QLabel(title)
            lbl.setStyleSheet(Template(
                "color: $dim; font-family: $font; font-size: 12px;"
                " padding: 12px 14px 4px 14px; border: none;"
            ).substitute(dim=icons.COLOR_TEXT_DIM, font=FONT))
            self.body.addWidget(lbl)

    def add(self, w: QWidget) -> None:
        self.body.addWidget(w)


class CheckRow(QWidget):
    """一行检测结果：状态点 + 名称 + 值。"""

    def __init__(self, key: str, name: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.dot = StatusDot(STATE_UNKNOWN, 14)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(Template(
            "color: $text; font-family: $font; font-size: 13px;"
        ).substitute(text=icons.COLOR_TEXT, font=FONT))
        self.value_lbl = QLabel("未检测")
        self.value_lbl.setStyleSheet(Template(
            "color: $dim; font-family: $font; font-size: 12px;"
        ).substitute(dim=icons.COLOR_TEXT_DIM, font=FONT))
        self.value_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.value_lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
        self.value_lbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Preferred)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 14, 0)
        row.setSpacing(10)
        row.addWidget(self.dot)
        row.addWidget(name_lbl)
        row.addWidget(self.value_lbl, 1)
        self.setFixedHeight(34)

    def apply(self, res: "CheckResult") -> bool:
        """返回状态是否发生变化（用来自测断言）。"""
        changed = res.state != self.dot.state_name
        self.dot.state_name = res.state
        self.value_lbl.setText(res.value or "—")
        self.value_lbl.setToolTip(res.hint or "")
        self.dot.set_state(res.state)
        return changed


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


def local_checks(host: "PanelHost") -> list[CheckResult]:
    """本机可立即得出的检测项——**不联网**，所以可以自动跑。

    每一项都对应一个用户真会遇到的故障；措辞面向使用者。
    """
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
        parts = [f"{t}={tags.get(t)}" for t in ("latest", "next", "alpha") if tags.get(t)]
        cur = self.install.version if self.install else ""
        # 通道本身查得到就算正常。装了较旧版本不判"异常"——那是用户的选择，
        # 该由「检查更新」去提示，不该在体检页把用户状态标红。
        highest = cur
        for t in ("latest", "next", "alpha"):
            v = tags.get(t)
            if v and (not highest or compat.compare(v, highest) > 0):
                highest = v
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
                 open_path=None, open_web=None, log_dir=None):
        self._install = install
        self._handle = handle
        self._release = host_release
        self.open_path = open_path or (lambda p: None)
        self.open_web = open_web or (lambda: None)
        self.log_dir = log_dir or (lambda: "")

    def install(self):
        return self._install() if callable(self._install) else self._install

    def handle(self):
        return self._handle() if callable(self._handle) else self._handle

    def host_release(self):
        return self._release() if callable(self._release) else self._release


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

def _label(text: str, kind: str = "body", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(kind)
    lbl.setWordWrap(wrap)
    return lbl


class KeyValueRow(QWidget):
    """键值一行：左键右值，值可选中复制。"""

    def __init__(self, key: str, value: str = "—", parent=None):
        super().__init__(parent)
        k = QLabel(key)
        k.setObjectName("kv_k")
        k.setFixedWidth(120)
        self.v = QLabel(value)
        self.v.setObjectName("kv_v")
        self.v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.v.setWordWrap(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 4, 14, 4)
        lay.setSpacing(8)
        lay.addWidget(k)
        lay.addWidget(self.v, 1)
        self.setMinimumHeight(30)

    def set(self, value: str) -> None:
        self.v.setText(value or "—")


class BasePage(QWidget):
    """页面基类。`on_show()` 在每次被切到时调用（含懒创建后的第一次）。"""

    def __init__(self, host: PanelHost, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(12)
        self.layout_ = lay
        self.host = host
        self._built = False

    def on_show(self) -> None:
        if not self._built:
            self._built = True
            self.build()
            stagger_in(self._top_widgets())

    def build(self) -> None:                                       # pragma: no cover
        pass

    def _top_widgets(self) -> list[QWidget]:
        return [self.layout_.itemAt(i).widget() for i in range(self.layout_.count())
                if self.layout_.itemAt(i).widget()]

    def shutdown(self, timeout_ms: int = 8000) -> bool:
        """收尾钩子。有后台线程的页面覆盖它。返回是否收尾干净。"""
        return True


class OverviewPage(BasePage):
    """概览：一眼看清当前状态与常用动作。"""

    def build(self) -> None:
        self.layout_.addWidget(_label("概览", "h1"))

        inst = self.host.install()
        self.kv_shell = KeyValueRow("桌面壳版本", HOST_VERSION)
        self.kv_dsh = KeyValueRow("DSH 版本", inst.version if inst else "未检测")
        self.kv_src = KeyValueRow("DSH 来源", inst.source if inst else "—")
        self.kv_svc = KeyValueRow("后台服务", "—")

        c1 = Card("当前状态")
        for w in (self.kv_shell, self.kv_dsh, self.kv_src, self.kv_svc):
            c1.add(w)
        self.layout_.addWidget(c1)

        row = QHBoxLayout()
        row.setSpacing(8)
        btn_web = QPushButton("打开 DSH 界面")
        btn_web.setObjectName("primary")
        btn_web.clicked.connect(lambda: self.host.open_web())
        btn_log = QPushButton("打开日志目录")
        btn_log.setObjectName("action")
        btn_log.clicked.connect(lambda: self.host.open_path(self.host.log_dir()))
        row.addWidget(btn_web)
        row.addWidget(btn_log)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        self.layout_.addWidget(holder)
        self.layout_.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        inst = self.host.install()
        h = self.host.handle()
        self.kv_dsh.set(inst.version if inst else "未检测")
        self.kv_src.set(inst.source if inst else "—")
        if h and h.port:
            alive = contract.tcp_open(h.host or "127.0.0.1", h.port, timeout=1.0)
            self.kv_svc.set(f"运行中 · 端口 {h.port}" if alive else f"已停止（端口 {h.port}）")
        else:
            self.kv_svc.set("未启动")

    def on_show(self) -> None:
        if not self._built:
            super().on_show()
        else:
            self.refresh()


class DeployPage(BasePage):
    """本机部署：**引导**，不代劳。

    为什么从"帮你装"改成"告诉你怎么装"
    ----------------------------------
    替用户装 Node / 装 DSH，意味着我们要跟着上游的下载地址、包名、安装参数一起改。
    上游一动我们就得重新打包发版。改成引导后，网页/命令放在适配表里（可外部覆盖），
    我们只负责**判断每一步成没成**——那部分是我们自己的契约，不会因为上游换网址而失效。
    """

    def build(self) -> None:
        self.layout_.addWidget(_label("本机部署", "h1"))
        self.layout_.addWidget(_label(
            "这一步由你来做，我们只负责告诉你做没做成。"
            "每一步做完回到「状态检测」点一次重新检测，就能看到结果。", "body"))

        links = guide_links()

        c1 = Card("第一步 · 安装 Node.js 运行环境")
        c1.add(KeyValueRow("状态", "见「状态检测」页的 Node.js 一行"))
        c1.add(self._link_row("下载页", links["node_download"]))
        c1.add(self._copy_row("或用命令行安装", links["node_winget"]))
        self.layout_.addWidget(c1)

        c2 = Card("第二步 · 安装 DeepSeek Harness")
        c2.add(self._copy_row("命令行安装", f"npm install -g {updater.PKG_NAME}"))
        c2.add(self._link_row("项目主页", links["dsh_site"]))
        c2.add(self._link_row("使用文档", links["dsh_docs"]))
        self.layout_.addWidget(c2)

        self.layout_.addStretch(1)

    def _link_row(self, title: str, url: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 4, 14, 4)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("kv_k")
        t.setFixedWidth(120)
        v = QLabel(url or "（未配置）")
        v.setObjectName("kv_v")
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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
        lay.setContentsMargins(14, 4, 14, 4)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("kv_k")
        t.setFixedWidth(120)
        v = QLabel(cmd or "（未配置）")
        v.setObjectName("kv_v")
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        btn = QPushButton("复制")
        btn.setObjectName("action")
        btn.setIcon(icons.qicon("copy", icons.COLOR_TEXT_DIM, 16))
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
        self.layout_.addWidget(_label("远程连接还在规划中，这里先说明我们打算怎么做。", "body"))

        c1 = Card("规划中的形态")
        for line in (
            "· 把另一台设备上的 DSH 呈现在本机窗口里，多个设备各开一个窗口",
            "· 目标设备可以还没启动 DSH——由连接过程带着把它拉起来",
            "· 连接方式依托你已有的通道（SSH 隧道或组网），不额外占用端口",
        ):
            c1.add(_label(line, "body"))
        self.layout_.addWidget(c1)

        c2 = Card("一条不会变的底线")
        c2.add(_label(
            "我们**不会**让 DSH 监听外网，也不会改它的绑定设置。"
            "DSH 的接口上有执行命令的能力，官方正是因为这个才把监听范围"
            "锁在本机（它自己的启动器里写着：绑定到全网段等于把远程代码执行"
            "暴露到网络上）。绕过这条限制，等于替官方做它明确拒绝的安全决定，"
            "而且上游一加校验我们就得崩。", "body"))
        c2.add(_label(
            "所以传输由我们这一侧负责：DSH 仍然只听本机，"
            "由隧道把它的本机端口借过来——对外只需要一条通道。", "body"))
        self.layout_.addWidget(c2)
        self.layout_.addStretch(1)


class StatusPage(BasePage):
    """状态检测：只报告，不修复。"""

    def build(self) -> None:
        head = QHBoxLayout()
        head.addWidget(_label("状态检测", "h1"))
        head.addStretch(1)
        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.setObjectName("primary")
        self.btn_recheck.setIcon(icons.qicon("refresh-cw", "#ffffff", 16))
        self.btn_recheck.clicked.connect(self.run_checks)
        self.btn_channel = QPushButton("查询官方通道")
        self.btn_channel.setObjectName("action")
        self.btn_channel.clicked.connect(self.run_channel)
        head.addWidget(self.btn_channel)
        head.addWidget(self.btn_recheck)
        head_w = QWidget()
        head_w.setLayout(head)
        self.layout_.addWidget(head_w)

        self.cards: dict[str, Card] = {}
        self.rows: dict[str, CheckRow] = {}
        # 分组：环境 / 数据 / 服务与版本
        groups = (
            ("运行环境", ("node", "npm", "dsh")),
            ("数据", ("home", "home_w")),
            ("服务与版本", ("svc", "channel")),
        )
        labels = dict(CHECK_ROWS)
        for gname, keys in groups:
            card = Card(gname)
            for k in keys:
                row = CheckRow(k, labels[k])
                self.rows[k] = row
                card.add(row)
            self.cards[gname] = card
            self.layout_.addWidget(card)
        self.layout_.addStretch(1)

        self._worker = None
        self._chan_worker = None

    def on_show(self) -> None:
        if not self._built:
            super().on_show()
            self.run_checks()
        else:
            self.run_checks()

    def run_checks(self) -> None:
        """跑一遍本机检测（后台）。"""
        if self._worker is not None and self._worker.isRunning():
            return
        for k in ("node", "npm", "dsh", "home", "home_w", "svc"):
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
            row.dot.state_name = res.state
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
        self.rows["channel"].dot.state_name = res.state
        self.rows["channel"].apply(res)
        self.btn_channel.setEnabled(True)

    def shutdown(self, timeout_ms: int = 8000) -> bool:
        """等后台线程跑完。

        为什么必须显式等：QThread 在解释器退出时还活着会让 Qt 直接 abort，
        而 abort 会**丢掉未刷出的 stdout 缓冲**——表现是"脚本没有任何输出、
        退出码非零"，排查时极具误导性（真正的原因在 C++ 层）。CI 也会因此判失败。

        注意 `QThread.quit()` 只退事件循环，**打断不了 run() 里的同步代码**，
        所以这里用 `wait()` 等它自己跑完。
        """
        ok = True
        for attr in ("_worker", "_chan_worker"):
            w = getattr(self, attr, None)
            if w is not None and w.isRunning() and not w.wait(timeout_ms):
                ok = False
        return ok


class AboutPage(BasePage):
    """关于。"""

    def build(self) -> None:
        self.layout_.addWidget(_label("关于", "h1"))
        inst = self.host.install()
        rel = self.host.host_release()

        c = Card("版本")
        c.add(KeyValueRow("桌面壳", HOST_VERSION))
        c.add(KeyValueRow("DSH", inst.version if inst else "未检测"))
        c.add(KeyValueRow("可更新到", rel or "（未查询）"))
        self.layout_.addWidget(c)

        c2 = Card("来源与许可")
        c2.add(KeyValueRow("桌面壳仓库", HOST_REPO))
        c2.add(KeyValueRow("DSH 依赖", updater.PKG_NAME))
        c2.add(_label(
            "界面图标来自 Lucide（isc 许可），已内联为源码，运行时不读外部文件。",
            "body"))
        self.layout_.addWidget(c2)

        c3 = Card("目录")
        c3.add(KeyValueRow("数据根", config.data_root()))
        c3.add(KeyValueRow("DSH_HOME", contract.resolve_home() or "（由 DSH 自行决定）"))
        self.layout_.addWidget(c3)
        self.layout_.addStretch(1)


# ------------------------------------------------------------------ 面板窗口

class TitleBar(QWidget):
    """自绘标题栏：标题 + 最小化 + 关闭，整条可拖动。

    无边框窗口只需要实现拖动这一件事（尺寸固定，没有 resize/最大化/Snap 的问题）。
    `startSystemMove()` 走 Win32 的 SC_MOVE，保留系统行为——**不要自己算鼠标位移**，
    否则多显示器、DPI 变化、贴边之类都会出问题。
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("titlebar")
        self.setFixedHeight(TITLEBAR_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 0, 0)
        lay.setSpacing(0)
        lbl = QLabel(title)
        lbl.setObjectName("panelTitle")
        lay.addWidget(lbl)
        lay.addStretch(1)

        self.btn_min = _win_button("minus", "最小化")
        self.btn_close = _win_button("x", "关闭", danger=True)
        lay.addWidget(self.btn_min)
        lay.addWidget(self.btn_close)

    def mousePressEvent(self, e) -> None:                          # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            win = self.window()
            handle = win.windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(e)


def _win_button(icon_name: str, tip: str, danger: bool = False) -> QToolButton:
    """标题栏上的小按钮。

    两个按钮**共用 objectName `winBtn`**，只靠动态属性区分（关闭键悬停变红）。
    之前给关闭键单独起名 `winBtnClose`，结果样式表里只有它的 hover 规则、没有基础
    规则，它就被系统默认样式接管、渲染成一块浅色底——**只在悬停时才好看，平时是白块**。
    共用 objectName 能让基础规则必然命中。
    """
    b = QToolButton()
    b.setObjectName("winBtn")
    b.setProperty("danger", danger)
    b.setIcon(icons.qicon(icon_name, icons.COLOR_TEXT_DIM, 16))
    b.setIconSize(QSize(16, 16))
    b.setFixedSize(44, TITLEBAR_H)
    b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class ControlPanel(QWidget):
    """控制面板主窗口：无边框 + 固定尺寸 880×600。"""

    #: 页签定义：(图标, 标题, 页面类)
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
        self.setStyleSheet(_STYLE)
        self.setWindowTitle("DSH Launcher 控制面板")
        self.setFixedSize(PANEL_W, PANEL_H)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.titlebar = TitleBar("DSH Launcher 控制面板", self)
        self.titlebar.btn_min.clicked.connect(self.showMinimized)
        self.titlebar.btn_close.clicked.connect(self.close)
        root.addWidget(self.titlebar)

        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)

        # 侧栏
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(SIDEBAR_W)
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 10, 0, 0)
        side_lay.setSpacing(2)
        self.navs: list[NavItem] = []
        for i, (icon_name, title, _cls) in enumerate(self.PAGES):
            item = NavItem(icon_name, title)
            item.clicked.connect(lambda _c=False, i=i: self.goto(i))
            self.navs.append(item)
            side_lay.addWidget(item)
        side_lay.addStretch(1)
        mid.addWidget(side)

        # 内容区（页面懒创建）
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

        # 底部状态条（只报告）
        self.statusbar = QWidget()
        self.statusbar.setObjectName("statusbar")
        self.statusbar.setFixedHeight(STATUSBAR_H)
        sb = QHBoxLayout(self.statusbar)
        sb.setContentsMargins(14, 0, 14, 0)
        self.status_dot = StatusDot(STATE_UNKNOWN, 12)
        self.status_lbl = QLabel("就绪")
        self.status_lbl.setObjectName("statusText")
        sb.addWidget(self.status_dot)
        sb.addWidget(self.status_lbl)
        sb.addStretch(1)
        root.addWidget(self.statusbar)

        self.current = -1
        self.goto(0)
        self.refresh_statusbar()

    # ---- 页面切换 ----
    def _page(self, index: int) -> BasePage:
        page = self.pages.get(index)
        if page is None:
            cls = self._pending[index]
            page = cls(self.host)
            # 用真页面替换占位控件
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
        # 动效原语之一：页面切换淡入
        fade_in(page, MS_PAGE)
        self.refresh_statusbar()

    # ---- 状态条 ----
    def refresh_statusbar(self) -> None:
        inst = self.host.install()
        h = self.host.handle()
        bits = []
        if inst:
            bits.append(f"DSH {inst.version}")
        else:
            bits.append("DSH 未检测")
        if h and h.port and contract.tcp_open(h.host or "127.0.0.1", h.port, timeout=1.0):
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

    只验不依赖真实 DSH 的部分：面板能构造、五个页签齐全、图标不缺失、
    窗口尺寸与无边框标志正确、检测行覆盖了预期的键。
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

    # 图标集完整性
    missing = icons.missing([i for i, _t, _c in ControlPanel.PAGES] + list(STATE_ICON.values()))
    check("页面与状态图标齐全", not missing, "缺：" + "、".join(missing) if missing else "")

    # 图标真的渲染出来了（currentColor 替换是否生效）
    bad = []
    for name in icons.SVG:
        r = icons.ink_ratio(name, 48, "#000000")
        if not (0.02 < r < 0.60):
            bad.append("%s=%.3f" % (name, r))
    check("全部图标渲染比例正常", not bad, "、".join(bad[:4]))

    host = PanelHost(install=None, handle=None)
    panel = ControlPanel(host)

    check("固定尺寸为 880x600",
          (panel.width(), panel.height()) == (PANEL_W, PANEL_H),
          "%sx%s" % (panel.width(), panel.height()))
    check("无边框", bool(panel.windowFlags() & Qt.WindowType.FramelessWindowHint))
    check("没有最大/最小尺寸的余地（isFixedSize）",
          panel.minimumSize() == panel.maximumSize() == panel.size())
    check("页签数量与定义一致", len(panel.navs) == len(ControlPanel.PAGES),
          "%d 个" % len(panel.navs))
    titles = [n.text().strip() for n in panel.navs]
    check("页签名正确", titles == ["概览", "本机部署", "连接", "状态检测", "关于"],
          "、".join(titles))

    # 逐个切页：应能懒创建并跑通 on_show
    try:
        for i in range(len(ControlPanel.PAGES)):
            panel.goto(i)
        check("五个页面都能切换并完成构建", len(panel.pages) == len(ControlPanel.PAGES),
              "已构建 %d 个" % len(panel.pages))
    except Exception as e:                                       # noqa: BLE001
        check("五个页面都能切换并完成构建", False, "%s: %s" % (type(e).__name__, e))

    # 检测行覆盖预期键
    sp = panel.pages.get(3)
    if sp is not None:
        keys = tuple(sp.rows.keys())
        check("状态检测页覆盖全部预期项",
              set(keys) == {k for k, _n in CHECK_ROWS}, "、".join(keys))
    else:
        check("状态检测页覆盖全部预期项", False, "状态页未构建")

    # 界面里不许出现 Emoji
    import re
    emoji = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
    hits = []
    for nav in panel.navs:
        if emoji.search(nav.text()):
            hits.append(nav.text())
    for i in range(len(ControlPanel.PAGES)):
        page = panel.pages.get(i)
        if page is None:
            continue
        for lbl in page.findChildren(QLabel):
            if emoji.search(lbl.text()):
                hits.append(lbl.text()[:30])
    check("面板不含 Emoji", not hits, "、".join(hits[:3]))

    # 标题栏按钮的底色必须是深色
    # ----------------------------
    # 这条是**像素级**回归：曾经关闭键因为只有 hover 规则、没有基础规则，
    # 被系统默认样式接管渲染成 #f5f5f5 的白块——断言看不出来，只有采样才看得见。
    panel.resize(PANEL_W, PANEL_H)
    panel.show()
    app.processEvents()
    shot = panel.grab().toImage()
    bad_light = []
    for btn in (panel.titlebar.btn_min, panel.titlebar.btn_close):
        g = btn.geometry()
        # 取按钮中心偏左一点：避开图标本身，只看底色
        x = max(0, g.x() + 6)
        y = g.y() + g.height() // 2
        if x < shot.width() and y < shot.height():
            c = shot.pixelColor(x, y)
            if c.lightness() > 120:
                bad_light.append("%s=%s" % (btn.toolTip(), c.name()))
    check("标题栏按钮底色是深色（不是系统默认浅色）", not bad_light,
          "、".join(bad_light))

    # 快速连点导航后，当前页必须真的可见
    # ------------------------------------
    # 页面切换是"淡入 220ms"。连点会产生多个叠在同一个页面上的淡入动画，
    # 先结束的那个若盲目撤掉自己的 effect，会把后开始那个动画的 effect 一起摘走，
    # 透明度再也推不动 —— 页面**永远一片空白**。断言看不出来，只有等动画跑完
    # 再看图形效果是否已撤掉才能发现。
    for i in (0, 1, 2, 3, 4, 0, 1):
        panel.goto(i)
    deadline = 2000
    step = 50
    waited = 0
    cur = panel.pages.get(panel.current)
    while cur is not None and cur.graphicsEffect() is not None and waited < deadline:
        loop_events(app, step)
        waited += step
    eff = cur.graphicsEffect() if cur is not None else None
    check("快速连点导航后当前页恢复可见（淡入动画能收尾）",
          cur is not None and eff is None,
          "等待 %dms，effect=%s" % (waited, type(eff).__name__ if eff else "已撤掉"))

    # 后台线程必须能干净收尾
    # ----------------------
    # 状态检测页会起 QThread 跑检测（遍历 DSH_HOME 可能上万文件，不能放界面线程）。
    # 线程在解释器退出时还活着会让 Qt abort，而 abort 会丢掉未刷出的 stdout 缓冲——
    # 表现是"脚本零输出、退出码 127"，CI 因此判失败，且现象极具误导性。
    check("后台检测线程能干净收尾", panel.shutdown(15000))

    panel.close()
    panel.deleteLater()
    return failures
