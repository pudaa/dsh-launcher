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


class TrayMixin:

    def _build_tray(self):
        """托盘菜单。

        分组原则（2026-09-20 重构）
        --------------------------
        按"用户想做什么"分组，而不是按"我们的模块怎么划分"分组：

          1. 显示主窗口
          2. 环境/运行     准备运行环境
          3. DSH 本身      检查 DSH 更新 / 预览计划 / 回归稳定版 / 回滚
          4. 桌面壳        检查桌面壳更新 / 关于
          5. 系统          打开日志目录 / 退出

        DSH 更新与桌面壳更新**必须分开**：用户看到"有更新"时得知道
        换的是 DSH 还是这个窗口程序，否则会以为更新完界面就该变。

        关于「停止后台服务」被移除
        --------------------------
        它和「退出」语义不同（一个停服务留窗口，一个关窗口），但用户点它的
        意图几乎都是"我要结束这一切"。两个入口殊途同归，只会让人犹豫。
        现在只保留「退出」，且退出时**默认连带停止后台服务**——
        想单独留服务的人可以用「仅退出窗口」的替代操作。
        """
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        # 菜单是系统弹窗，默认样式与 DSH 差得最远（直角、系统字体、灰分隔线）。
        # 圆角要靠 WA_TranslucentBackground；dsh_theme.Menu 还会在每次显示时
        # 清掉 QWindows11Style 装上的、会被窗口裁掉右下的投影。
        menu = dsh_theme.Menu()
        self._menu = menu

        act_show = QAction("显示主窗口", self)
        act_show.triggered.connect(self._show_main)

        # 页面白掉 / 卡住时的自救入口。2026-10-20 那次登录白屏证明：
        # 一旦主视图被导航到空白页，没有刷新入口就只能重启整个应用。
        self.act_reload = QAction("重新加载页面", self)
        self.act_reload.triggered.connect(self._reload_page)

        # 只在环境确实缺东西时才可用——平时不给一个点了没反应的菜单项
        self.act_setup = QAction("准备运行环境…", self)
        self.act_setup.setEnabled(False)
        self.act_setup.triggered.connect(self._prepare_env)

        # --- DSH 相关 ---
        self.act_check = QAction("检查 DSH 更新…", self)
        self.act_check.triggered.connect(lambda: self._check_update(manual=True))

        self.act_prev = QAction("加入预览计划", self)
        self.act_prev.setCheckable(True)
        self.act_prev.setChecked(bool(config.get("prerelease_opt_in")))
        self.act_prev.triggered.connect(self._toggle_prerelease)

        self.act_stable = QAction("回归稳定版", self)
        self.act_stable.triggered.connect(self._go_stable)

        self.act_rollback = QAction("回滚 DSH 到上一版本", self)
        self.act_rollback.triggered.connect(self._rollback)

        # --- 鲸坞自身 ---
        self.act_host_check = QAction(f"检查{HOST_NAME}更新…", self)
        self.act_host_check.triggered.connect(lambda: self._check_host_update(manual=True))
        # 开发运行时自更新不可用，直接不给点，避免"点了报错说不行"
        supported, _reason = selfupdate.self_update_supported()
        if not supported:
            self.act_host_check.setEnabled(False)
            self.act_host_check.setText(f"检查{HOST_NAME}更新…（仅打包版可用）")

        self.act_about = QAction(f"关于{HOST_NAME}", self)
        self.act_about.triggered.connect(self._show_about)

        # 控制面板：托盘 menu 装不下的东西都在这里（状态、后续的引导与连接）
        self.act_panel = QAction("控制面板…", self)
        self.act_panel.triggered.connect(self._open_panel)

        # 标题栏自适应：视觉偏好因人而异，给一个关掉的开关
        self.act_titlebar = QAction("标题栏跟随界面配色", self)
        self.act_titlebar.setCheckable(True)
        self.act_titlebar.setChecked(bool(config.get("adaptive_titlebar")))
        self.act_titlebar.setEnabled(dwm.available())
        if not dwm.available():
            self.act_titlebar.setText("标题栏跟随界面配色（系统不支持）")
        self.act_titlebar.triggered.connect(self._toggle_titlebar)

        # --- 系统 ---
        act_logs = QAction("打开日志目录", self)
        act_logs.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(config.log_dir())))

        self.act_quit = QAction("退出", self)
        self.act_quit.triggered.connect(self._quit)

        # 托盘只留"必要且一步到位"的项：打开、检查更新、看日志、退出。
        #
        # 设置类与版本管理类的项都搬到控制面板了（加入预览计划 / 回归稳定版 /
        # 回滚 / 标题栏跟随配色 / 准备运行环境）。理由：托盘 menu 放不了说明文字，
        # "回滚到上一版本"这种有风险的动作应该带上下文，而不是藏在二级菜单里
        # 点一下就执行。
        #
        # 上面那些 QAction 仍然保留（面板走同一套回调，`_refresh_menu` 也还在
        # 更新它们的文字与可用性），只是不再挂进菜单。
        menu.addAction(act_show)
        menu.addAction(self.act_reload)

        # 快捷键挂在主窗口上：托盘菜单里的项只有在菜单打开时才响应按键，
        # 而"页面白了我按 F5"必须随时生效。留着引用，否则 Python 侧被 GC
        # 会连带删掉 C++ 的 QShortcut。
        self._reload_shortcuts = []
        for key in (QKeySequence.Refresh, QKeySequence("Ctrl+R")):
            sc = QShortcut(key, self)
            sc.activated.connect(self._reload_page)
            self._reload_shortcuts.append(sc)
        menu.addAction(self.act_panel)
        menu.addSeparator()
        menu.addAction(self.act_check)
        menu.addAction(self.act_host_check)
        menu.addSeparator()
        menu.addAction(act_logs)
        menu.addAction(self.act_about)
        menu.addSeparator()
        menu.addAction(self.act_quit)

        self.tray.setContextMenu(menu)
        self.tray.setToolTip(f"{APP_TITLE}\n{HOST_NAME} v{HOST_VERSION}")
        self.tray.activated.connect(
            lambda reason: self._show_main()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.messageClicked.connect(self._on_tray_message_clicked)
        self.tray.show()
        self._refresh_menu()
    def _refresh_menu(self):
        """按当前版本状态调整菜单可用性——不做无意义的可点项。"""
        info = self.update_info
        cur = self.handle.install.version if self.handle else None
        is_remote = bool(self.handle is not None
                         and getattr(self.handle, "remote", ""))

        if is_remote:
            # 远程会话期间，"更新 / 回滚 / 回归稳定版 / 加预览 / 准备环境"动的
            # 都是**本机这份** DSH，跟窗口里显示的那台机器毫无关系。留着可点
            # 等于骗人按下去——用户会以为自己在升级远端。一律禁用并写明原因。
            for act in (self.act_check, self.act_prev, self.act_stable,
                        self.act_rollback, self.act_setup):
                act.setEnabled(False)
            self.act_stable.setText("（远程会话中不可用）")
            self.act_rollback.setText("（远程会话中不可用）")
            self.act_check.setText("检查 DSH 更新（远程会话中不可用）")
        else:
            # 回到本地会话：把上面禁掉的那几项放开，再按各自状态细化
            for act in (self.act_check, self.act_prev, self.act_setup):
                act.setEnabled(True)
            self.act_check.setText("检查 DSH 更新…")
            if info:
                self.act_stable.setEnabled(info.action == "downgrade")
                self.act_stable.setText(
                    f"回归稳定版 {info.stable}" if info.action == "downgrade"
                    else "已是稳定版")
            else:
                # 还没拿到通道信息时不知道该回到哪个版本，宁可不给点
                self.act_stable.setEnabled(False)
                self.act_stable.setText("回归稳定版")

            prev = config.get("previous_version")
            rollback_ok = bool(prev and cur and prev != cur)
            self.act_rollback.setEnabled(rollback_ok)
            self.act_rollback.setText(
                f"回滚 DSH 到 {prev}" if rollback_ok else "回滚 DSH 到上一版本")

        if self.host_release:
            self.act_host_check.setText(f"更新{HOST_NAME}到 {self.host_release.version}…")
        else:
            self.act_host_check.setText(
                f"检查{HOST_NAME}更新…" if selfupdate.self_update_supported()[0]
                else f"检查{HOST_NAME}更新…（仅打包版可用）")
    def _on_tray_message_clicked(self):
        if self.update_info and self.update_info.has_action:
            self._show_update_dialog(self.update_info.target)
    def _show_about(self):
        supported, reason = selfupdate.self_update_supported()
        text = (
            f"{HOST_NAME}  v{HOST_VERSION}\n\n"
            f"{HOST_TAGLINE}。\n"
            f"项目地址：https://github.com/{selfupdate.HOST_REPO}\n\n"
            f"{HOST_NAME}更新　{'可用' if supported else '不可用'}\n"
            f"　{('从 GitHub Release 拉取新版本并自动替换。' if supported else reason)}\n\n"
            f"DSH 更新　走官方 npm 通道，与{HOST_NAME}更新互不影响。\n"
        )
        dsh_theme.MessageDialog.about(self, f"关于{HOST_NAME}", text)
