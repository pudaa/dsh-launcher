"""%s

MainWindow 的一个分支（2026-11-13 从 2000 行的单类里拆出）。
只放方法、不放状态：所有属性都在 `MainWindow.__init__` 里初始化。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

from PySide6.QtCore import (QCoreApplication, QEvent, QPoint, QRect, QStandardPaths,
                            Qt, QThread, QTimer, QUrl)
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QIcon, QImage,
                           QKeySequence, QPixmap, QShortcut)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QMainWindow, QPlainTextEdit, QProgressBar,
                               QPushButton, QStackedLayout, QStackedWidget,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from dsh_host import (config, contract, dwm, lifecycle, procmem, provision, remote,
                      selfupdate, storage, updater)
from dsh_host.contract import DshError, ServiceHandle
from dsh_host.logutil import host_log
from dsh_host.version import HOST_NAME, HOST_TAGLINE, HOST_VERSION
from dsh_ui import console as dsh_console
from dsh_ui import devtools as dsh_devtools
from dsh_ui import panel as dsh_panel
from dsh_ui import theme as dsh_theme
from dsh_ui.dialogs import (DataTaskDialog, HostUpdateDialog, OnboardingDialog,
                            ReinstallDialog, UpdateDialog)
from dsh_ui.prelude import *        # noqa: F401,F403
from dsh_ui.workers import (CheckWorker, DataTaskWorker, DiagnoseWorker, HostCheckWorker,
                            HostUpdateWorker, NotesWorker, ProvisionWorker,
                            RemoteConnectWorker, ServiceSwapWorker, StartupWorker,
                            UpdateWorker, VersionReverifyWorker)


class WindowFrameMixin:

    _WORKERS = ("starter", "_reverify", "_checker", "_host_checker",
                "_swap_worker", "_data_worker", "_remote_worker")

    def _build_loading_page(self):
        page = QWidget()
        page.setObjectName("loadingPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        bar.setFixedHeight(3)
        # 必须留引用：加载页在 StackAll 里**一直可见**，indeterminate 进度条的
        # 动画定时器也会一直跑。2026-10-24 实测这不只是白烧 CPU——它还是
        # 「打开原生 <select> 弹层后 Web 视图停止呈现」的触发条件（把加载页
        # 换成静止版就不复现）。所以 Web 视图露出来之后必须把它停下来。
        self.progress = bar
        self.tip = QLabel("正在启动 DSH 服务…")
        self.tip.setObjectName("loadingTip")
        self.tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tip.setWordWrap(True)
        lay.addWidget(bar)
        lay.addStretch(1)
        lay.addWidget(self.tip)
        lay.addStretch(1)
        # 深色界面下不要让启动瞬间先闪一片系统白
        dsh_theme.style_loading_page(page)
        self.loading_page = page
        self.stack.addWidget(page)
    def _set_tip(self, text: str):
        self.tip.setText(text)
    def _loading_busy(self, busy: bool) -> None:
        """加载页动画的开关。

        StackAll 下加载页始终处于 show 状态，所以 indeterminate 进度条
        （range 0..0）的动画**会一直跑**，即使 Web 视图早就盖在它上面。
        两个后果：
          1. 白烧一个定时器（项目纪律：没在用就不占资源）；
          2. 2026-10-24 实测：这个持续重绘是「打开原生弹层后 Web 视图停止
             呈现」的触发条件——把加载页换成静止版就复现不出来。
        所以视图露出来后调 busy=False（定成静止的 0/1 空条），需要再显示
        加载页时（换来源/重启服务/重新准备环境）调 busy=True 恢复动画。
        """
        bar = getattr(self, "progress", None)
        if bar is None:
            return
        try:
            if busy:
                bar.setRange(0, 0)
            else:
                bar.setRange(0, 1)
                bar.setValue(0)
        except RuntimeError:
            pass
    def _show_main(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
    def closeEvent(self, event):
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self.tray.showMessage("DSH", "应用已最小化到托盘，后台服务仍在运行",
                                  QSystemTrayIcon.MessageIcon.Information, 5000)
            self._tray_hint_shown = True
    def _quit(self):
        """退出应用。

        退出时**连带停止后台服务**——这是重构后的默认行为。

        理由：「停止后台服务」与「退出」原本是两个菜单项，语义不同
        （一个停服务留窗口、一个关窗口留服务），但用户点它们时的意图
        几乎都是"我要结束这一切"。留两个入口只会让人犹豫点哪个。
        现在只保留「退出」，并且它做的是"把这件事完整结束掉"。

        代价：想在关掉窗口后继续跑 DSH 后台任务的人失去了入口。
        对桌面壳的目标用户（用图形界面干活的人）这个代价可以接受——
        真需要常驻服务的人本来就会用命令行。

        远程连接进行中**拒绝退出**：`RemoteConnectWorker` 里的 SSH 调用是阻塞的
        （远端冷启动最长 90 秒），把它丢下就退出，Qt 会因为线程还活着而 abort，
        表现是"点了退出，程序直接消失，日志一段都没刷出来"。连接本身有超时上限，
        等它结束只是一两分钟的事。
        """
        worker = self._remote_worker
        if worker is not None and worker.isRunning():
            self._info("正在建立远程连接，请等它结束再退出。\n\n"
                       "（远端冷启动最多需要一两分钟，界面上有进度。）")
            return
        host_log("用户退出，连带停止后台服务")
        # 先收尾面板的后台线程。线程在解释器退出时还活着会让 Qt 直接 abort，
        # 而 abort 会丢掉未刷出的日志缓冲——那会让"退出"变成一件莫名其妙的事。
        panel = getattr(self, "_panel", None)
        if panel is not None:
            try:
                if not panel.shutdown():
                    host_log("警告：面板的后台检测线程未在超时内结束")
            except Exception as e:                               # noqa: BLE001
                host_log("退出时收尾面板失败：%s: %s" % (type(e).__name__, e))
        # 主窗口自己的后台任务同理，而且更危险：启动线程半路被丢下还会继续把
        # 服务拉起来，于是退出后留下一个孤儿 node.exe（2026-10-20 体检补上）。
        left = self._join_workers()
        if left:
            host_log("警告：后台任务未在超时内结束：%s" % "、".join(left))
        try:
            n = self._stop_service()
            host_log("已停止后台服务（%d 个进程）" % n)
        except Exception as e:                                   # noqa: BLE001
            host_log("退出时停止服务失败：%s: %s" % (type(e).__name__, e))
        self.tray.hide()
        QApplication.quit()
    def _join_workers(self, timeout_ms: int = 4000) -> list:
        """等主窗口的后台线程收尾，返回超时没等到的属性名。

        `wait()` 只等、不打断 `run()` 里的同步代码——这正是我们要的：
        等服务探测/启动走到一个安全点，再把它停掉。总预算按线程数叠加，
        实际退出路径上通常一个都不需要等。
        """
        left = []
        for name in self._WORKERS:
            w = getattr(self, name, None)
            if w is not None and w.isRunning() and not w.wait(timeout_ms):
                left.append(name)
        return left
    def _launch_startup(self):
        # 重入守卫：切换来源、面板「自动准备运行环境」都会走到这里。没有守卫时
        # 两个 StartupWorker 会并发 launch/wait_ready —— 多一个没人管的服务，
        # 或者 attach 到同一个服务后 `_on_ready` 再跑一遍。`_swap_service` 和
        # `_check_update` 都有同类守卫，只有启动这条没有（2026-10-20 体检补上）。
        if self.starter is not None and self.starter.isRunning():
            return
        if self.starter is not None:
            self.starter.deleteLater()
        self.starter = StartupWorker(requested_service_port(),
                                     bool(config.get("port_auto_fallback")),
                                     config.service_log())
        self.starter.progress.connect(self._set_tip)
        self.starter.ready.connect(self._on_ready)
        self.starter.failed.connect(self._on_failed)
        self.starter.needs_setup.connect(self._on_needs_setup)
        self.starter.start()
    def _info(self, text: str):
        """应用内提示。

        手动操作的结果**必须**落在应用内。托盘气泡有两个致命问题：
        用户视线在鼠标位置而气泡在屏幕角落，容易错过；系统「通知」设置
        还可能直接把它拦掉。两条叠加，用户看到的就是"点了没反应"。
        """
        self._show_main()
        dsh_theme.MessageDialog.info(self, "DSH", text)
    def _warn(self, text: str):
        self._show_main()
        dsh_theme.MessageDialog.warn(self, "DSH", text)
