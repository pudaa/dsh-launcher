"""面板与主窗口之间的窄接口，以及引导用的外部链接。"""
from __future__ import annotations
from .styles import *          # noqa: F401,F403
from .widgets import CheckResult, STATE_BAD, STATE_OK, STATE_UNKNOWN, STATE_WARN  # noqa: F401

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


class PanelHost:
    """面板与主窗口之间的**窄接口**。

    面板不直接引用 MainWindow——主窗口一改内部结构面板就跟着坏，而且面板会不自觉
    地伸手去拿更多东西。这里用回调把需要的能力逐条写清楚，面板也能脱离主窗口单测。
    """

    def __init__(self, install=None, handle=None, host_release=None,
                 theme=None, open_path=None, open_web=None, log_dir=None,
                 flag=None, set_flag=None, action=None, data_dir=None,
                 usage=None, remote_hosts=None, remote_save=None,
                 remote_connect=None, remote_disconnect=None,
                 remote_state=None):
        self._install = install
        self._handle = handle
        self._release = host_release
        self._theme = theme
        self.open_path = open_path or (lambda p: None)
        self.open_web = open_web or (lambda: None)
        self.log_dir = log_dir or (lambda: "")
        #: 桌面壳数据根（"常规 → 数据目录"用）。默认直接问 config，
        #: 这样面板能脱离主窗口单独跑自测。
        self.data_dir = data_dir or config.data_root
        #: 数据根占用统计。**很贵**（秒级全树遍历），调用方必须放到后台线程里跑。
        #: 做成可注入的，是为了让自测不必真的去数自己那块盘。
        self.usage = usage or storage.breakdown
        self._flag = flag or (lambda name: False)
        self._set_flag = set_flag or (lambda name, value: None)
        self.action = action or (lambda name: None)
        #: 远程主机（2026-11-12）。读写默认直接走 `dsh_host.remote`，
        #: 连接/断开必须注入——那是主窗口的活，面板不该自己去起 SSH 隧道。
        #: 自测时全部注入假的，于是这块 UI 能在没有 SSH 的机器上跑。
        self.remote_hosts = remote_hosts or remote.load_hosts
        self.remote_save = remote_save or remote.save_hosts
        self.remote_connect = remote_connect or (lambda host: None)
        self.remote_disconnect = remote_disconnect or (lambda: None)
        #: {"host_id":…, "stage":…, "connected":bool, "error":…}
        self.remote_state = remote_state or (lambda: {})

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
