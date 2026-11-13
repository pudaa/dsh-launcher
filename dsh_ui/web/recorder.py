"""页面观测记录：控制台消息、网络请求的事件环形缓冲。

单独成文件的理由见 `dsh_devtools.py` 的模块说明。
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import (QEvent, QObject, QRegularExpression, Qt, QTimer,
                             QUrl)
from PySide6.QtGui import (QColor, QFont, QTextCharFormat, QTextCursor,
                           QTextDocument, QSyntaxHighlighter)
from PySide6.QtWebEngineCore import (QWebEngineDownloadRequest, QWebEnginePage,
                                     QWebEngineProfile, QWebEngineSettings,
                                     QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup,
                               QFileDialog,
                               QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton,
                               QSizePolicy, QStackedWidget, QTabWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)


from dsh_ui import console as dsh_console
from dsh_host import config, procmem
from dsh_host.logutil import host_log
# 字体与调色板是面板那套设计令牌，开发者窗口沿用同一个来源，
# 免得两个窗口的等宽字体各写各的。
from dsh_ui.panel import FONT, palette


MAX_CONSOLE = 2000

MAX_NET = 2000


_TOKEN_RE = re.compile(r"(token=)[^&\s]+", re.I)


def redact(url: str) -> str:
    """把 URL 里的令牌打码。写日志和复制诊断信息时一律走它。

    令牌是这个服务的唯一身份凭据，进了日志就等于长期留在磁盘上。
    """
    return _TOKEN_RE.sub(r"\1***", str(url or ""))

def _as_json(value):
    """把 `runJavaScript` 的返回值解成 Python 结构。

    为什么要绕一道：PySide6 6.11 实测对 **array / object 的返回值一律给空串**
    （`[1,2]` 直接回 `''`），而字符串是稳的。所以 JS 侧统一 `JSON.stringify`，
    Python 侧统一走这里；已经转好的（老版本会直接给 list/dict）也兼容。
    """
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None

@dataclass
class ConsoleEntry:
    seq: int
    ts: float
    level: str
    message: str
    source: str
    line: int

@dataclass
class NetEntry:
    seq: int
    ts: float
    method: str
    rtype: str
    url: str
    size: int = -1
    duration: float = -1.0

class Recorder:
    """页面观测记录。**线程安全**：拦截器在 IO 线程写，界面在主线程读。

    用环形缓冲 + 自增序号，而不是给界面发信号：每个请求都发一次跨线程信号
    会让事件循环被高频小事件塞满，而"开发者窗口本来就按秒级刷新"完全不需要
    实时。界面按序号增量取，读多少取多少。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self.console: deque[ConsoleEntry] = deque(maxlen=MAX_CONSOLE)
        self.net: deque[NetEntry] = deque(maxlen=MAX_NET)
        #: 网络拦截只在开发者模式开启时挂上——见模块开头的取舍说明。
        self.net_enabled = False

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def add_console(self, level: str, message: str, line: int, source: str) -> None:
        text = str(message if message is not None else "")
        if len(text) > 8000:
            text = text[:8000] + " …（已截断）"
        with self._lock:
            self.console.append(ConsoleEntry(self._next(), time.time(), level,
                                             text, str(source or ""),
                                             int(line or 0)))

    def add_net(self, method: str, rtype: str, url: str) -> None:
        with self._lock:
            self.net.append(NetEntry(self._next(), time.time(), method, rtype,
                                     url))

    def fill_net(self, url: str, size: int, duration: float,
                 rtype: str | None = None) -> None:
        """把页面性能条目补到已记录的请求上；没有则新增一条。"""
        with self._lock:
            for e in reversed(self.net):
                if e.url == url:
                    e.size, e.duration = int(size), float(duration)
                    return
            self.net.append(NetEntry(self._next(), time.time(), "(perf)",
                                     rtype or "other", url, int(size),
                                     float(duration)))

    def since_console(self, seq: int) -> list[ConsoleEntry]:
        with self._lock:
            return [e for e in self.console if e.seq > seq]

    def since_net(self, seq: int) -> list[NetEntry]:
        with self._lock:
            return [e for e in self.net if e.seq > seq]

    def clear_console(self) -> None:
        with self._lock:
            self.console.clear()

    def clear_net(self) -> None:
        with self._lock:
            self.net.clear()

    def counts(self) -> tuple[int, int]:
        with self._lock:
            return len(self.console), len(self.net)

class RequestRecorder(QWebEngineUrlRequestInterceptor):
    """把页面发出的请求抄一份给 Recorder。

    注意 `interceptRequest` 每次请求都会被调用一次（含图片、字体、XHR），
    所以首行就是廉价的开关判断，关闭状态下 Python 侧只做一次属性读取。
    """

    def __init__(self, recorder: Recorder, parent=None):
        super().__init__(parent)
        self._rec = recorder

    def interceptRequest(self, info) -> None:                   # noqa: N802
        if not self._rec.net_enabled:
            return
        try:
            u = info.requestUrl()
            if u.scheme().lower() in ("data", "blob", "devtools", "chrome",
                                      "chrome-error"):
                return
            method = bytes(info.requestMethod()).decode("utf-8", "replace")
            self._rec.add_net(method, _resource_type(info), redact(u.toString()))
        except Exception:                                      # noqa: BLE001
            # 观测层出任何问题都不能影响页面本身
            pass
