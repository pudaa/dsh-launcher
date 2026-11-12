"""WebEngine 页面子类、弹层补帧守卫、外链与剪贴板策略。"""
from __future__ import annotations
from .recorder import *          # noqa: F401,F403

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


import dsh_console
from dsh_host import config, procmem
from dsh_host.logutil import host_log
# 字体与调色板是面板那套设计令牌，开发者窗口沿用同一个来源，
# 免得两个窗口的等宽字体各写各的。
from dsh_panel import FONT, palette


WATCHDOG_INTERVAL_MS = 15000

WATCHDOG_BLANK_STRIKES = 3

RENDER_BUSY_MS = 25000

RELOAD_BURST = 3

WATCHDOG_JS = "document.body ? document.body.childElementCount : -1"

EXTERNAL_SCHEMES = ("mailto", "tel")


def nav_disposition(url, base) -> str:
    """判断一次导航该落在哪：`internal` / `external` / `ignore`。

    **同源才算内部**，判据是 (scheme∈http(s), host, port) 三元组；不同端口
    也算外部（DSH 自己的端口是随机的，不能靠"反正是本机"猜）。
    基址未知时（理论上只在服务就绪前）一律按内部处理——宁可不开外链，
    也不要把自家页面甩到浏览器里。
    """
    u = url if isinstance(url, QUrl) else QUrl(str(url or ""))
    if not u.isValid() or u.isEmpty():
        return "ignore"
    scheme = u.scheme().lower()
    if scheme in ("http", "https"):
        b = base if isinstance(base, QUrl) else QUrl(str(base or ""))
        if not b.isValid() or b.isEmpty() or not b.host():
            return "internal"
        if u.host().lower() != b.host().lower():
            return "external"
        if u.port(-1) != b.port(-1):
            return "external"
        return "internal"
    if scheme in EXTERNAL_SCHEMES:
        return "external"
    # javascript: / data: / blob: / about: —— 都是页面内部行为
    return "internal"

CLIPBOARD_FEATURES = frozenset(
    f for f in (getattr(QWebEnginePage.Feature, name, None)
                for name in ("ClipboardReadWrite", "ClipboardSanitizedWrite"))
    if f is not None)

def clipboard_origin_allowed(origin, base) -> bool:
    """剪贴板权限只给我方页面：**严格同源**（scheme + host + port）。

    为什么不能只看"是本机"：DSH 用随机端口起本地服务，端口每次启动都可能不同，
    所以这里逐项比；比字符串更可靠（末尾斜杠、大小写、默认端口都不会误判）。
    基址未知时（服务还没起来）只放行 loopback——那时页面上不可能有别人的内容。
    """
    o = origin if isinstance(origin, QUrl) else QUrl(str(origin or ""))
    if not o.isValid() or not o.host():
        return False
    b = base if isinstance(base, QUrl) else QUrl(str(base or ""))
    if not b.isValid() or b.isEmpty() or not b.host():
        return o.host().lower() in ("127.0.0.1", "localhost", "::1")
    if o.scheme().lower() != b.scheme().lower():
        return False
    if o.host().lower() != b.host().lower():
        return False
    return o.port(-1) == b.port(-1)

class ShellPage(QWebEnginePage):
    """主视图使用的页面：接管外链出口 + 采集控制台消息 + 放行剪贴板权限。

    只拦 **LinkClicked**。表单提交、重定向、刷新、前进后退一律放行——
    早期若不加判据全拦，会把登录跳转之类一起打掉（那类"功能莫名其妙坏掉"
    极难排查）。

    权限只放行**我方页面**的剪贴板写入（见 `_on_feature_permission`），
    其它一律拒绝——Qt 默认就拒绝，但那样复制按钮会静默失效。
    """

    def __init__(self, profile, parent, base_url=None, recorder=None,
                 open_external=None, view=None, capture_console: bool = False,
                 notify=None, hard_recover=None):
        super().__init__(profile, parent)
        self._base_url = base_url or (lambda: "")
        self.recorder = recorder or Recorder()
        #: 控制台采集**默认关**：开发者模式没开时不该有任何后台记录。
        #: 每次 console 消息都会回调进 Python，虽然便宜，但不是零成本。
        self.capture_console = bool(capture_console)
        self._open_external = open_external
        #: 页面监护（白屏/崩溃）给用户的一句话通知（托盘气泡之类）。
        #: 缺省不通知，只写日志——自测里就是这个形态。
        self._notify_cb = notify
        #: Qt6 的 QWebEnginePage **不再有 `view()`**（那是 Qt5 的 API），
        #: 所以视图引用必须由调用方显式传进来。
        self._view = view if view is not None else (
            parent if hasattr(parent, "setUrl") else None)
        self.newWindowRequested.connect(self._on_new_window)
        self.featurePermissionRequested.connect(self._on_feature_permission)
        # legacy 路径（`document.execCommand('copy')`）也放开写权限。
        # DSH 前端的复制助手**不会**回退到它（失败就直接 return false），
        # 但别的调用方可能用；实测默认返回 False、开启后能真正写进系统剪贴板。
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)

        # ---- 页面存活监护（2026-10-21 用户报"长任务后页面变空白"）----
        # 见 _on_render_terminated / _watchdog_tick 的说明：渲染进程被杀后
        # QtWebEngine 不会自己恢复（浏览器里那种"崩溃了"页面这里就是一片空白），
        # 主窗口之前也没有任何监听——于是一旦发生就永久空白，只能手动刷新。
        self._loaded_ok = False
        self._escalated = False                     # 已请求宿主重建（只提一次）
        self._reload_requests: deque = deque()      # 近期恢复请求时间戳（防循环）
        self._hb_pending_since: float | None = None  # 心跳在途（None=空闲）
        self._hb_blank = 0                          # 连续"DOM 为空"的次数
        #: 宿主注入的"重建 Web 视图"回调（界面层才能换视图；这里只管升级）。
        self._hard_recover_cb = hard_recover
        self.loadFinished.connect(self._on_load_ok)
        self.renderProcessTerminated.connect(self._on_render_terminated)
        # 心跳看门狗只挂在**主页面**上（popup 页 view=None，不值得也没有视图可判）
        if self._view is not None:
            self._watchdog = QTimer(self)
            self._watchdog.setInterval(WATCHDOG_INTERVAL_MS)
            self._watchdog.timeout.connect(self._watchdog_tick)
            self._watchdog.start()
        else:
            self._watchdog = None

    # ---- 页面存活监护 ----

    def _shell_log(self, msg: str) -> None:
        """写桌面壳日志。dsh_gui_qt 不能反向 import（会成环），所以就地写。"""
        try:
            with open(config.host_log(), "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except OSError:
            pass

    def _notify(self, msg: str) -> None:
        if self._notify_cb is not None:
            try:
                self._notify_cb(msg)
            except Exception:                                  # noqa: BLE001
                pass

    def _on_load_ok(self, ok: bool) -> None:
        try:
            self._loaded_ok = bool(ok)
        except RuntimeError:
            return
        self._hb_pending_since = None
        self._hb_blank = 0
        self._escalated = False

    def _may_recover(self, why: str) -> bool:
        """统一的恢复闸门：120 秒内最多 RELOAD_BURST 次，防"恢复→崩→恢复"循环。"""
        now = time.monotonic()
        while self._reload_requests and now - self._reload_requests[0] > 120:
            self._reload_requests.popleft()
        if len(self._reload_requests) >= RELOAD_BURST:
            self._shell_log("页面监护：%s，但近期恢复次数过多，停止自动恢复" % why)
            return False
        self._reload_requests.append(now)
        return True

    def _on_render_terminated(self, status, code) -> None:
        """渲染进程退出：先记证据，再尝试自动恢复。

        status/code 必须落日志——这是区分"崩溃(=代码缺陷)"与"被杀(=内存不足)"
        的唯一线索，缺了它下次还得靠猜。
        """
        name = getattr(status, "name", str(status))
        try:
            url = self.url().toString()
        except RuntimeError:
            url = ""
        # 崩溃当下的内存快照是"是被杀(OOM)还是自己崩"的唯一现场证据：
        # 渲染进程已经退出时拿不到它自己的数，但同一棵树里的其它进程还在。
        try:
            mem = procmem.summary_line()
        except Exception:                                      # noqa: BLE001
            mem = ""
        self._shell_log("渲染进程退出：status=%s code=%s url=%s%s"
                        % (name, code, url, (" ｜ " + mem) if mem else ""))
        if not self._loaded_ok:
            return          # 还没成功加载过：那是启动流程的事，这里不抢
        if not self._may_recover("渲染进程退出"):
            self._notify("渲染进程反复退出，已停止自动刷新；正在重建视图…")
            self._escalate_to_host("渲染进程反复退出")
            return
        # 留 500 ms 让 Qt 完成渲染进程的清理，再原地重载（保留当前路由与令牌）
        QTimer.singleShot(500, self, self._recover_page)

    def _watchdog_tick(self) -> None:
        """心跳看门狗：活着但空白 / 彻底无响应，都自动重载。

        为什么必须有它：用户报的问题是"长任务跑着跑着页面变空白"，而空白有两种
        形态——渲染进程被杀（renderProcessTerminated 能接到）和**进程还活着、
        页面却是空的**（前端把根节点卸载了 / 渲染进程主循环卡死，Qt 什么信号都
        不发）。后者只能主动探。

        成本：每 WATCHDOG_INTERVAL_MS 一次 JS 往返，只在窗口可见时发，
        空闲时是一次空转的查表。误判靠"连续 N 次"吸收：长任务期间渲染进程
        只是忙，JS 往返会慢但不会连续 3 轮（≈45 秒）都慢。
        """
        now = time.monotonic()
        if self._hb_pending_since is not None:
            # 上一次心跳还没回来：给 RENDER_BUSY_MS 宽限，超时按"无响应"处理
            if now - self._hb_pending_since > RENDER_BUSY_MS:
                self._hb_pending_since = None
                self._hb_strike("JS 无响应")
            return
        if not self._loaded_ok:
            return
        view = self._view
        try:
            if view is None or not view.isVisible() or view.isMinimized():
                return          # 隐藏/最小化时 Chromium 会节流，探测会误判
        except RuntimeError:
            return
        self._hb_pending_since = now
        try:
            self.runJavaScript(WATCHDOG_JS, self._hb_done)
        except RuntimeError:
            self._hb_pending_since = None

    # ---- 恢复动作 ----

    def _reload(self) -> bool:
        """可用的 reload。

        **PySide6 6.11 的 QWebEnginePage 没有 reload()**（那是 Qt5 的 API）。
        v2.4.1 的自动恢复直接写 self.reload()，异常被 Qt 吞进定时器槽里
        （打包版没有控制台，连 traceback 都看不到），于是「白屏自愈」
        从来没真正执行过。这里显式走视图的 reload，再回退到 triggerAction。
        """
        view = self._view
        if view is not None:
            try:
                view.reload()
                return True
            except RuntimeError:
                pass
        try:
            self.triggerAction(QWebEnginePage.WebAction.Reload)
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def _recover_page(self, force_base: bool = False) -> None:
        """恢复页面：优先 reload，必要时回到带令牌的入口地址。

        为什么不能无脑 reload（2026-10-20 事故的教训）：如果页面已经跑偏成
        about:blank（登录流程里 window.open 的产物），reload 只会把空白页
        再载一遍——用户看到的就是「刷新了还是白的」。
        """
        if not force_base:
            try:
                u = self.url()
            except RuntimeError:
                u = QUrl()          # 页对象正在销毁
            if u.isValid() and u.scheme() in ("http", "https"):
                if self._reload():
                    return
        base = ""
        try:
            base = self._base()
        except Exception:                                      # noqa: BLE001
            base = ""
        if base:
            self.setUrl(QUrl(base))
            return
        self._reload()

    def _escalate_to_host(self, why: str) -> None:
        """自动恢复手段用尽：请宿主重建 Web 视图（换掉合成表面）。

        2026-10-24 实测：QtWebEngine 6.11 打开原生 <select> 弹层后，视图会
        停止呈现（DOM/JS 全正常，reload 无效），只有换一个 QWebEngineView
        才恢复。正常路径已被 PopupRepaintGuard + 停掉加载页动画挡住；这里是
        兜底：反复恢复失败时升级成重建视图。宿主自己做限流（120s/2 次）。
        """
        cb = self._hard_recover_cb
        if cb is None or self._escalated:
            return
        self._escalated = True
        self._notify("页面反复无法恢复，正在重建 Web 视图…")
        try:
            cb()
        except Exception as e:                                 # noqa: BLE001
            self._shell_log("页面监护：重建视图失败 %s: %s"
                            % (type(e).__name__, e))


    def _hb_done(self, value) -> None:
        self._hb_pending_since = None
        try:
            n = int(value)          # JS 里取的是 document.body 的子元素数
        except (TypeError, ValueError):
            n = -1                  # null / 异常 → 当作异常状态，计入连续计数
        if n > 0:
            self._hb_blank = 0      # 页面有内容：一切正常
            return
        self._hb_blank += 1
        self._shell_log("页面监护：DOM 为空（连续 %d 次）" % self._hb_blank)
        if self._hb_blank >= WATCHDOG_BLANK_STRIKES:
            self._hb_blank = 0
            if self._may_recover("页面空白"):
                self._notify("页面变为空白，已自动重新加载。")
                # DOM 都空了，当前地址常常已经不可信 → 直接回入口地址
                self._recover_page(force_base=True)
            else:
                self._escalate_to_host("页面空白反复恢复失败")

    def _hb_strike(self, why: str) -> None:
        self._shell_log("页面监护：%s，准备自动重载" % why)
        if self._may_recover(why):
            self._notify("页面无响应，已自动重新加载。")
            self._recover_page(force_base=True)
        else:
            self._escalate_to_host("页面无响应反复恢复失败")

    # ---- 导航策略 ----

    def _base(self) -> str:
        try:
            return self._base_url() or ""
        except Exception:                                      # noqa: BLE001
            return ""

    def _deliver_external(self, url) -> None:
        if self._open_external is not None:
            try:
                self._open_external(url)
            except Exception:                                  # noqa: BLE001
                pass

    def acceptNavigationRequest(self, url, nav_type, is_main_frame) -> bool:  # noqa: N802
        try:
            if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                where = nav_disposition(url, self._base())
                if where == "external":
                    self._deliver_external(url)
                    return False
                if where == "ignore":
                    return False
            elif is_main_frame:
                # 脚本发起的主框架导航（window.location / 重定向）：
                # 与官方桌面端一致——外部站点交给系统浏览器。
                # 官方 apps/desktop 的 will-navigate 就是 preventDefault +
                # shell.openExternal。放行的后果是主窗口被带离 DSH。
                # u / scheme 要在这里重新取：LinkClicked 分支里那两个变量
                # 是本分支的外层，直接引用会 NameError，而 except 会把它
                # 吞掉、表现为"什么都没拦"（2026-10-20 回归测试抓到）。
                u = url if isinstance(url, QUrl) else QUrl(str(url or ""))
                scheme = u.scheme().lower()
                where = nav_disposition(url, self._base())
                if where == "external":
                    self._deliver_external(url)
                    return False
                # 主框架去 about:blank 同理：那是 window.open 的占位地址，
                # 主窗口从来不该去那里（2026-10-20 白屏事故的原始形态）。
                if scheme == "about" and not u.host():
                    return False
        except Exception:                                      # noqa: BLE001
            pass
        return True

    def _on_new_window(self, request) -> None:
        """处理 `target="_blank"` / `window.open()`。

        Qt 默认让这类请求**无声消失**（createWindow 返回 None），这正是
        "点了没反应"的直接来源。这里一律拒绝内核开新窗口：
          - 异源 → 交系统浏览器
          - 同源 → 用当前视图打开（同源意味着登录态还在；不为它再起一个
            WebEngine 实例，那是 100MB 起的开销）

        ⚠️ **`QWebEngineNewWindowRequest` 没有 `accept()` / `reject()`。**
        唯一的出口是 `openIn(WebWindowType)`（交给 Qt 自己开窗）；
        **不调用任何方法就等于拒绝**。2026-10-19 误调了 `request.reject()`，
        异常在"交给浏览器"之前抛出，表现正是"点了没反应"——
        所以这里除了读 URL，不碰请求对象。
        """
        try:
            url = request.requestedUrl()
        except Exception:                                      # noqa: BLE001
            return
        where = nav_disposition(url, self._base())
        scheme = url.scheme().lower()
        # `about:blank` 这类是 `window.open()` 的**占位窗口**——先开一个空窗口
        # 避开弹窗拦截，拿到句柄后再把它导航到目标页（DSH 的登录流程就是这样，
        # 见 ui-settings-account/client.js:999）。
        #
        # 绝不能拿主视图去接（2026-10-20 事故）：`nav_disposition()` 把 about:
        # 归为"页面内部行为"，于是 `view.setUrl(about:blank)` 把整个应用换成
        # 空白页——DOM 只剩 <html><head></head><body></body></html>，没有地址栏、
        # 没有返回入口，用户只能重启。
        if scheme in ("about", "javascript", "data", "blob"):
            # 交给 Qt 开窗（2026-10-20，第三次才做对）。
            #
            # 这里的坑值得写清楚，因为连错了两次：
            # ① `view.setUrl(about:blank)` —— 主窗口被换成空白页；
            # ② 自己 new 一个 QMainWindow —— Chromium 不知道它，
            #    `window.open()` 返回 null，DSH 后续的
            #    `popup.location.replace(authorizeUrl)` 永不执行，
            #    于是留下一个永远空白的窗口，登录也卡死；
            # ③ 正解：`request.openIn(...)` 让 Qt 接管，
            #    并重写 `createWindow` 返回一个页面（Qt 的默认实现返回
            #    nullptr，那样窗口根本不会创建）。这样 Chromium 会拿到
            #    有效的 window 对象，DSH 的登录流程才能走下去。
            #
            # 新页面随后被导航到外部授权页时，由它自己的
            # `acceptNavigationRequest` 转交系统浏览器并阻止渲染——
            # 效果等同官方桌面端的 openExternal + deny。
            try:
                request.openIn(
                    QWebEnginePage.WebWindowType.WebBrowserTab)
            except Exception:                                  # noqa: BLE001
                pass
            return
        if where == "external":
            self._deliver_external(url)
        elif where == "internal":
            view = self._view
            if view is not None:
                view.setUrl(url)

    def createWindow(self, window_type):                        # noqa: N802
        """为 `window.open` 提供新页面。

        **Qt 的默认实现返回 nullptr**，那样窗口根本不会创建，页面里的
        `window.open()` 只能拿到 null。DSH 的登录流程恰恰依赖那个对象：
        `ui-settings-account/client.js` 的 `begin()` 先占位，
        `update()` 里 `popup.location.replace(authorizeUrl)` 才把它导航到授权页。
        返回 null 等于把登录卡死（2026-10-20 实测复现）。

        新页面共用同一份 ShellPage 逻辑：一旦被导航到外部站点，
        `acceptNavigationRequest` 会转交系统浏览器并返回 False——
        用户看到授权页在自己的默认浏览器里打开，与官方桌面端行为一致。
        """
        page = ShellPage(self.profile(), self, base_url=self._base,
                         recorder=self.recorder,
                         open_external=self._open_external,
                         view=None, capture_console=self.capture_console,
                         notify=self._notify_cb)
        page._is_popup = True
        return page

    # ---- 页面权限 ----

    def _on_feature_permission(self, origin, feature) -> None:
        """页面申请权限：只给剪贴板，其余明确拒绝。

        为什么必须自己处理（2026-10-20 用户报"点复制没反应"）
        --------------------------------------------------
        `navigator.clipboard.writeText()` 在 QtWebEngine 里**受权限管**，而 Qt 的
        默认行为是拒绝、且不弹任何提示：Promise 直接以
        `NotAllowedError: ... Write permission denied.` 结束。
        DSH 前端的复制助手（bundle 里的 `async function zo(e)`）拿到异常后
        **直接 return false，不回退 execCommand**，于是"点了复制、没有报错、
        剪贴板也没变"——正是用户看到的现象。

        实测（真窗口，点页面上的复制按钮）：
          - 不授权：clicks=1，权限请求 ClipboardReadWrite @ http://127.0.0.1:9/，
            结果 `err:NotAllowedError:Write permission denied`，系统剪贴板不变；
          - 授权后同一次点击：`__r='ok'`，剪贴板立刻变成新内容。
        """
        try:
            allow = (feature in CLIPBOARD_FEATURES
                     and clipboard_origin_allowed(origin, self._base()))
            self.setFeaturePermission(
                origin, feature,
                QWebEnginePage.PermissionPolicy.PermissionGrantedByUser
                if allow
                else QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)
            if not allow:
                # 拒绝要留痕：这类"页面点了没反应"事后唯一能查的就是日志
                self._shell_log("权限被拒绝：%s @ %s"
                                % (getattr(feature, "name", feature),
                                   redact(origin.toString())))
        except Exception:                                      # noqa: BLE001
            pass

    # ---- 控制台 ----

    def set_capture_console(self, on: bool) -> None:
        self.capture_console = bool(on)

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        if not self.capture_console:
            return
        try:
            self.recorder.add_console(LEVEL_NAMES.get(level, "log"), message,
                                      line, source)
        except Exception:                                      # noqa: BLE001
            pass

class PopupRepaintGuard(QObject):
    """原生弹层出现/消失时，给 Web 视图补一帧。

    2026-10-24 实测（tools/probe_popup_*.py，PySide6 6.11.2 / Windows）：
    点开一个原生 <select> 弹层后，QWebEngineView **停止呈现**——StackAll
    布局里能看到垫在下面的加载页透出来，而 JS/DOM 全部正常、reload 无效；
    对视图调用一次 update()（+repaint()）立刻恢复。

    所以这里监听**应用内所有顶层 Popup 窗口**的 Show/Hide/Close：弹层一出现
    就补一帧，关闭后再补一帧。对非 WebEngine 的弹层（托盘菜单等）调用
    view.update() 只是一次廉价重绘，没有副作用。

    只做这件事、不承担恢复职责：真正的兜底是 ShellPage 的像素级监护。
    """

    def __init__(self, view_getter, parent=None):
        super().__init__(parent)
        self._view_getter = view_getter
        self._last = 0.0
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj, ev):                       # noqa: N802
        t = ev.type()
        if t in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Close,
                 QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate):
            try:
                if not obj.isWindow():
                    return False
                # 注意：Qt.WindowType 不是位标志！Popup 的值本身含 Window 位
                # （Popup = 0x9），所以 windowType() & Popup 对**所有顶层窗口**
                # 都为真。必须用 WindowType_Mask 先取窗口类别再比较。
                kind = obj.windowType() & Qt.WindowType.WindowType_Mask
                if kind != Qt.WindowType.Popup:
                    return False
            except (RuntimeError, AttributeError):
                return False
            # 实测（delay=0，probe_popup_run.py --scenario popup-hook）：
            # 在 Show 的同一拍补一帧就足以让「停帧」根本不发生；再补两拍
            # 吸收弹层尺寸确定、关闭后的重排。
            self._schedule(0)
            self._schedule(60)
            self._schedule(260)
        return False

    def _schedule(self, delay_ms: int) -> None:
        QTimer.singleShot(delay_ms, self, self.nudge)

    def nudge(self) -> None:
        """让主视图重画一帧。调用方也可以直接调（自测 / 像素监护）。"""
        now = time.monotonic()
        if now - self._last < 0.04:          # 合并同一轮里的多次事件
            return
        self._last = now
        view = None
        try:
            view = self._view_getter()
        except Exception:                                  # noqa: BLE001
            view = None
        if view is None:
            return
        try:
            view.update()
        except RuntimeError:
            pass

JS_LAYOUT = r"""
(function () {
  var out = [];
  var skip = {SCRIPT: 1, STYLE: 1, LINK: 1, META: 1, HEAD: 1, NOSCRIPT: 1};
  function path(el) {
    var parts = [];
    var n = el;
    while (n && n.nodeType === 1 && n !== document.documentElement) {
      var s = n.tagName.toLowerCase();
      if (n.id) { parts.unshift(s + '#' + (window.CSS && CSS.escape ? CSS.escape(n.id) : n.id)); break; }
      var p = n.parentElement;
      if (p) {
        var idx = Array.prototype.indexOf.call(p.children, n) + 1;
        s += ':nth-child(' + idx + ')';
      }
      parts.unshift(s);
      n = n.parentElement;
    }
    return parts.join(' > ');
  }
  function walk(el, depth) {
    if (!el || out.length >= 1200 || depth > 24) return;
    if (skip[el.tagName]) return;
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    var hidden = cs.display === 'none' || cs.visibility === 'hidden';
    if (!hidden && (r.width > 0 || r.height > 0)) {
      var cls = (typeof el.className === 'string' ? el.className : '')
                  .split(/\s+/).filter(Boolean).slice(0, 3).join('.');
      out.push({sel: path(el), tag: el.tagName.toLowerCase(), id: el.id || '',
                cls: cls, x: Math.round(r.left), y: Math.round(r.top),
                w: Math.round(r.width), h: Math.round(r.height),
                display: cs.display, pos: cs.position,
                of: cs.overflow, depth: depth});
    }
    var kids = el.children;
    for (var i = 0; i < kids.length; i++) walk(kids[i], depth + 1);
  }
  walk(document.body, 0);
  // 返回值一律 JSON.stringify：PySide6 的 runJavaScript 对 array/object
  // 的自动转换不可靠（实测 6.11 直接给空串），字符串通道最稳。
  return JSON.stringify(out);
})()
"""

JS_HILITE = r"""
(function (sel) {
  var st = window.__dshDevHi;
  if (!st) { st = window.__dshDevHi = {el: null, outline: '', offset: ''}; }
  if (st.el) {
    try { st.el.style.outline = st.outline; st.el.style.outlineOffset = st.offset; } catch (e) {}
  }
  st.el = null;
  var el = null;
  try { el = document.querySelector(sel); } catch (e) { return 'bad-selector'; }
  if (!el) return 'not-found';
  st.el = el;
  st.outline = el.style.outline || '';
  st.offset = el.style.outlineOffset || '';
  el.style.outline = '2px solid #ff3b30';
  el.style.outlineOffset = '-2px';
  try { el.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (e) {}
  return 'ok';
})
"""

JS_PERF = r"""
(function () {
  var out = [];
  try {
    var nav = performance.getEntriesByType('navigation')[0];
    if (nav) out.push({name: nav.name, type: 'document',
                       size: nav.transferSize || nav.encodedBodySize || 0,
                       dur: Math.round(nav.duration)});
    var list = performance.getEntriesByType('resource');
    for (var i = 0; i < list.length; i++) {
      var e = list[i];
      out.push({name: e.name, type: e.initiatorType || 'resource',
                size: e.transferSize || 0, dur: Math.round(e.duration)});
    }
  } catch (e) {}
  return JSON.stringify(out);
})()
"""

JS_ENV = r"""
(function () {
  var de = document.documentElement;
  return JSON.stringify({
    url: location.href,
    title: document.title || '',
    vw: window.innerWidth, vh: window.innerHeight,
    dpr: window.devicePixelRatio || 1,
    ready: document.readyState,
    nodes: de ? de.getElementsByTagName('*').length : 0,
    ua: navigator.userAgent
  });
})()
"""
