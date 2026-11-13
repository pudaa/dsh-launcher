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


class UpdatesMixin:

    def _check_update(self, manual: bool):
        if getattr(self, "_checking", False):
            if manual:
                # 静默吞掉点击是最糟的反馈——明确告诉用户正在查
                self._info("正在检查更新，请稍候。")
            return
        self._checking = True
        if manual:
            self.act_check.setEnabled(False)
            self.act_check.setText("检查中…")
            host_log("手动检查更新")
        self._checker = CheckWorker()
        self._checker.found.connect(lambda info: self._on_check(info, manual))
        self._checker.failed.connect(lambda msg: self._on_check_failed(msg, manual))
        self._checker.done.connect(self._on_check_done)
        self._checker.start()
    def _on_check(self, info, manual: bool):
        self.update_info = info
        self._refresh_menu()
        if manual:
            # 手动检查：无论有没有更新，结果都要明确给出来
            if info.has_action:
                self._show_update_dialog(info.target)
            else:
                # 说"已是最新"必须附带一句预览通道的情况。否则用户会以为官方没发新版——
                # 0.1.7-rc.1/rc.2 就是这么被漏掉的（当时只看了 alpha 通道）。
                newer = info.newer_preview()
                hint = ("\n\n预览通道上已有更新的 %s。\n"
                        "要获取它，请在托盘菜单里勾选「加入预览计划」。" % newer
                        ) if newer else ""
                lower = info.preview_below_current()
                if lower:
                    # 典型场景：已装 0.2.1-rc.2，尝鲜通道 0.2.1-alpha.5。
                    # 预览计划取最高预览版，不降级；不解释一句用户会以为漏检。
                    hint += ("\n\n说明　　%s 低于已安装版本（alpha < beta < rc）。\n"
                             "　　　　预览计划取预览通道最高版，不会自动降级。"
                             % "、".join("%s %s" % (n, v) for n, v in lower))
                self._info("当前已是最新版本。\n\n"
                           "已安装　　　DSH %s\n"
                           "检查通道　　%s\n"
                           "稳定通道　　%s\n"
                           "候选通道　　%s\n"
                           "尝鲜通道　　%s%s"
                           % (info.current,
                              updater.channel_label(info.channel),
                              info.stable or "（未知）",
                              info.next or "（未知）",
                              info.alpha or "（未知）",
                              hint))
            return
        if info.has_action:
            self.tray.showMessage("DSH 更新", info.summary() + "（点击查看）",
                                  QSystemTrayIcon.MessageIcon.Information, 10000)
    def _on_check_failed(self, msg: str, manual: bool):
        if manual:
            self._warn("检查更新失败。\n\n" + msg + "\n\n"
                       "常见原因：网络不可达、npm 源配置异常。\n"
                       "详细记录见日志目录下的 host.log。")
    def _on_check_done(self):
        self._checking = False
        self.act_check.setEnabled(True)
        self.act_check.setText("检查更新…")
    def _toggle_prerelease(self, checked: bool):
        config.set(prerelease_opt_in=bool(checked))
        self.act_prev.setChecked(bool(checked))
        host_log("预览计划 → %s" % ("开" if checked else "关"))
        # 切换通道后立刻给结果：走手动检查路径，所以会有应用内反馈
        self._check_update(manual=True)
    def _go_stable(self):
        target = self.update_info.stable if self.update_info else None
        if not target:
            self._info("还没有取到稳定版版本号。\n\n请先点「检查更新」，"
                       "拿到结果后再试「回归稳定版」。")
            return
        config.set(prerelease_opt_in=False)
        self.act_prev.setChecked(False)
        host_log("回归稳定版 %s" % target)
        self._show_update_dialog(target)
    def _rollback(self):
        prev = config.get("previous_version")
        if prev:
            self._show_update_dialog(prev)
    def _reinstall_dsh(self):
        """按当前版本重装一遍 DSH。

        存在理由：`npm install -g` 可能只升主包，把精确版本的子依赖留在旧版。
        那种状态下版本号完全正常，但界面会报接口 404 / 账号信息读不出来——
        用户没有任何办法自己判断，只能重装。这个入口就是那条路，
        而且把"先卸载再装"和"子依赖校验"都收在 updater 里，不需要用户懂 npm。
        """
        if not self.handle or not getattr(self.handle, "install", None):
            self._info("DSH 还没运行起来。先让它启动，再来重装。")
            return
        inst = self.handle.install
        dlg = ReinstallDialog(self, inst.version, self.handle)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        new_handle = dlg.new_handle if accepted else None
        dlg.deleteLater()            # 带整篇更新日志的对话框，不释放会一直挂着
        if new_handle:
            self.handle = new_handle
            self._ensure_webview()
            self.stack.setCurrentIndex(1)
            self.view.load(QUrl(self.handle.url))
    def _show_update_dialog(self, target: str):
        if not self.handle:
            return
        info = self.update_info
        if not info or info.target != target:
            # 手工指定一个目标版本（回滚 / 回归稳定版）时，按目标落在哪个通道标注。
            # 不要用 "alpha" in target 这种字符串嗅探——rc 版本会因此被误判成稳定版。
            if info and info.stable == target:
                channel = "latest"
            elif info and info.next == target:
                channel = "next"
            else:
                channel = "alpha"
            info = updater.UpdateInfo(
                current=self.handle.install.version,
                stable=self.update_info.stable if self.update_info else target,
                next=self.update_info.next if self.update_info else None,
                alpha=self.update_info.alpha if self.update_info else None,
                target=target,
                channel=channel,
                # 手工回滚的目标可能比已装版本低：必须标成 downgrade，
                # 否则对话框会把"降级"说成"更新"、也不给旧版兼容性警告。
                action=updater.action_for(self.handle.install.version, target),
            )
        dlg = UpdateDialog(self, info, self.handle)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        new_handle = dlg.new_handle if accepted else None
        dlg.deleteLater()            # 带整篇更新日志的对话框，不释放会一直挂着
        if new_handle:
            self.handle = new_handle
            self._ensure_webview()
            self.stack.setCurrentIndex(1)
            self.view.load(QUrl(self.handle.url))
            self.update_info = None
            self._refresh_menu()
            QTimer.singleShot(2000, lambda: self._check_update(manual=False))
    def _check_host_update(self, manual: bool):
        """检查桌面壳自身更新。与 DSH 检查完全独立。"""
        if getattr(self, "_host_checking", False):
            if manual:
                self._info(f"正在检查{HOST_NAME}更新，请稍候。")
            return
        self._host_checking = True
        if manual:
            self.act_host_check.setEnabled(False)
            self.act_host_check.setText("检查中…")
            host_log(f"手动检查{HOST_NAME}更新")

        self._host_checker = HostCheckWorker()
        self._host_checker.found.connect(
            lambda rel, note: self._on_host_check(rel, note, manual))
        self._host_checker.failed.connect(
            lambda msg: self._on_host_check_failed(msg, manual))
        self._host_checker.done.connect(self._on_host_check_done)
        self._host_checker.start()
    def _on_host_check(self, rel, note: str, manual: bool):
        self.host_release = rel
        self._refresh_menu()
        if not manual:
            if rel:
                self.tray.showMessage(
                    f"{HOST_NAME}更新", f"{HOST_NAME}有新版本 v{rel.version}（点击查看）",
                    QSystemTrayIcon.MessageIcon.Information, 10000)
            return
        if not rel:
            self._info(f"{HOST_NAME}{note}")
            return
        self._show_host_update_dialog(rel)
    def _on_host_check_failed(self, msg: str, manual: bool):
        if getattr(self, "_host_checking", False) and not manual:
            return
        if manual:
            self._warn(f"检查{HOST_NAME}更新失败。\n\n" + msg + "\n\n"
                       f"{HOST_NAME}更新从 GitHub 获取，网络不可达时属正常现象。\n"
                       "不影响 DSH 本身的更新（那条走 npm 通道）。")
    def _on_host_check_done(self):
        self._host_checking = False
        self.act_host_check.setEnabled(True)
        self._refresh_menu()
    def _show_host_update_dialog(self, rel):
        dlg = HostUpdateDialog(self, rel)
        result = dlg.exec()
        dlg.deleteLater()            # 带整篇更新日志的对话框，不释放会一直挂着
        if result == HostUpdateDialog.RESTART_NOW:
            self._relaunch_after_update()
    def _relaunch_after_update(self):
        """新版本**已经替换到位**，这里只负责"拉起新实例 + 自己退出"。

        顺序不能错，每一步都有理由：

        1. **先释放单实例锁。** 新实例如启动时发现管道还连得上，会把自己当成
           重复实例直接退出——用户看到的是"点了重启，什么都没发生"。
           实测 `QLocalServer.close()` 之后新进程就连不上了；判定依据是
           "能否连上那根管道"，不是"有没有同名进程"。
        2. **再拉起新实例，成功后才退出。** 起不来就留在原地报错，
           而不是把自己关掉、把用户丢在一个空桌面前。
        3. **不停后台服务。** 把当前服务的端口随命令行传给新实例去接管，
           省掉"停服务 → 重新启动 → 等就绪"的七八秒。新窗口几乎立刻可用。
        4. **直接 `os._exit`，不走 `_quit()`。** 那里会连带停服务，
           而服务正是我们要留给新实例接管的。代价是跳过 Qt 收尾——
           此时任何"优雅退出"都会把服务一起带走。
        """
        try:
            target = selfupdate.current_exe()
        except Exception:                                        # noqa: BLE001
            target = ""
        port = self.handle.port if self.handle else 0

        srv = getattr(self, "_singleton_server", None)
        if srv is not None:
            srv.close()
        QLocalServer.removeServer(SINGLETON_ID)

        try:
            selfupdate.relaunch(target, attach_port=port)
        except selfupdate.SelfUpdateError as e:
            host_log("更新重启失败：%s" % e)
            dsh_theme.MessageDialog.error(
                self, "无法启动新版本",
                "%s\n\n当前版本仍可继续使用。" % e)
            return

        host_log(f"{HOST_NAME}更新：已拉起新实例（接管端口 {port or '自动分配'}），"
                 "本进程退出")
        # **退出前必须摘掉 Job 的"退出即回收"**（2026-11-13 加）。
        # 服务被挂在一个 KILL_ON_JOB_CLOSE 的作业对象里，本进程 `os._exit`
        # 会让 Job 关闭 → 服务跟着被杀 → 新实例只好重新拉起（慢七八秒，
        # 而且这条路径存在的意义就是把服务留给它接手）。
        # 摘掉之后服务独立存活，新实例按端口接管。
        try:
            from dsh_host import jobobj
            if jobobj.detach_kill_on_close():
                host_log("已解除作业对象的退出回收，服务交由新实例接管")
        except Exception as e:                                 # noqa: BLE001
            host_log("解除作业对象回收失败（新实例会重新拉起服务）："
                     "%s: %s" % (type(e).__name__, e))
        self.tray.hide()
        os._exit(0)
