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
                               QComboBox, QPushButton, QStackedLayout, QStackedWidget,
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


class ServiceMixin:

    def _on_ready(self, handle):
        self.handle = handle
        is_remote = bool(getattr(handle, "remote", ""))
        if is_remote:
            host_log("会话就绪：远程 %s · 隧道端口 %s" % (handle.remote, handle.port))
        elif getattr(handle, "attached", False):
            # 接管上次留下的服务——可能是"关窗口没退出"（设计如此），
            # 也可能是崩溃/强杀后没人收尸。用户看不到进程表，所以要主动说一声，
            # 否则他会以为"我刚打开，怎么就已经占着内存了"。
            # 用托盘气泡而不是对话框：**"关窗口留服务"是设计好的常态**，
            # 每次启动都弹窗会打断人。气泡三秒自散，只做告知。
            host_log("接管已在运行的服务：端口 %s" % handle.port)
            QTimer.singleShot(1500, lambda: self.tray.showMessage(
                HOST_NAME,
                "已接管一个正在运行的背景服务（端口 %s）。\n"
                "关窗口不会停它；点「退出」会一并停止。" % handle.port,
                QSystemTrayIcon.MessageIcon.Information, 3500))
        else:
            host_log("服务就绪：%s | 端口 %s | 来源 %s"
                     % (handle.install.describe(), handle.port, handle.install.source))
        self._ensure_webview()
        # UniqueConnection：`_on_ready` 会被 StartupWorker 与 `_swap_service.done`
        # 反复调用（换来源、迁移数据后重启服务）。不加这个，一次加载会触发 N 条
        # 渲染探针链 + N 条取色链（取色带整窗位图回读，最贵的一项）。
        self.view.loadFinished.connect(self._on_view_loaded,
                                       Qt.ConnectionType.UniqueConnection)
        self.view.load(QUrl(handle.url))
        QTimer.singleShot(15000, self, self._force_reveal)
        self._refresh_menu()
        if is_remote:
            # 远程不做版本复验、不查更新：那台机器上的 DSH 是它自己的事，
            # 我们既改不了也不该替它下判断。界面上的"DSH 版本"会显示"未知"。
            return
        # 版本复验放到后台：启动路径采信了缓存里的版本号，这里补一次真实核对。
        # 对不上（用户在壳之外装了别的版本）时更新显示并提示，但不挡首屏。
        self._reverify = VersionReverifyWorker(handle.install.version)
        self._reverify.changed.connect(self._on_version_changed)
        self._reverify.start()
        if config.get("check_update_on_start"):
            QTimer.singleShot(3000, lambda: self._check_update(manual=False))
    def _on_version_changed(self, fresh):
        """后台复验发现实际版本与界面此前显示的不一致。"""
        shown = self.handle.install.version if self.handle else "?"
        host_log("版本复验：界面显示 %s，实际 %s" % (shown, fresh.version))
        if self.handle:
            # 只换元数据。服务是用同一份 bin_js 拉起来的，版本号变了不代表
            # 需要重启——重启反而会打断用户正在做的事。
            self.handle.install = fresh
            contract.save_state(self.handle)
        self._refresh_menu()
        self._info("检测到 DSH 版本与界面显示的不一致。\n\n"
                   "此前显示　%s\n"
                   "实际版本　%s\n\n"
                   f"通常意味着 DSH 在{HOST_NAME}之外被升级或降级过"
                   "（例如手动执行过 npm install -g）。\n"
                   "显示已按实际版本更新。"
                   % (shown, fresh.version))
        # 当前版本变了，之前算出的"有没有新版"就不再成立，重新算一次
        self._check_update(manual=False)
    def _on_failed(self, msg: str):
        host_log("启动失败：%s" % msg.replace("\n", " | ")[:600])
        # 启动失败是硬失败：托盘气泡容易错过，改走应用内提示
        self._set_tip("启动失败")
        self._warn("DSH 启动失败。\n\n" + msg)
    def _on_needs_setup(self, rep):
        """缺 Node / 缺 DSH：这不是"启动失败"，而是"还没准备好"，走引导。"""
        host_log("环境自检未通过：%s | broken=%s"
                 % (rep.detail or "(无说明)", rep.broken))
        self._set_tip("需要先准备运行环境")
        self.act_setup.setEnabled(True)
        self._show_main()
        dlg = OnboardingDialog(self, rep)
        dlg.exec()
        ok = dlg.succeeded           # 先取出来：下面要延迟销毁这个对话框
        dlg.deleteLater()
        if ok:
            host_log("环境准备完成，重新启动服务")
            self.act_setup.setEnabled(False)
            self._set_tip("环境已就绪，正在启动…")
            self.stack.setCurrentIndex(0)
            self._loading_busy(True)
            self._launch_startup()
        else:
            self._set_tip("运行环境尚未就绪。可从托盘菜单「准备运行环境」重试。")
            self.tray.showMessage(
                "DSH", "运行环境尚未就绪。可从托盘菜单「准备运行环境」重试。",
                QSystemTrayIcon.MessageIcon.Information, 6000)
    def _prepare_env(self):
        self.stack.setCurrentIndex(0)
        self._loading_busy(True)
        self._set_tip("正在检查运行环境…")
        # route=False：**这是"回到本机"，不是一次新的启动**。
        # 传 True 会去读启动途径配置——从远程断开时又把用户连回远程，
        # 或者每次断开都弹一遍选择面板。
        self._launch_startup(route=False)
    def _apply_start_route(self) -> bool:
        """按「启动途径」决定先起谁。返回 True 表示已接管，不再起本地。

        为什么要这个（2026-11-16 老大提的场景）：有人打开鲸坞**只是为了连另一台
        机器**，这时再自动起一份本地服务纯属浪费（600 MB 上下、还要等十几秒）。

        `start_mode`：
          local  —— 起本机（默认，和以前一致）
          remote —— 直接连 `start_remote_host` 指定的主机
          ask    —— 每次启动问一次。使用场景不固定的人用这个

        没配置任何远程主机时一律回退本机——否则用户会卡在一个空的选择面板前。
        """
        mode = str(config.get("start_mode") or "local")
        if mode not in ("remote", "ask"):
            return False
        try:
            hosts = remote.load_hosts()
        except Exception as e:                                   # noqa: BLE001
            host_log("读取远程主机失败，改走本机：%s: %s" % (type(e).__name__, e))
            return False
        if not hosts:
            host_log("启动途径配置为 %s，但没有远程主机，改走本机" % mode)
            return False

        if mode == "ask":
            picked = self._ask_start_route(hosts)
            if picked is None:
                return False                 # 选了本机
            host = picked
        else:
            want = str(config.get("start_remote_host") or "")
            host = next((h for h in hosts if h.id == want), hosts[0])

        host_log("按启动途径直接连接远程：%s" % host.name)
        self._set_tip("正在连接 %s…" % host.name)
        self._panel_remote_connect(host)
        return True

    def _ask_start_route(self, hosts):
        """启动时的选择面板。返回 None=本机，SshHost=远程，False=关掉窗口（当本机）。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("从哪儿开始")
        dlg.setModal(True)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(14)

        title = QLabel("这次要连本机，还是另一台机器？")
        title.setObjectName("dlgTitle")
        lay.addWidget(title)
        hint = QLabel("可以在「运行方式」里把默认值改成固定的，之后就不再问。")
        hint.setObjectName("dlgHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        combo = QComboBox()
        combo.addItem("本机 · 这台电脑上的 DSH")
        for h in hosts:
            combo.addItem("%s · %s" % (h.name, h.display_address))
        want = str(config.get("start_remote_host") or "")
        for i, h in enumerate(hosts, start=1):
            if h.id == want:
                combo.setCurrentIndex(i)
                break
        lay.addWidget(combo)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("ghost")
        btn_ok = QPushButton("开始")
        btn_ok.setObjectName("action")
        btn_ok.setDefault(True)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        lay.addLayout(row)

        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        dsh_theme.style_dialog(dlg, self.panel_palette() if hasattr(self, "panel_palette") else None)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            # 取消不等于"别启动"——把人丢在空窗口前更糟，照常起本机。
            host_log("启动途径选择被取消，按本机启动")
            return None
        idx = combo.currentIndex()
        return None if idx <= 0 else hosts[idx - 1]

    def _activate_version(self, version: str) -> None:
        """切到版本库里的另一个版本。

        顺序不能反：**先停服务再切指针**。Windows 上 node_modules 被 node
        占用时，切换后的启动会读到一半旧的、一半新的模块树——那种失败
        比"切不过去"难查得多。
        """
        from dsh_host import versions
        cur = versions.active_version()
        if version == cur:
            self._info("这已经是当前使用的版本。")
            return
        host_log("切换 DSH 版本：%s → %s" % (cur or "(无)", version))
        try:
            self._stop_service()
        except Exception as e:                                   # noqa: BLE001
            host_log("停服务失败，仍继续切换：%s: %s" % (type(e).__name__, e))
        ok, msg = versions.activate(version)
        if not ok:
            self._warn("切换失败。\n\n" + msg)
            # 切不过去也要把服务拉回来，否则界面停在空窗口
            self._prepare_env()
            return
        # 切换后必须清掉"启动路径"缓存里那个已就绪标记
        self._set_tip("正在用 %s 启动…" % version)
        self._refresh_versions_page()
        self._prepare_env()

    def _stop_service(self) -> int:
        """停止后台服务，返回终止的进程数。

        不再是菜单项（见 _quit 的说明），改为退出流程内部调用。
        保留成独立方法是为了让"停服务"和"退窗口"两件事在代码里仍然分开——
        将来若要恢复成两个入口，直接挂回菜单即可。

        `self.handle` 为空时（启动还没就绪、或启动失败）退到 `service.json`
        里记的端口：那种情况下进程是**已经起来了**的，不按端口反查就只能干看着
        它常驻。`stop()` 对映像名做了校验，误杀别的程序的概率很低（2026-10-20 体检）。
        """
        # 远程会话：停的是隧道（和可选的远端进程），没有本地 node 进程可杀。
        # 这里不能落到下面的 `contract.stop()`——那会按端口去杀本机进程，
        # 而那个端口是我们自己的隧道监听端口，杀了等于砍掉自己的转发。
        if self._remote is not None or (self.handle is not None
                                        and getattr(self.handle, "remote", "")):
            self._stop_remote()
            self.handle = None
            # 开了「保留本地服务」时，本地那份**还在跑**，这里必须接着停掉。
            # 漏了这一步，用户点「退出」会留下一个 600 MB 的孤儿 node——
            # Job Object 只在他崩溃/被强杀时兜底，正常退出走的就是这条路径
            # （2026-11-16 加 keep_local 时差点漏掉）。
            local = getattr(self, "_local_handle", None)
            if local is not None and not getattr(local, "remote", ""):
                n_local = contract.stop(local, port=local.port or 0)
                if n_local:
                    contract.save_state(None)
                self._local_handle = None
                host_log("退出时一并停止被保留的本地服务（%d 个进程）" % n_local)
                return 1 + n_local
            return 1

        if self.handle is not None:
            port = self.handle.port or 0
        else:
            state = contract.load_state() or {}
            try:
                port = int(state.get("port") or config.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
        n = contract.stop(self.handle, port=port)
        if n:
            contract.save_state(None)
        return n
    def _swap_service(self, note: str = "") -> None:
        """按当前配置重启后台服务（来源 / 数据目录变了之后）。"""
        if getattr(self, "_swapping", False):
            self._info("正在重启服务，请稍候。")
            return
        self._swapping = True
        self.stack.setCurrentIndex(0)
        self._loading_busy(True)
        self._set_tip("正在按新配置重启服务…")
        w = ServiceSwapWorker(self.handle, config.service_log())
        self._swap_worker = w

        def done(ok: bool, detail: str, handle) -> None:
            self._swapping = False
            if ok and handle is not None:
                if note:
                    host_log(note)
                self._on_ready(handle)
            else:
                host_log("重启服务失败：%s" % detail)
                self._warn("服务没有起来：\n\n%s\n\n可到「状态检测」页查看详情。" % detail)

        w.done.connect(done)
        w.start()
    def _switch_source(self, mode: str) -> None:
        """切换 DSH 来源。私有未就绪时先复制一份现有安装。"""
        if str(config.get("dsh_source") or "auto") == mode:
            return
        if mode == "private" and not provision.private_ready():
            inst = self.handle.install if self.handle else None
            if inst is None:
                self._info("还没有可用的 DSH。先到「本机部署」准备运行环境。")
                return
            if not dsh_theme.MessageDialog.ask(
                    self, "准备私有副本",
                    "会在应用自己的数据目录里放一份 DSH（约 250 MB），"
                    "与命令行里那份互不影响。\n\n"
                    "现在把当前安装（%s）复制过去，通常几十秒。" % inst.version,
                    "复制并启用", "取消"):
                return
            ok, payload = self._run_data_task(
                "private", "准备私有副本",
                "正在把 %s 复制到应用的数据目录。" % inst.version,
                "约 250 MB、上千个文件。复制在后台进行，界面不会假死；"
                "中途请不要关闭窗口——留下半个副本比没有更难收拾。",
                install=inst)
            if not ok:
                self._warn("准备私有副本失败：\n\n%s"
                           % ((payload or {}).get("error") or "详见日志"))
                return
        config.set(dsh_source=mode)
        self._swap_service("运行方式切换为 %s" % mode)
