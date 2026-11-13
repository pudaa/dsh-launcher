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

from PySide6.QtCore import (QCoreApplication, QEvent, QPoint, QRect,
                            QStandardPaths, Qt, QThread, QTimer, QUrl, qVersion)
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


MANUAL_RESET_WINDOW_S = 20
SURFACE_RESET_WINDOW_S = 120
SURFACE_RESET_LIMIT = 2


class ViewMixin:

    JS_RENDERED = ("(function(){try{var b=document.body;"
                   "return (b&&b.children.length>0&&(b.textContent||'').length>40)"
                   "?'1':'0';}catch(e){return '0';}})()")

    def _ensure_webview(self):
        if self.view is not None:
            return
        # `DSH_PROFILE_DIR` 只给自测/排查用：让一次冒烟测试不去碰用户正在用的
        # profile 目录。踩过的坑（2026-10-20）：前一个探针进程还留着一个
        # QtWebEngineProcess 握着 profile 的缓存锁，冒烟测试这边就一路报
        # "Unable to move the cache: 拒绝访问 (0x5)"，然后启动链路超时——
        # 看起来像产品缺陷，其实是两个进程抢同一个缓存目录。
        profile_root = (os.environ.get("DSH_PROFILE_DIR")
                        or config.profile_dir())
        # 缓存路径必须显式指定并按 Qt 版本隔离：默认路径只按应用名推导，
        # 6.10 与 6.11 共用同一个 Cache，后启动的迁移失败就是一屏
        # `Unable to move the cache`。自测设了 DSH_PROFILE_DIR 时跟着临时
        # 目录走，别去碰用户真实缓存。
        if os.environ.get("DSH_PROFILE_DIR"):
            cache_dir = os.path.join(profile_root, "cache")
        else:
            cache_dir = config.webview_cache_dir(qVersion())
        os.makedirs(cache_dir, exist_ok=True)

        # 默认 profile（`QWebEngineView(self)` 构造时那个临时 page 用的）
        # **没法用 setCachePath 改路径**：实测 `defaultProfile().cachePath()`
        # 设置前后都是空串，它始终走 applicationName 推导出来的默认目录。
        # 所以隔离放在 `dsh_gui_qt.main()`：源码运行时 app 名带 Qt 版本后缀。
        # 这里只管自定义 profile 的路径。
        self.profile = QWebEngineProfile("dsh-profile", self)
        self.profile.setPersistentStoragePath(profile_root)
        self.profile.setCachePath(cache_dir)
        # 下载必须自己接（2026-10-21 老大报"点下载 session 没反应"）：
        # QtWebEngine 对没人处理的 downloadRequested **静默取消**，而前端
        # `a.download` 的 click 早就同步返回了 —— 不报错、不落盘、什么都不发生。
        # 父对象给 profile：换来源/迁移数据重建 profile 时一起回收，不会残留。
        self.downloads = dsh_devtools.DownloadManager(
            self.profile,
            choose_path=self._ask_download_path,
            notify=self._download_notify,
            log=host_log,
            parent=self.profile,
        )
        # 拦截器**不在关闭时挂载**：挂上之后每个请求都会回调进 Python
        # （图片、字体、轮询都算），"关闭状态只读一个布尔值"也是有成本的。
        # 关闭时干脆不挂，开关打开时再挂（见 _apply_dev_mode）。
        dev_on = bool(config.get("dev_mode"))
        self.recorder.net_enabled = dev_on
        self.interceptor = dsh_devtools.RequestRecorder(self.recorder, self.profile)
        if dev_on:
            self.profile.setUrlRequestInterceptor(self.interceptor)
        self.view = QWebEngineView(self)
        # 页面策略（外链出口 + 控制台采集）在 ShellPage 里，不写在界面方法里：
        # 这样它可以脱离主窗口单独测（见 dsh_devtools.selftest_checks）。
        self.page = dsh_devtools.ShellPage(
            self.profile, self.view,
            base_url=lambda: self.handle.url if self.handle else "",
            recorder=self.recorder,
            open_external=self._open_external_url,
            view=self.view,
            capture_console=dev_on,
            notify=self._page_notify,
            # 最后一级恢复：换掉整个 Web 视图（只有界面层能做到）
            hard_recover=self._reset_webview,
        )
        self.view.setPage(self.page)
        # 右键交还给网页自己处理。
        #
        # 默认情况下 QWebEngineView 会在**每一次**右键时弹出 Qt 自己的
        # 标准菜单（刷新 / 保存网页 / 前进后退…），而且它是在 widget 层生成的，
        # **网页里 preventDefault 拦不住** —— 结果是网页自己的右键菜单被它盖住。
        # （老大实测反馈："弹出来的好像是 PySide6 的右键菜单"。）
        #
        # NoContextMenu 只是让 Qt 不出这个菜单；右键的鼠标事件**照常转发给
        # Chromium**，所以网页里自己的 contextmenu 处理与 DOM 菜单都会正常工作。
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.stack.addWidget(self.view)
        # StackAll 下新加的页会被顶到最上层。加载页必须继续盖在 Web 视图
        # 上面，直到前端真的画出东西（见 _reveal_when_rendered）——否则
        # 用户会先看到 Web 视图自己的白色/空白底面。
        if self.loading_page is not None:
            self.stack.setCurrentWidget(self.loading_page)
            self._loading_busy(True)
    def _on_view_loaded(self, ok: bool):
        """页面加载完成回调。

        标题栏取色在这里触发，而**不是**等一个固定时长。
        早期实现是 `singleShot(4000, ...)` 死等 4 秒，老大反馈
        "加载完网页后有明显延迟"——正是这 4 秒。

        现在改成"立刻试一次 + 失败退避重试"：既然有了空白检测
        （见 dwm.FLAT_RATIO），就能区分"页面还没画出来"和"页面本身是纯色"，
        于是在页面真正渲染完成的那一刻就上色，通常 1 秒内完成。
        """
        if ok:
            self._reveal_when_rendered()
        if ok and config.get("adaptive_titlebar") and dwm.available():
            self._schedule_titlebar_probe(0)
            self._start_theme_watch()
    def _reveal_when_rendered(self, tries: int = 0) -> None:
        """等前端真的画出内容，再把加载页换成 Web 视图。

        `loadFinished` 只代表 **HTML 到了**。DSH 是 SPA，此刻 `#root` 还是空的，
        立刻切过去就是一片白——这正是"页面加载完了却要愣一下"的来源。
        这里用一个很便宜的 DOM 探针轮询（只读 children/textContent，不读布局），
        最多等约 1.8 秒；超时也切——宁可看到白，也不能永远卡在加载页。
        """

        def decided(value) -> None:
            if str(value).strip() == "1" or tries >= 12:      # 12 × 150ms ≈ 1.8s
                # StackAll 下这一步只是把 Web 视图 raise 到最上层，
                # 不改变任何一页的可见性/尺寸，所以不会触发重新排版。
                self._show_webpage()
            else:
                QTimer.singleShot(
                    150, lambda: self._reveal_when_rendered(tries + 1))

        try:
            self.page.runJavaScript(self.JS_RENDERED, decided)
        except Exception:                                      # noqa: BLE001
            self._show_webpage()
    def _show_webpage(self) -> None:
        """把 Web 视图露出来，并把加载页的动画停掉（见 _loading_busy）。"""
        self.stack.setCurrentIndex(1)
        self._loading_busy(False)
    def _force_reveal(self) -> None:
        """兜底：等不到前端首绘也切过去——宁可看到白，也不能永远卡加载页。"""
        self._show_webpage()
    def _page_notify(self, msg: str) -> None:
        """页面监护（渲染进程退出/白屏自动恢复）给用户的一句话。

        托盘气泡是这里唯一不打断操作的形式——自动恢复本身已经做了恢复，
        通知只是解释"刚才闪了一下/刷新了一下"是什么原因。
        """
        host_log("页面监护：%s" % msg)
        tray = getattr(self, "tray", None)
        if tray is not None and tray.isVisible():
            try:
                tray.showMessage("DSH 页面", msg)
            except Exception:                                   # noqa: BLE001
                pass
    def _reset_webview(self, manual: bool = False) -> None:
        """换掉整个 Web 视图（ShellPage 兜底请求，或用户连按"重新加载"）。

        必须**异步**：ShellPage 的调用点就在那个页对象的定时器槽里，同步
        把它 deleteLater + 跑 DeferredDelete 等于在自己的调用栈上删自己。
        排到下一轮事件循环再换。
        """
        if self._reset_pending:
            return
        self._reset_pending = True
        QTimer.singleShot(0, self, lambda: self._reset_webview_now(manual))
    def _reset_webview_now(self, manual: bool = False) -> None:
        """销毁并重建 QWebEngineView / ShellPage，保留 profile 与登录态。

        适用场景（2026-10-24 用户现场）：页面 DOM 正常、JS 心跳正常，但
        Web 视图不再呈现任何帧——整窗透出下面垫着的加载页，原生 select
        弹层还残留着；reload 无效，用户把窗口关到托盘再打开才恢复。
        进程内等价手段就是换一个视图（= 新合成表面），再载回入口地址。
        """
        self._reset_pending = False
        if self.handle is None or self.view is None:
            return
        now = time.monotonic()
        self._surface_resets = [t for t in self._surface_resets
                                if now - t < SURFACE_RESET_WINDOW_S]
        # 用户主动连按"重新加载"不受自动重建的限流约束——那是明确意图，
        # 而且 rate limit 的本意只是防自动循环。
        if not manual and len(self._surface_resets) >= SURFACE_RESET_LIMIT:
            # 同一轮异常里别每 45 秒弹一次托盘气泡
            if now - getattr(self, "_reset_blocked_at", 0.0) > 600:
                self._reset_blocked_at = now
                host_log("页面重建：%d 秒内已重建 %d 次，停止自动重建"
                         % (SURFACE_RESET_WINDOW_S, SURFACE_RESET_LIMIT))
                self._page_notify("页面渲染持续异常，已暂停自动重建。请重启应用。")
            return
        self._surface_resets.append(now)
        host_log("页面重建：销毁并重建 Web 视图（保留 profile 与登录态）")
        old = self.view
        self.view = None
        self.page = None
        # 这两个窗口缓存着旧 page，不关掉它们会握着已删对象
        if getattr(self, "_devwin", None) is not None:
            self._devwin.close()
            self._devwin.deleteLater()
            self._devwin = None
        if getattr(self, "_inspector", None) is not None:
            self._inspector.close()
            self._inspector = None
        if old is not None:
            try:
                self.stack.removeWidget(old)
                old.deleteLater()
            except RuntimeError:
                pass
        # 让旧视图/旧页面的延迟删除真正执行，别让旧合成表面继续挂着
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        try:
            self._ensure_webview()
            if self.handle is not None and self.view is not None:
                self.view.loadFinished.connect(
                    self._on_view_loaded,
                    Qt.ConnectionType.UniqueConnection)
                self.view.load(QUrl(self.handle.url))
        except Exception as e:                                 # noqa: BLE001
            host_log("页面重建失败：%s: %s" % (type(e).__name__, e))
            self._page_notify("页面重建失败：%s" % e)
    def _drop_webview(self) -> None:
        """释放 WebEngine 实例。

        删/搬数据目录前**必须**先做这一步：Windows 上 WebEngine 进程会锁住
        profile 目录里的缓存文件，不释放就删不掉、也复制不完整。

        两个调试窗口也一起关掉（2026-10-20 体检）：`DevWindow` 与
        `InspectorWindow` 都缓存着 `self.page`，视图没了它们就成了"握着已删对象"
        的窗口——`_open_dev_window` 又只在 `_devwin is None` 时创建，于是
        迁一次数据之后轻量日志窗口就永久坏了（点布局/环境页直接 RuntimeError）。
        """
        try:
            if getattr(self, "_devwin", None) is not None:
                self._devwin.close()
                self._devwin.deleteLater()
                self._devwin = None
            if getattr(self, "_inspector", None) is not None:
                self._inspector.close()      # WA_DeleteOnClose，会自己销毁
                self._inspector = None
            if self.view is not None:
                self.stack.removeWidget(self.view)
                self.view.deleteLater()
                self.view = None
                self.page = None
            if self.profile is not None:
                # 下载处理器挂在 profile 上，profile 释放后它也没有意义了
                self.downloads = None
                self.profile.deleteLater()
                self.profile = None
        except Exception:                                      # noqa: BLE001
            pass
        # `deleteLater()` 是异步的：`processEvents()` 在本地循环里**不处理**
        # DeferredDelete，所以光靠它并不保证 profile 已经释放、目录句柄已经关掉。
        # 显式把排队中的删除事件跑掉，后面的 rmtree/copytree 才不会撞上"文件被占用"。
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    def _reload_page(self):
        """把主视图重新加载回 DSH 首页。

        刻意**不用** `view.reload()`：页面已经跑偏时（例如被 `window.open`
        的 about:blank 顶掉），reload 只会把空白页再加载一遍。这里始终回到
        handle 里那个带 token 的地址。

        2026-10-20：登录流程把主窗口换成空白页，当时应用里没有任何刷新入口，
        只能重启。这个入口就是为那次事故加的。
        """
        if not self.handle:
            self._info("服务还没就绪，稍等一下再试。")
            return
        try:
            now = time.monotonic()
            self._manual_reloads = [t for t in self._manual_reloads
                                    if now - t < MANUAL_RESET_WINDOW_S]
            if self._manual_reloads:
                # 第二次按：软重载试过了还是不行，直接换视图（一定有效）
                host_log("手动重新加载：%d 秒内再次触发，改为重建 Web 视图"
                         % MANUAL_RESET_WINDOW_S)
                self._reset_webview(manual=True)
                return
            self._manual_reloads.append(now)
            self._ensure_webview()
            self.view.load(QUrl(self.handle.url))
            self._show_webpage()
            host_log("手动重新加载页面：%s"
                     % str(self.handle.url).split("token=")[0])
            # 2026-10-24：用户点"重新加载"往往就是因为页面已经白了。原生弹层
            # 造成的停帧 update() 一次就恢复（实测），先轻后重。
            try:
                self.view.update()
                QTimer.singleShot(300, self.view, self.view.repaint)
                QTimer.singleShot(1200, self.view, self.view.update)
            except RuntimeError:
                pass
        except Exception as e:                                 # noqa: BLE001
            host_log("重新加载失败：%s: %s" % (type(e).__name__, e))
    def _open_external_url(self, url):
        """把页面里的异源链接交给系统默认浏览器。

        为什么不自己开一个 QWebEngineView：那等于为一次点击再加 100~300MB
        的渲染进程；系统浏览器本来就是为看网页存在的，还带用户自己的
        登录态与扩展。老大明确要求优先走系统浏览器。

        链接里的令牌一律打码后再进日志——日志会长期留在磁盘上。
        """
        qurl = url if isinstance(url, QUrl) else QUrl(str(url or ""))
        shown = dsh_devtools.redact(qurl.toString())
        if not QDesktopServices.openUrl(qurl):
            host_log("系统浏览器未能打开：%s" % shown)
            self._warn("链接没能交给系统浏览器：\n%s" % shown)
        else:
            host_log("外链交给系统浏览器：%s" % shown)
    def _ask_download_path(self, suggested: str) -> str:
        """网页要下载文件时问一句存哪。返回空串 = 用户取消。

        为什么弹框而不是直接丢进"下载"文件夹：这是用户主动点的动作，
        官方桌面端（Electron）的默认行为也是弹保存框；而且 DSH 导出的
        session 包可能几十 MB，事后让人去翻文件夹找它不合适。
        """
        start_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation)
        if not start_dir:
            start_dir = os.path.expanduser("~")
        path, _ = QFileDialog.getSaveFileName(
            self, "保存下载文件", os.path.join(start_dir, suggested))
        return path or ""
    def _download_notify(self, msg: str) -> None:
        """下载的开始 / 完成 / 失败各给一句话（托盘气泡，不打断操作）。"""
        host_log("下载：%s" % msg)
        tray = getattr(self, "tray", None)
        if tray is not None and tray.isVisible():
            try:
                tray.showMessage("DSH 下载", msg,
                                 QSystemTrayIcon.MessageIcon.Information, 4000)
            except Exception:                                   # noqa: BLE001
                pass
