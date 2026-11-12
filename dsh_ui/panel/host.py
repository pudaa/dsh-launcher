"""面板与主窗口之间的窄接口，以及引导用的外部链接。"""
from __future__ import annotations
from .styles import *          # noqa: F401,F403

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
