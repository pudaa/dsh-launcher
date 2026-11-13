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
from dsh_ui.panel.styles import MEM_SAMPLE_MS
from dsh_ui import theme as dsh_theme
from dsh_ui.dialogs import (DataTaskDialog, HostUpdateDialog, OnboardingDialog,
                            ReinstallDialog, UpdateDialog)
from dsh_ui.prelude import *        # noqa: F401,F403
from dsh_ui.workers import (CheckWorker, DataTaskWorker, DiagnoseWorker, HostCheckWorker,
                            HostUpdateWorker, NotesWorker, ProvisionWorker,
                            RemoteConnectWorker, ServiceSwapWorker, StartupWorker,
                            UpdateWorker, VersionReverifyWorker)


MEM_LOG_MAX_BYTES = 2 * 1024 * 1024
MEM_WEB_WARN_MB = 1200


class PanelMixin:

    JS_MEM_PROBE = ("(function(){try{var m=performance.memory||{};"
                    "return JSON.stringify({h:m.usedJSHeapSize||0,"
                    "t:m.totalJSHeapSize||0,"
                    "n:document.getElementsByTagName('*').length});}"
                    "catch(e){return '';}})()")

    def _open_panel(self):
        """打开控制面板。

        面板只被创建一次，之后是 show/raise —— 反复开关不该重复构造整个界面。
        面板与主窗口之间走 `dsh_panel.PanelHost` 这个窄接口：面板不认识 MainWindow，
        所以主窗口内部怎么改都不会连累它。
        """
        if getattr(self, "_panel", None) is None:
            host = dsh_panel.PanelHost(
                install=lambda: self.handle.install if self.handle else None,
                handle=lambda: self.handle,
                host_release=lambda: self.host_release,
                # 主题复用标题栏取色的既有判定，面板不自己判
                theme=lambda: "dark" if self._theme_dark else "light",
                open_path=lambda p: QDesktopServices.openUrl(QUrl.fromLocalFile(p))
                if p else self._info("还没有可打开的目录。"),
                open_web=self._open_in_browser,
                log_dir=config.log_dir,
                flag=self._panel_flag,
                set_flag=self._panel_set_flag,
                action=self._panel_action,
                # 远程：连接/断开必须由主窗口做（它管隧道生命周期和收尾），
                # 面板只拿到"发指令"和"读状态"两件事。
                remote_hosts=remote.load_hosts,
                remote_save=remote.save_hosts,
                remote_connect=self._panel_remote_connect,
                remote_disconnect=self._panel_remote_disconnect,
                remote_state=self._panel_remote_state,
            )
            self._panel = dsh_panel.ControlPanel(host, parent=None)
        self._panel.show()
        self._panel.raise_()
        self._panel.activateWindow()
        # 面板可能停在上次看的那一页，回来时刷新一下底部状态条
        self._panel.refresh_statusbar()
    def _open_in_browser(self):
        """用系统浏览器打开当前的 DSH 地址。"""
        if self.handle and self.handle.url:
            QDesktopServices.openUrl(QUrl(self.handle.url))
        else:
            self._info("服务还没就绪，暂时没有可打开的地址。")
    def _start_mem_watch(self) -> None:
        if not hasattr(procmem, "snapshot"):
            return
        if self._mem_timer is not None:
            return
        t = QTimer(self)
        t.setInterval(MEM_SAMPLE_MS)
        t.timeout.connect(self._mem_tick)
        t.start()
        self._mem_timer = t
    def _mem_tick(self) -> None:
        try:
            snap = procmem.snapshot()
        except Exception as e:                                 # noqa: BLE001
            host_log("内存采样失败：%s: %s" % (type(e).__name__, e))
            return
        page = getattr(self, "page", None)   # __init__ 里到 _ensure_webview 之前还没有它
        if page is None:
            self._write_mem(snap, "")
            return
        try:
            page.runJavaScript(
                self.JS_MEM_PROBE,
                lambda v: self._write_mem(snap, v))
        except RuntimeError:
            # 视图正在销毁/重建（换来源、迁移数据），这一轮就只记进程数据
            self._write_mem(snap, "")
    def _write_mem(self, snap: dict, js_raw) -> None:
        series = procmem.summary_line_from(snap) if hasattr(
            procmem, "summary_line_from") else ""
        parts = [series] if series else []
        data = dsh_devtools._as_json(js_raw) if isinstance(js_raw, str) else None
        if isinstance(data, dict):
            h = data.get("h") or 0
            t = data.get("t") or 0
            n = data.get("n")
            if h:
                parts.append("JS 堆 %.0f/%.0f MB"
                             % (h / 1048576.0, t / 1048576.0))
            if isinstance(n, int) and n >= 0:
                parts.append("DOM %d 节点" % n)
        web = (snap.get("groups") or {}).get("webengine") or {}
        if (web.get("private") or 0) >= MEM_WEB_WARN_MB:
            parts.append("网页进程已超过 %d MB，可「重新加载页面」释放"
                         % MEM_WEB_WARN_MB)
        line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                           " ｜ ".join(parts))
        path = os.path.join(config.log_dir(), "mem.log")
        try:
            if os.path.isfile(path) and os.path.getsize(path) > MEM_LOG_MAX_BYTES:
                # 只留最后一段：这份日志的价值在"最近几小时"
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.read().splitlines()[-400:]
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(tail) + "\n")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    def _panel_flag(self, name: str) -> bool:
        return {
            "prerelease_opt_in": bool(config.get("prerelease_opt_in")),
            "adaptive_titlebar": bool(config.get("adaptive_titlebar")),
            "dev_mode": bool(config.get("dev_mode")),
            "webengine_hw_accel": bool(config.get("webengine_hw_accel")),
        }.get(name, False)
    def _panel_set_flag(self, name: str, value: bool) -> None:
        if name == "prerelease_opt_in":
            self._toggle_prerelease(bool(value))
        elif name == "adaptive_titlebar":
            self._toggle_titlebar(bool(value))
        elif name == "dev_mode":
            self._toggle_dev_mode(bool(value))
        elif name == "webengine_hw_accel":
            # Chromium 只读一次 flags（在任何 PySide6 导入之前），所以只能
            # 落盘 + 提示重启，运行中切换没有意义。
            config.set(webengine_hw_accel=bool(value))
            host_log("硬件加速渲染 → %s（重启应用后生效）"
                     % ("开" if value else "关"))
    def _panel_action(self, name: str) -> None:
        if name == "check_update":
            self._check_update(manual=True)
        elif name == "go_stable":
            self._go_stable()
        elif name == "rollback":
            self._rollback()
        elif name == "provision":
            self._prepare_env()
        elif name == "reinstall_dsh":
            self._reinstall_dsh()
        elif name == "use_private":
            self._switch_source("private")
        elif name == "use_system":
            self._switch_source("system")
        elif name == "migrate_data":
            self._migrate_data()
        elif name == "clear_cache":
            self._clear_cache()
        elif name == "clear_all":
            self._clear_all()
        elif name == "open_devtools":
            self._open_inspector()
        elif name == "open_devlog":
            self._open_dev_window()
    def _panel_remote_state(self) -> dict:
        return dict(self._remote_state)
    def _push_remote_state(self) -> None:
        """把远程状态推给面板（面板没开过就什么都不做）。"""
        panel = getattr(self, "_panel", None)
        if panel is None:
            return
        try:
            panel.refresh_remote()
        except Exception as e:                                   # noqa: BLE001
            host_log("刷新远程状态失败：%s: %s" % (type(e).__name__, e))
    def _panel_remote_connect(self, host_obj) -> None:
        """面板点了某台主机的「连接」。"""
        worker = self._remote_worker
        if worker is not None and worker.isRunning():
            self._info("正在连接中，请稍候。")
            return
        if self._remote is not None:
            # 换主机 = 先断旧的。断开要在后台做（可能要等远端进程退出），
            # 但它有超时上限，这里同步等一小会儿可接受。
            self._stop_remote()
        self._remote_host = host_obj
        self._remote_state = {"host_id": host_obj.id, "name": host_obj.name,
                              "stage": "正在连接 SSH…", "connected": False,
                              "error": ""}
        self._push_remote_state()

        # 远程与本地服务互斥：本地那份先停掉，否则两个 DSH 抢同一套界面状态，
        # 状态栏、菜单、退出收尾全得分叉判断。停本地是秒级的，用户感觉不到。
        if self.handle is not None and not getattr(self.handle, "remote", ""):
            host_log("切换远程：先停本地服务")
            try:
                self._stop_service()
            except Exception as e:                               # noqa: BLE001
                host_log("停本地服务失败：%s: %s" % (type(e).__name__, e))

        w = RemoteConnectWorker(host_obj)
        w.stage.connect(self._on_remote_stage)
        w.ready.connect(self._on_remote_ready)
        w.failed.connect(self._on_remote_failed)
        self._remote_worker = w
        w.start()
    def _on_remote_stage(self, text: str) -> None:
        self._remote_state["stage"] = text
        self._push_remote_state()
    def _on_remote_ready(self, session, url: str) -> None:
        host_obj = self._remote_host
        self._remote = session
        self._remote_state.update({"connected": True, "stage": "", "error": ""})
        self._push_remote_state()

        endpoint = getattr(session, "endpoint", None)
        tunnel = getattr(session, "tunnel", None)
        local_port = getattr(tunnel, "local_port", 0) or 0
        name = host_obj.name if host_obj else "远程"
        # 远端装的是哪个版本我们不知道，也不该猜。写"未知"是诚实的；
        # 界面上"DSH 版本"那一行会显示它，而不是拿本地的版本号冒充。
        inst = contract.DshInstall(node="", bin_js="", pkg_root="", prefix="",
                                   npm_cli=None, version="未知",
                                   source="远程 · %s" % name)
        handle = ServiceHandle(
            install=inst,
            pid=int(getattr(endpoint, "pid", 0) or 0),
            host="127.0.0.1", requested_port=local_port, port=local_port,
            url=url, token=getattr(endpoint, "token", ""),
            log_path=config.service_log(), started_at=time.time(),
            remote=name)
        host_log("远程会话就绪：%s · 远端端口 %s · 隧道端口 %s"
                 % (name, getattr(endpoint, "port", "?"), local_port))
        self._on_ready(handle)
    def _on_remote_failed(self, kind: str, message: str) -> None:
        self._remote_state.update({"connected": False, "stage": "",
                                   "error": message})
        self._push_remote_state()
        hint = remote.KIND_HINTS.get(kind, "")
        text = message if (not hint or hint in message) else (message + "\n\n" + hint)
        host_log("远程连接失败（%s）：%s" % (kind, message.splitlines()[0][:120]))
        self._warn("远程连接失败。\n\n" + text)
    def _panel_remote_disconnect(self) -> None:
        worker = self._remote_worker
        if worker is not None and worker.isRunning():
            self._info("正在连接中，暂时不能断开。")
            return
        self._stop_remote()
    def _stop_remote(self, stop_remote_side: bool | None = None) -> None:
        """收掉远程会话。

        `stop_remote_side` 为 None 时交给主机自己的设置（保留/停止/每次询问），
        其中"每次询问"在这里按**保留**处理——退出流程里不该弹窗拦人。
        想"停掉远端"的交互必须由调用方明确传入 True。
        """
        session, self._remote = self._remote, None
        if session is not None:
            try:
                session.close(stop_remote=stop_remote_side)
                host_log("远程会话已断开")
            except Exception as e:                               # noqa: BLE001
                host_log("断开远程会话失败：%s: %s" % (type(e).__name__, e))
        self._remote_host = None
        self._remote_state = {}
        self._push_remote_state()
    def _run_data_task(self, task: str, title: str, headline: str, hint: str,
                       **kw) -> tuple[bool, object]:
        """起一个 DataTaskWorker，用模态进度窗等它跑完。

        模态是刻意的：这期间主窗口不可操作，而底下的服务/WebEngine 已经被
        我们停掉释放了——让用户还能点点点只会得到一堆"连不上"。
        """
        w = DataTaskWorker(task, None, **kw)
        dlg = DataTaskDialog(self, title, headline, hint, w)
        self._data_worker = w          # 持有引用，别让 GC 在 run() 中途收走
        try:
            w.start()
            dlg.exec()
            w.wait(5000)               # 兜底：线程活着退出会让 Qt abort
        finally:
            w.deleteLater()
            self._data_worker = None
        return dlg.ok, dlg.result_payload
    def _migrate_data(self) -> None:
        # 这里的 breakdown() 只为了弹窗里那句"共 N 个文件"；它带 3 秒预算，
        # 不会像复制本身那样几十秒。真正的重活全在后台线程里。
        info = storage.breakdown()
        target = QFileDialog.getExistingDirectory(
            self, "选择新的数据目录（建议空目录）", info["root"])
        if not target:
            return
        if not dsh_theme.MessageDialog.ask(
                self, "迁移数据目录",
                "把数据从\n%s\n搬到\n%s\n\n"
                "先停止后台服务，复制完成后自动重启。共 %d 个文件（%s）。"
                % (info["root"], target, info["files"],
                   storage.str_of_bytes(info["bytes"])),
                "开始迁移", "取消"):
            return
        old = info["root"]
        self._stop_service()
        self._drop_webview()
        ok, payload = self._run_data_task(
            "migrate", "迁移数据目录",
            "正在把数据复制到\n%s" % target,
            "复制完成后会核对文件数，再清理旧目录。整个过程在后台进行，"
            "界面不会再出现「未响应」。",
            target=target, old=old)
        if not ok:
            err = (payload or {}).get("error") or "详见日志。原目录未被改动。"
            self._warn("迁移失败：\n\n%s" % err)
            self._swap_service()
            return
        new_root = (payload or {}).get("root") or target
        self._swap_service("数据目录迁移到 %s" % new_root)
        self._info("数据已搬到：\n%s\n\n旧目录里没有删干净的部分可以手动清理。"
                   % new_root)
    def _clear_cache(self) -> None:
        info = storage.breakdown()
        if not dsh_theme.MessageDialog.ask(
                self, "清理缓存",
                "删除浏览器缓存、日志与登录态（约 %s）。\n\n"
                "设置和 DSH 安装不受影响；清理后需要重新登录 DSH。"
                % storage.str_of_bytes(info["bytes"]),
                "清理", "取消"):
            return
        self._stop_service()
        self._drop_webview()
        ok, payload = self._run_data_task(
            "clear_cache", "清理缓存",
            "正在删除浏览器缓存、日志与登录态。",
            "删除在后台进行；完成后会自动重启服务，需要重新登录 DSH。")
        if not ok:
            self._warn("清理失败：\n\n%s" % ((payload or {}).get("error")
                                            or "详见日志"))
            self._swap_service()
            return
        p = payload or {}
        self._swap_service("已清理缓存：%d 个文件（%s）"
                           % (p.get("files", 0),
                              storage.str_of_bytes(p.get("bytes", 0))))
    def _clear_all(self) -> None:
        info = storage.breakdown()
        if not dsh_theme.MessageDialog.ask(
                self, "清空全部数据",
                f"删除{HOST_NAME}的全部数据（%s，%d 个文件）：设置、登录态、日志，"
                "以及私有 DSH 安装。\n\n这个操作不可撤销。"
                % (storage.str_of_bytes(info["bytes"]), info["files"]),
                "全部清空", "取消"):
            return
        self._stop_service()
        self._drop_webview()
        ok, payload = self._run_data_task(
            "clear_all", "清空全部数据",
            f"正在删除{HOST_NAME}的全部数据。",
            "删除在后台进行，完成后需要退出应用重新启动。")
        if not ok:
            self._warn("清空未完成：\n\n%s" % ((payload or {}).get("error")
                                              or "详见日志"))
            return
        n = (payload or {}).get("files", 0)
        self._warn("已清空全部数据（%d 个文件）。\n\n"
                   "退出应用后重新启动，会回到首次运行的引导流程。" % n)
    def _toggle_dev_mode(self, on: bool):
        """开关开发者模式。

        开：挂上网络记录，并直接打开 **Chromium 自带的开发者工具**
            （Elements / Console / Network / Sources 全都在里面，老大明确要求用现成的）。
        关：停止记录并收起我们自己那些窗口，但**不清空**已记录的内容（误关还能再看）。
        """
        config.set(dev_mode=bool(on))
        self._apply_dev_mode(bool(on))
        host_log("开发者模式 → %s" % ("开" if on else "关"))
        if on:
            self._open_inspector()
        else:
            for win in (self._inspector, self._devwin):
                if win is not None:
                    win.close()
    def _apply_dev_mode(self, on: bool) -> None:
        """按开关接线：控制台采集、请求拦截器、终端 fd 采集。

        **关闭时必须真的停下来**，而不是"挂在那里但不记录"：
        - 不接管 fd、不起读线程、不写 console.log（dsh_console.shutdown）；
        - 不挂请求拦截器（挂上就代表每个请求都要回调进 Python）；
        - 页面不再收集 console 消息。
        这样"关闭开发者模式"在任务管理器里应当看不出差别。

        不碰任何窗口：自测可以直接调它验证资源状态。
        """
        page = getattr(self, "page", None)
        if page is not None:
            page.set_capture_console(bool(on))
        self.recorder.net_enabled = bool(on)

        if on:
            dsh_console.install()
            if self.profile is not None and self.interceptor is not None:
                self.profile.setUrlRequestInterceptor(self.interceptor)
        else:
            if self.profile is not None:
                self.profile.setUrlRequestInterceptor(None)
            dsh_console.shutdown()
    def _open_inspector(self):
        """打开 Chromium 开发者工具（窗口关闭即释放那个渲染实例）。"""
        if self.view is None or getattr(self, "page", None) is None:
            self._info("DSH 界面还没加载好，稍后再试。")
            return
        if self._inspector is not None:
            self._inspector.show()
            self._inspector.raise_()
            self._inspector.activateWindow()
            return
        try:
            self._inspector = dsh_devtools.InspectorWindow(
                self.page, "dark" if self._theme_dark else "light")
        except Exception as e:                                 # noqa: BLE001
            self._inspector = None
            host_log("开发者工具打不开：%s: %s" % (type(e).__name__, e))
            self._warn("开发者工具打不开：%s: %s" % (type(e).__name__, e))
            return
        self._inspector.destroyed.connect(self._on_inspector_gone)
        self._inspector.show()
        self._inspector.raise_()
        self._inspector.activateWindow()
        host_log("已打开开发者工具（Chromium DevTools）")
    def _on_inspector_gone(self, *_a):
        self._inspector = None
    def _open_dev_window(self):
        """打开轻量日志窗口（只创建一次，之后是 show/raise）。

        它是给"工具没开时也想回看报错"用的：控制台与请求记录**常开**，
        而 DevTools 只能看到它自己打开之后的控制台。布局页只是坐标快照，
        不要拿它替代 Elements。
        """
        if self.view is None or getattr(self, "page", None) is None:
            self._info("DSH 界面还没加载好，稍后再试。")
            return
        if self._devwin is None:
            self._devwin = dsh_devtools.DevWindow(
                page=self.page, recorder=self.recorder,
                theme="dark" if self._theme_dark else "light",
                open_inspector=self._open_inspector)
        self._devwin.show()
        self._devwin.raise_()
        self._devwin.activateWindow()
