"""面板自己的后台线程：环境检测、通道查询、占用统计、内存采样。

为什么不在界面线程：占用统计是秒级全树遍历，内存采样一次 16 ms——
挂在界面线程上会让滚动发黏（2026-11-12 之前试过）。
"""
from __future__ import annotations
from .styles import *          # noqa: F401,F403
from .widgets import CheckResult           # noqa: F401
from .host import local_checks             # noqa: F401

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

class UsageWorker(QThread):
    """在后台数数据目录有多大。

    为什么必须后台（2026-10-20 用户反馈"每次切进常规 tab 都明显卡顿"）
    -----------------------------------------------------------------
    `storage.breakdown()` 要遍历整个数据根。私有部署下那是 **6.6 万个文件 /
    727 MB**：旧实现（分组各走一遍）实测 **10.2 秒**，改成一次遍历后 **5.1 秒**
    （Windows Defender 逐文件扫描占大头）。以前它在 `GeneralPage.on_show()` 里
    **同步**跑，于是每次切到「常规」页，界面就冻住十几秒——连页签高亮都要等它
    算完才画出来。

    所以挪到线程里：页面立刻显示（"统计中…"），算完再回填。
    顺带加了 30 秒缓存（见 `GeneralPage._refresh_usage`），来回切页不重复付账。
    """
    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as e:                                   # noqa: BLE001
            # 统计失败不该把整页搞崩：只让"占用"这一行显示失败原因。
            self.done.emit({"error": "%s: %s" % (type(e).__name__, e)})

class MemWorker(QThread):
    """常驻的整树内存采样线程。

    为什么不放界面线程（2026-11-12）
    -------------------------------
    `procmem.snapshot()` 要遍历全机进程表再逐个取内存，**实测 16 ms**——
    正好是一帧的时间。面板里每 2 秒付一次这种代价，滚动或淡入动画撞上就顿一下；
    而这块数据的用途是"盯着看有没有涨"，晚 2 秒到毫无影响。

    采样间隔用 `Event.wait()` 而不是 `time.sleep()`：停止时能立刻醒来，
    不然退出面板要等最多一个间隔（Qt 里线程晚退就是 abort，见 shutdown 的注释）。
    """
    sample = Signal(object)

    def __init__(self, interval_ms: int = MEM_SAMPLE_MS, parent=None):
        super().__init__(parent)
        self._interval = max(500, int(interval_ms)) / 1000.0
        self._halt = threading.Event()
        self._halt.set()          # 未 start 前视为"已停"

    def run(self):
        while not self._halt.is_set():
            try:
                snap = procmem.snapshot()
            except Exception:                                    # noqa: BLE001
                snap = None      # 采不到就当这一轮没有，不要连累界面
            if self._halt.is_set():
                break
            if snap is not None:
                self.sample.emit(snap)
            self._halt.wait(self._interval)

    def start_sampling(self) -> None:
        if self.isRunning():
            return
        self._halt.clear()
        self.start()

    def stop_sampling(self, timeout_ms: int = 3000) -> bool:
        """停并等它真的退出。返回是否在超时内结束。"""
        self._halt.set()
        return self.wait(timeout_ms)
