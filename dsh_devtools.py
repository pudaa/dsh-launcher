# -*- coding: utf-8 -*-
"""开发者模式：外链出口策略 + 页面观测记录 + 原生开发者窗口。

要解决的两件事
--------------
1. **外链点了没反应。** DSH 界面里的说明与账号类链接常常是 `target="_blank"`
   或直接指向外部站点。QtWebEngine 对"新窗口请求"默认是**没人接**的：
   `QWebEngineView.createWindow()` 返回 `None`，于是页面什么都不发生；
   同窗口内的外链则会把 DSH 界面自身导航走。两种表现都不该出现。
   这里的策略是：**同源留在内核里，异源一律交系统浏览器**——顺带把渲染
   进程的内存还给系统（老大明确要求优先用系统浏览器）。
2. **出问题时要有证据链。** 原先只能靠经验判断；现在给一个可查的窗口：
   控制台报错、网络请求、页面布局（含每个元素的坐标尺寸）。

设计取舍
--------
- **记录常开、窗口按需**：控制台消息很便宜（一条一句），网络拦截要跨语言
  回调（每个请求一次），只在开发者模式开启时挂上，不给普通用户加开销。
- **窗口是原生 QWidget**，配色复用 `dsh_panel` 的官方令牌。理由与控制面板
  相同：多一个 WebEngine 实例就多 100~300MB，而"看日志"不值得这个价。
- 真正需要 Elements 断点/存储/性能剖析时，再按需开**内核开发者工具**：
  那是第二个 WebEngine 实例，所以只在用户点的时候创建，按钮旁写着代价。
- 本模块不引用 `MainWindow`：外链怎么开、窗口挂哪里，全由调用方以回调注入。
  这样它可以在自测里脱离主窗口单独构造。
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

import dsh_console
from dsh_host import config, procmem
from dsh_host.version import HOST_VERSION
from dsh_panel import FONT, palette

MAX_CONSOLE = 2000
MAX_NET = 2000

#: 心跳看门狗的探测间隔（毫秒）。只在主窗口可见时发送。
WATCHDOG_INTERVAL_MS = 15000
#: 连续几次"DOM 为空"才判定白屏（3 × 15s ≈ 45 s，吸收首屏渲染的短暂空窗）。
WATCHDOG_BLANK_STRIKES = 3
#: 一次心跳允许的最长往返（毫秒）。渲染进程只是忙时会慢，给足宽限。
RENDER_BUSY_MS = 25000
#: 120 秒内最多自动恢复几次（渲染进程退出 / 白屏 / 无响应共用这个计数）。
RELOAD_BURST = 3

#: 探测白屏用的 JS：返回 body 的子元素数。空页面 = 0，渲染进程死了不回调。
WATCHDOG_JS = "document.body ? document.body.childElementCount : -1"

#: 可以安全交给系统处理的非 http 协议。`javascript:` / `data:` / `blob:`
#: 必须留在内核里，否则页面里的按钮会直接失效。
EXTERNAL_SCHEMES = ("mailto", "tel")

LEVEL_NAMES = {
    QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel: "info",
    QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel: "warning",
    QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel: "error",
}

_TOKEN_RE = re.compile(r"(token=)[^&\s]+", re.I)


def redact(url: str) -> str:
    """把 URL 里的令牌打码。写日志和复制诊断信息时一律走它。

    令牌是这个服务的唯一身份凭据，进了日志就等于长期留在磁盘上。
    """
    return _TOKEN_RE.sub(r"\1***", str(url or ""))


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


#: 要放行的页面权限：只有剪贴板写入。
#: `ClipboardReadWrite` 是 `navigator.clipboard.writeText()` 要的那个；
#: `ClipboardSanitizedWrite` 是它的"只写、不可读"变体，**PySide6 6.11 还没暴露**
#: （实测 `QWebEnginePage.Feature` 里只有前者），所以按名字挑、有就一起给，
#: 免得换个 Qt 版本复制又坏掉。
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


def _resource_type(info) -> str:
    try:
        name = info.resourceType().name
    except Exception:                                          # noqa: BLE001
        return "other"
    return name.replace("ResourceType", "").lower() or "other"


def _size_text(n: int) -> str:
    if n is None or n < 0:
        return "—"
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


# ------------------------------------------------------------------ 记录

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


# ------------------------------------------------------------------ 弹层补帧

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


# ------------------------------------------------------------------ 注入脚本

#: 布局快照。给每个**可见**元素一条记录（含坐标与尺寸），够回答
#: "这个控件到底在哪、被谁挤走了"。上限 1200 条，避免超大页面把主线程拖住。
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

#: 高亮某个元素（选中布局表里的行时用）。返回 ok / not-found / bad-selector。
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

#: 页面已经加载过的资源（performance 条目）。补的是"开发者模式开启之前"
#: 那些请求——拦截器只能看到打开之后发生的。
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


# --------------------------------------------------------------- 浏览器下载
#
# 为什么必须有这一段（2026-10-21 老大报"点下载 session 没反应"）
# ------------------------------------------------------------------
# DSH 的导出按钮原文（`dsh-session-log-export/lib/client.js`）：
#
#     function downloadUrl(url, filename) {
#         const anchor = document.createElement("a");
#         anchor.href = url;
#         anchor.download = filename;
#         anchor.click();
#     }
#
# 在浏览器里 `download` 属性会把这次点击交给下载管理器。而在 QtWebEngine 里，
# 没有人接 `QWebEngineProfile.downloadRequested` 时，**请求被直接取消**：
# 不落盘、不报错、也不弹任何提示；而 `anchor.click()` 早就同步返回了，
# 前端以为"已经交给浏览器"，于是什么都不做。
#
# 这是同一个坑的第三种形态（前两种：剪贴板权限、window.open）。共同点：
# **Qt 的默认行为是拒绝，而且拒绝得一声不响。**

#: Windows 文件名里不合法、或会让资源管理器困惑的字符
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Windows 设备名，做文件名会直接失败（不论扩展名）
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)])


def browser_safe_filename(name, fallback: str = "download.bin") -> str:
    """把网页给的文件名洗成 Windows 能落盘的形状。

    网页给的名字不可信（可能带路径分隔符、控制字符，或者干脆是 `..`）。
    清洗顺序：去掉非法字符 → 去掉结尾的点和空格（Windows 会静默吞掉）→
    设备名加前缀 → 空名回退。
    """
    s = _BAD_FILENAME.sub("_", str(name or "")).strip()
    s = s.rstrip(". ")
    if not s or s in (".", ".."):
        return fallback
    stem, ext = os.path.splitext(s)
    if stem.upper() in _RESERVED_NAMES:
        s = "_" + s
    # 单段长度留足余量：260 字符的整路径限制比文件名限制更早撞上
    if len(s) > 180:
        s = s[:180 - len(ext)] + ext
    return s


def unique_download_path(directory: str, filename: str) -> str:
    """同名文件已存在时按 `名字 (1).ext` 递增，不静默覆盖。"""
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(filename)
    for i in range(1, 1000):
        cand = os.path.join(directory, "%s (%d)%s" % (stem, i, ext))
        if not os.path.exists(cand):
            return cand
    return os.path.join(directory, "%s (%d)%s" % (stem, os.getpid(), ext))


class DownloadManager(QObject):
    """把网页发起的下载接到桌面上。

    `choose_path(filename) -> str`：问用户存哪，返回完整路径；返回空串表示取消。
    **不注入时一律按取消处理**——没有界面就没人能回答"存哪"，
    宁可明确取消，也不要悄悄写到某个用户找不到的地方。
    `notify(text)`：一句话告知（开始 / 完成 / 失败），由界面层接到托盘气泡。
    """

    def __init__(self, profile, choose_path=None, notify=None, log=None,
                 parent=None):
        super().__init__(parent)
        self._choose = choose_path
        self._notify = notify
        self._log = log or (lambda _s: None)
        self._active: dict = {}
        self.attach(profile)

    def attach(self, profile) -> None:
        """接上 profile 的下载信号。profile 重建（换来源/迁移数据）后要重挂。"""
        try:
            profile.downloadRequested.connect(self._on_requested)
        except (AttributeError, RuntimeError, TypeError) as e:
            self._log("挂接下载处理器失败：%s: %s" % (type(e).__name__, e))

    # ---- 信号 ----

    def _on_requested(self, request) -> None:
        try:
            suggested = (request.downloadFileName()
                         or request.suggestedFileName() or "")
        except RuntimeError:
            return                                  # 请求已失效
        name = browser_safe_filename(suggested)

        path = ""
        if self._choose is not None:
            try:
                path = str(self._choose(name) or "")
            except Exception as e:                  # noqa: BLE001
                self._log("选择下载位置失败：%s: %s" % (type(e).__name__, e))
                path = ""
        if not path:
            # 用户取消：必须显式 cancel，否则请求会悬着（Qt 释放时算作失败）
            self._log("下载已取消：%s" % name)
            try:
                request.cancel()
            except RuntimeError:
                pass
            return

        folder, fname = os.path.split(path)
        fname = browser_safe_filename(fname or name)
        try:
            # 顺序有讲究：**先设目录与文件名，再 accept**。
            # accept 之后再设会无效（Qt 会打 warning 并忽略）。
            if folder:
                request.setDownloadDirectory(folder)
            request.setDownloadFileName(fname)
            # 完成/中断的信号要赶在 accept 之前接上：小文件可能瞬间就结束
            request.stateChanged.connect(
                lambda state, req=request: self._on_state(req, state))
            request.accept()
        except (RuntimeError, TypeError) as e:
            self._log("下载未能开始：%s: %s" % (type(e).__name__, e))
            return

        self._active[request] = {"path": path}
        self._log("开始下载：%s" % path)
        if self._notify is not None:
            self._notify("正在下载 %s" % fname)

    def _on_state(self, request, state) -> None:
        S = QWebEngineDownloadRequest.DownloadState
        if state == S.DownloadCompleted:
            info = self._active.pop(request, None)
            path = (info or {}).get("path") or ""
            self._log("下载完成：%s" % path)
            if self._notify is not None:
                self._notify("已保存到 %s" % path)
        elif state in (S.DownloadCancelled, S.DownloadInterrupted):
            self._active.pop(request, None)
            reason = ""
            try:
                reason = getattr(request.interruptReason(), "name", "") or ""
            except RuntimeError:
                pass
            self._log("下载未完成（%s）：%s"
                      % (getattr(state, "name", state), reason))
            if self._notify is not None:
                self._notify("下载未完成%s"
                             % (("：" + reason) if reason else "（已取消或中断）"))


# ------------------------------------------------------------------ 窗口

def _mono() -> str:
    return '"Cascadia Mono", "Consolas", "Courier New", monospace'


def _inspector_style(pal: dict) -> str:
    """DevTools 窗口顶部工具条的样式。只覆盖我们自己的那条 bar，
    里面的 DevTools 是内核页面，样式表管不到它。"""
    return ("""
QWidget#inspectorBar { background: %(side)s; border-bottom: 1px solid %(line)s; }
QLabel#inspectorTitle { color: %(text)s; font-family: %(font)s;
                        font-size: 12px; font-weight: 600; }
QLabel#inspectorTarget { color: %(muted)s; font-family: %(mono)s; font-size: 11px; }
QPushButton#segBtn { color: %(dim)s; background: transparent;
    border: 1px solid %(line)s; border-radius: 6px; padding: 2px 10px;
    font-family: %(font)s; font-size: 12px; }
QPushButton#segBtn:hover { color: %(text)s; background: %(hover)s; }
QPushButton#segBtn:checked { color: %(text)s; background: %(nav_active)s;
                             border-color: %(line)s; font-weight: 600; }
QPushButton#pinBtn { color: %(dim)s; background: transparent;
    border: 1px solid %(line)s; border-radius: 6px; padding: 2px 10px;
    font-family: %(font)s; font-size: 12px; }
QPushButton#pinBtn:hover { background: %(hover)s; color: %(text)s; }
QPushButton#pinBtn:checked { color: %(accent)s; border-color: %(accent)s;
                             background: %(hover)s; font-weight: 600; }
""" % {"side": pal["side"], "line": pal["line"], "text": pal["text"],
       "dim": pal["dim"], "muted": pal["muted"], "hover": pal["hover"],
       "accent": pal["accent"], "nav_active": pal["nav_active"],
       "font": FONT, "mono": _mono()})


def _style(pal: dict) -> str:
    return ("""
QWidget { background: %(bg)s; color: %(text)s;
          font-family: %(font)s; font-size: 12px; }
QLabel#devTitle { font-size: 15px; font-weight: 600; color: %(text)s; }
QLabel#devHint  { color: %(muted)s; }
QLabel#devUrl   { color: %(dim)s; }
QTabWidget::pane { border: 1px solid %(line)s; border-radius: 8px;
                   top: -1px; background: %(bg)s; }
QTabBar::tab { background: transparent; color: %(dim)s;
               padding: 6px 14px; margin-right: 2px; border: none; }
QTabBar::tab:selected { color: %(text)s; background: %(hover)s;
                        border-radius: 6px; font-weight: 600; }
QTabBar::tab:hover { color: %(text)s; }
QPlainTextEdit, QTableWidget { background: %(card)s; color: %(text)s;
    border: 1px solid %(line)s; border-radius: 8px;
    selection-background-color: %(nav_active)s; selection-color: %(text)s; }
QHeaderView::section { background: %(side)s; color: %(dim)s; border: none;
    border-bottom: 1px solid %(line)s; padding: 4px 8px; font-weight: 600; }
QTableWidget { gridline-color: %(line_soft)s; }
QPushButton { color: %(text)s; background: %(btn_fill)s;
    border: 1px solid %(line)s; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover { background: %(btn_hover)s; }
QPushButton:checked { color: %(accent)s; border-color: %(accent)s; }
QPushButton:disabled { color: %(muted)s; }
QLineEdit { background: %(card)s; color: %(text)s; border: 1px solid %(line)s;
    border-radius: 6px; padding: 4px 8px; }
""" % {
        "bg": pal["bg"], "text": pal["text"], "font": FONT, "muted": pal["muted"],
        "dim": pal["dim"], "line": pal["line"], "line_soft": pal["line_soft"],
        "card": pal["card"], "side": pal["side"], "hover": pal["hover"],
        "nav_active": pal["nav_active"], "btn_fill": pal["btn_fill"],
        "btn_hover": pal["btn_hover"], "accent": pal["accent"],
    })


def _terminal_style(pal: dict) -> str:
    """终端页的样式。日志区用等宽字体、不换行，与真正的终端一致。"""
    return """
QWidget#termBar { background: %(side)s; border-bottom: 1px solid %(line)s; }
QLabel#termStatus { color: %(muted)s; font-family: %(font)s; font-size: 11px; }
QPlainTextEdit#termLog {
    background: %(card)s; color: %(text)s; border: none;
    selection-background-color: %(nav_active)s; selection-color: %(text)s;
    font-family: %(mono)s; font-size: 12px; padding: 6px 8px;
}
QLineEdit#termSearch {
    background: %(card)s; color: %(text)s; border: 1px solid %(line)s;
    border-radius: 6px; padding: 3px 8px;
    font-family: %(font)s; font-size: 12px;
}
QPushButton#termBtn {
    color: %(dim)s; background: transparent; border: 1px solid transparent;
    border-radius: 6px; padding: 3px 9px;
    font-family: %(font)s; font-size: 12px;
}
QPushButton#termBtn:hover { background: %(hover)s; color: %(text)s; }
QPushButton#termBtn:checked { color: %(accent)s; background: %(hover)s; }
QPushButton#termBtn:disabled { color: %(line)s; }
QScrollBar:vertical, QScrollBar:horizontal {
    background: transparent; width: 9px; height: 9px; margin: 2px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: %(scroll)s; border-radius: 4px; min-height: 28px; min-width: 28px;
}
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
""" % {
        "side": pal["side"], "line": pal["line"], "muted": pal["muted"],
        "card": pal["card"], "text": pal["text"], "hover": pal["hover"],
        "nav_active": pal["nav_active"], "accent": pal["accent"],
        "scroll": pal["scroll"], "dim": pal["dim"], "font": FONT,
        "mono": '"Cascadia Mono", "Consolas", monospace',
    }


class _LogHighlighter(QSyntaxHighlighter):
    """按级别给日志着色。**只改颜色，不改文本**——复制出来的内容必须是原样。"""

    def __init__(self, document, pal: dict):
        super().__init__(document)
        self.set_palette(pal)

    def set_palette(self, pal: dict) -> None:
        def fmt(color: str, bold: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            return f

        ci = QRegularExpression.PatternOption.CaseInsensitiveOption
        self._rules = [
            (QRegularExpression(
                r"\[ERROR|\[FATAL|\bERROR\b|\bFATAL\b|\bCRITICAL\b|"
                r"Traceback|\bException\b|Segmentation fault", ci),
             fmt(pal["bad"], True)),
            (QRegularExpression(r"\[WARN|\[WARNING|\bWARN(ING)?\b", ci),
             fmt(pal["warn"])),
            (QRegularExpression(r"^(js:|\[ERROR:CONSOLE|CONSOLE\()"),
             fmt(pal["accent"])),
        ]
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:            # noqa: N802 (Qt 命名)
        for rx, form in self._rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), form)


class TerminalView(QWidget):
    """宿主进程的输出面板（stdout / stderr，含 Chromium 的 C++ 日志）。

    打包成 windowed exe 后没有控制台，这份输出去哪了？
      · Python 的 print → 进程没有 stdout；
      · Chromium 的 `js: ...`、`[ERROR:CONSOLE]` → 写向无效的 fd 2。
    两条都看不见，而这恰恰是定位问题的第一手证据。`dsh_console.install()`
    在启动最早期把 fd 1/2 接进环形缓冲，这里负责把它读出来、染上颜色、
    并且**保证可以整段复制**——复制出去直接粘给 agent 就能复现问题。
    """

    def __init__(self, pal: dict, console=None, parent=None):
        super().__init__(parent)
        self.pal = pal
        self.console = console or dsh_console.CONSOLE
        self._seq = 0
        self._pending = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._build_bar())

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("termLog")
        self.editor.setReadOnly(True)
        self.editor.setUndoRedoEnabled(False)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # 比采集缓冲略大：多出来的部分只是显示余量，真正的内容由环形缓冲决定
        self.editor.setMaximumBlockCount(dsh_console.MAX_LINES + 100)
        font = QFont("Consolas")
        font.setPointSize(9)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.editor.setFont(font)
        self.highlighter = _LogHighlighter(self.editor.document(), pal)
        lay.addWidget(self.editor, 1)

        # 用户主动滚上去就暂停跟随（用 actionTriggered，避免被程序滚动误触）
        self.editor.verticalScrollBar().actionTriggered.connect(self._on_user_scroll)

        self.setStyleSheet(_terminal_style(pal))

        # 定时器的 parent 是本控件：DevTools 窗口关闭即销毁，它随之消失，
        # 不会在后台留下一个每 300ms 醒一次的轮询。
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.tick)
        self._timer.start()
        self.tick()

    # ---- 顶部工具条 ----

    def _build_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("termBar")
        bar.setFixedHeight(34)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 0, 8, 0)
        row.setSpacing(4)

        self.btn_copy_all = self._button("复制全部", "复制全部输出（可直接粘给 agent）")
        self.btn_copy_all.clicked.connect(self._copy_all)
        self.btn_copy_sel = self._button("复制选中", "复制当前选中的内容")
        self.btn_copy_sel.clicked.connect(self._copy_selected)
        self.btn_clear = self._button("清空", "清空缓冲（不影响日志文件）")
        self.btn_clear.clicked.connect(self._clear)

        self.btn_follow = self._button("跟随", "新输出到达时自动滚到底部")
        self.btn_follow.setCheckable(True)
        self.btn_follow.setChecked(True)
        self.btn_follow.toggled.connect(self._on_follow)

        self.btn_bottom = self._button("回到底部", "跳到最新一行并恢复跟随")
        self.btn_bottom.clicked.connect(self.jump_to_end)
        self.btn_save = self._button("另存为…", "把当前缓冲导出为文本文件")
        self.btn_save.clicked.connect(self._save)

        self.search = QLineEdit()
        self.search.setObjectName("termSearch")
        self.search.setPlaceholderText("搜索（回车下一个，Shift+回车上一个）")
        self.search.setFixedWidth(240)
        self.search.returnPressed.connect(self._search)

        self.status = QLabel("0 行")
        self.status.setObjectName("termStatus")

        for b in (self.btn_copy_all, self.btn_copy_sel, self.btn_clear):
            row.addWidget(b)
        row.addSpacing(6)
        row.addWidget(self.btn_follow)
        row.addWidget(self.btn_bottom)
        row.addWidget(self.btn_save)
        row.addWidget(self.search)
        row.addStretch(1)
        row.addWidget(self.status)
        return bar

    def _button(self, text: str, tip: str) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("termBtn")
        b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFlat(True)
        return b

    # ---- 数据 ----

    def tick(self) -> None:
        entries = self.console.since(self._seq)
        if entries:
            self._seq = entries[-1].seq
            self._append(entries)
        self._refresh_status()

    def _append(self, entries) -> None:
        if len(entries) > 400:
            # 启动瞬间可能一次几百行：整段插入比逐行 append 快一个量级
            self.editor.setUpdatesEnabled(False)
            try:
                cursor = QTextCursor(self.editor.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText("\n".join(e.text for e in entries) + "\n")
            finally:
                self.editor.setUpdatesEnabled(True)
        else:
            for e in entries:
                self.editor.appendPlainText(e.text)
        if self.btn_follow.isChecked():
            self.jump_to_end(scroll_only=True)
        else:
            self._pending += len(entries)

    def _refresh_status(self) -> None:
        total, _seq = self.console.stats()
        if self.btn_follow.isChecked():
            self.status.setText("%d 行 · 跟随中" % total)
        else:
            self.status.setText("%d 行 · 已暂停%s"
                                % (total, ("（新 %d 条）" % self._pending)
                                   if self._pending else ""))
        self.pal_hint()

    def pal_hint(self) -> None:
        """错误计数放在状态文字里（不额外占位）：有错误时用错误色。"""
        total, _seq = self.console.stats()
        try:
            errors = sum(1 for e in self.console.since(0) if e.level == "error")
        except Exception:                                      # noqa: BLE001
            errors = 0
        if errors:
            self.status.setText(self.status.text() + " · 错误 %d" % errors)
            self.status.setStyleSheet("color:%s;" % self.pal["bad"])
        else:
            self.status.setStyleSheet("")

    def _on_follow(self, on: bool) -> None:
        if on:
            self._pending = 0
            self.jump_to_end()
        self._refresh_status()

    def _on_user_scroll(self, _action: int) -> None:
        if self.btn_follow.isChecked() and not self._at_bottom():
            self.btn_follow.setChecked(False)

    def _at_bottom(self) -> bool:
        bar = self.editor.verticalScrollBar()
        return bar.value() >= bar.maximum() - 2

    def jump_to_end(self, scroll_only: bool = False) -> None:
        bar = self.editor.verticalScrollBar()
        bar.setValue(bar.maximum())
        if not scroll_only:
            self._pending = 0
            if not self.btn_follow.isChecked():
                self.btn_follow.setChecked(True)
        self._refresh_status()

    # ---- 动作 ----

    def _flash(self, button: QPushButton, text: str, restore: str) -> None:
        button.setText(text)
        # 绑定到按钮自己（context 重载）：1.4 秒内窗口被关掉时定时器随之作废，
        # 不会去碰一个已经删掉的控件（否则槽里抛 RuntimeError，还被 Qt 吞掉）。
        QTimer.singleShot(1400, button, lambda: button.setText(restore))

    def _copy_all(self) -> None:
        from PySide6.QtWidgets import QApplication
        text = self.console.text_all()
        QApplication.clipboard().setText(text)
        lines, _ = self.console.stats()
        self._flash(self.btn_copy_all, "已复制 %d 行" % lines, "复制全部")

    def _copy_selected(self) -> None:
        self.editor.copy()

    def _clear(self) -> None:
        self.console.clear()
        self._seq = 0
        self._pending = 0
        self.editor.clear()
        self._refresh_status()

    def _save(self) -> None:
        import time as _t
        name = "dsh-console-%s.txt" % _t.strftime("%Y%m%d-%H%M%S")
        path, _f = QFileDialog.getSaveFileName(self, "导出终端输出", name,
                                               "文本文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.console.text_all() + "\n")
            self._flash(self.btn_save, "已导出", "另存为…")
        except OSError as e:
            self.status.setText("导出失败：%s" % e)

    def _search(self) -> None:
        needle = self.search.text().strip()
        if not needle:
            return
        backwards = bool(QApplication.keyboardModifiers()
                         & Qt.KeyboardModifier.ShiftModifier)
        flags = QTextDocument.FindFlag.FindBackward if backwards else \
            QTextDocument.FindFlag(0)
        if self.editor.find(needle, flags):
            return
        # 到头了：绕回另一端再找一次
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End if backwards
                            else QTextCursor.MoveOperation.Start)
        self.editor.setTextCursor(cursor)
        self.editor.find(needle, flags)

    def apply_theme(self, pal: dict) -> None:
        self.pal = pal
        self.setStyleSheet(_terminal_style(pal))
        self.highlighter.set_palette(pal)
        self._refresh_status()


class InspectorWindow(QWidget):
    """Chromium 自带开发者工具的宿主窗口（Elements / Console / Network / Sources…）。

    为什么直接用它、而不是自绘：盒模型与计算样式、强制状态、字体/对比度检查、
    Sources 断点、Network 的状态码与瀑布图、Application 的存储查看，全是现成的，
    而且与 Chrome 完全一致。自绘一张"布局表"永远追不上（老大的原话：完全无法比较）。

    代价是它自己要一个渲染实例（约 100~200MB），所以只在用户明确打开时创建，
    窗口关闭即销毁（`WA_DeleteOnClose`），不会常驻。
    """

    def __init__(self, inspected_page, theme: str = "light", parent=None):
        super().__init__(parent)
        self.setWindowTitle("DSH — 开发者工具")
        self.resize(1200, 820)
        self.setMinimumSize(720, 480)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._theme = theme if theme in ("light", "dark") else "light"
        self.pal = palette(self._theme)
        # 先记下被检查页：顶部工具条要读它的 host:port
        self._inspected = inspected_page

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_bar())

        self.view = QWebEngineView(self)
        # DevTools 自己的偏好（**包括界面语言**）要写进 profile 才记得住。
        # 不给 profile 的 page 是临时存储：关掉窗口，语言选择就丢了，
        # 于是每次都得重新点一次「Switch DevTools to Chinese」。
        self.profile = QWebEngineProfile("dsh-devtools", self)
        store = os.path.join(config.data_root(), "devtools-profile")
        self.profile.setPersistentStoragePath(store)
        self.profile.setCachePath(os.path.join(store, "cache"))
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)

        # 两页共用同一条工具条：DevTools 与终端输出随时切换。
        # 终端页在主线程 300ms 拉一次增量，创建成本可以忽略，所以不懒加载。
        self.terminal = TerminalView(self.pal)
        self.body = QStackedWidget(self)
        self.body.addWidget(self.view)
        self.body.addWidget(self.terminal)
        root.addWidget(self.body, 1)
        self.mode_group.idClicked.connect(self._switch_mode)

        self.setStyleSheet(_inspector_style(self.pal))

        #: DevTools 前端是否真的加载出来了（供自测/日志用）
        self.ready = False
        try:
            # Qt 6.3+：由工具页反向声明"我检查谁"
            self.page.setInspectedPage(inspected_page)
        except Exception:                                      # noqa: BLE001
            self._attach_legacy()

        # 个别发行版对 setInspectedPage 支持不全：2.5 秒还没起来就退老 API。
        # 用子对象定时器，窗口销毁时自动停，不会打到已释放的 C++ 对象上。
        self._check = QTimer(self)
        self._check.setSingleShot(True)
        self._check.timeout.connect(self._ensure_loaded)
        self._check.start(2500)

    def _build_bar(self) -> QWidget:
        """顶部一条窄工具条：置顶 + 被检查页地址。

        DevTools 自己的 UI 已经排得很满，且右上角被它自己的图标占着；
        在窗口里加一条 30px 的条，比往它的工具栏上叠按钮稳妥得多。
        """
        bar = QWidget()
        bar.setObjectName("inspectorBar")
        bar.setFixedHeight(32)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(10)

        title = QLabel("开发者工具")
        title.setObjectName("inspectorTitle")
        target = QLabel("")
        target.setObjectName("inspectorTarget")
        try:
            target.setText(redact(self._target_text()))
        except Exception:                                      # noqa: BLE001
            pass
        self.lbl_target = target

        self.btn_dev = QPushButton("开发者工具")
        self.btn_dev.setObjectName("segBtn")
        self.btn_dev.setCheckable(True)
        self.btn_dev.setChecked(True)
        self.btn_dev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dev.setToolTip("Chromium 开发者工具：元素 / 控制台 / 网络 / 源码")
        self.btn_term = QPushButton("终端")
        self.btn_term.setObjectName("segBtn")
        self.btn_term.setCheckable(True)
        self.btn_term.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_term.setToolTip("宿主进程的输出（打包前在终端里能看到的那份）")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.btn_dev, 0)
        self.mode_group.addButton(self.btn_term, 1)

        self.btn_pin = QPushButton("置顶")
        self.btn_pin.setObjectName("pinBtn")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pin.setToolTip("让开发者工具保持在其它窗口之上（再次点击取消）")
        self.btn_pin.toggled.connect(self._toggle_pin)

        lay.addWidget(title)
        lay.addWidget(target, 1)
        lay.addWidget(self.btn_dev)
        lay.addWidget(self.btn_term)
        lay.addSpacing(8)
        lay.addWidget(self.btn_pin)
        return bar

    def _target_text(self) -> str:
        page = getattr(self, "_inspected", None)
        if page is None:
            return ""
        u = page.url()
        return (u.host() + (":%d" % u.port() if u.port() > 0 else "")) if u.host() else ""

    def _switch_mode(self, index: int) -> None:
        """在「开发者工具」与「终端」之间切换。

        DevTools 不销毁也不重载：断点、选中的元素都保留；只是从布局里藏起来。
        """
        self.body.setCurrentIndex(index)
        if index == 1:
            self.terminal.tick()
            if self.terminal.btn_follow.isChecked():
                self.terminal.jump_to_end(scroll_only=True)

    def _toggle_pin(self, on: bool) -> None:
        """切换置顶。

        Qt 必须重新设置窗口标志并 `show()` 才生效；DevTools 页面本身不会
        重新加载，所以断点、选中的元素都还在。
        """
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(on))
        self.show()
        self.raise_()
        self.btn_pin.setText("已置顶" if on else "置顶")

    def apply_theme(self, theme: str) -> None:
        if theme not in ("light", "dark") or theme == self._theme:
            return
        self._theme = theme
        self.pal = palette(theme)
        self.setStyleSheet(_inspector_style(self.pal))
        terminal = getattr(self, "terminal", None)
        if terminal is not None:
            terminal.apply_theme(self.pal)

    def _attach_legacy(self) -> None:
        """Qt 6.3 之前只有 `setDevToolsPage()` 这一条路。"""
        try:
            self._inspected.setDevToolsPage(self.page)
        except Exception:                                      # noqa: BLE001
            pass

    def _ensure_loaded(self) -> None:
        try:
            self.ready = not self.page.url().isEmpty()
        except RuntimeError:
            return
        if not self.ready:
            self._attach_legacy()


class DevWindow(QWidget):
    """原生开发者窗口：控制台 / 网络 / 布局 / 环境。

    窗口**不**用 WebEngine 渲染（理由见模块文档）。所有数据都来自
    `Recorder` 与按需注入的 JS，主线程按 400ms 轮询增量取。
    """

    NET_COLS = ("时间", "类型", "方法", "大小", "耗时", "地址")
    LAY_COLS = ("元素", "尺寸 W×H", "位置 x,y", "display", "overflow")

    def __init__(self, page, recorder: Recorder, theme: str = "dark",
                 open_inspector=None, parent=None):
        super().__init__(parent)
        self.page = page
        self.recorder = recorder
        self._open_inspector_cb = open_inspector
        self._theme = theme if theme in ("light", "dark") else "dark"
        self.pal = palette(self._theme)

        self.setWindowTitle("DSH — 开发者窗口")
        self.resize(1000, 660)
        self.setMinimumSize(760, 480)

        self._console_seq = 0
        self._net_seq = 0
        self._only_errors = False
        self._net_rows: dict[str, int] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("轻量日志")
        title.setObjectName("devTitle")
        self.lbl_url = QLabel("—")
        self.lbl_url.setObjectName("devUrl")
        self.lbl_url.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_url.setSizePolicy(QSizePolicy.Policy.Ignored,
                                   QSizePolicy.Policy.Preferred)
        head.addWidget(title)
        head.addWidget(self.lbl_url, 1)
        if self._open_inspector_cb is not None:
            btn_full = QPushButton("打开完整开发者工具")
            btn_full.setToolTip("Chromium 自带：Elements 盒模型 / Sources 断点 / "
                                "Network 状态码 / Application 存储")
            btn_full.clicked.connect(lambda: self._open_inspector_cb())
            head.addWidget(btn_full)
        root.addLayout(head)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_console(), "控制台")
        self.tabs.addTab(self._build_net(), "网络")
        self.tabs.addTab(self._build_layout(), "布局快照")
        self.tabs.addTab(self._build_env(), "环境")
        self.tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self.tabs, 1)

        self.lbl_foot = QLabel("—")
        self.lbl_foot.setObjectName("devHint")
        root.addWidget(self.lbl_foot)

        self.setStyleSheet(_style(self.pal))

        # 页面事件写进控制台，省得用户对不上"什么时候加载的"。
        try:
            page.loadStarted.connect(
                lambda: self._note("页面开始加载"))
            page.loadFinished.connect(
                lambda ok: self._note("页面加载完成（%s）" % ("成功" if ok else "失败")))
            page.renderProcessTerminated.connect(
                lambda status, code: self._note(
                    "渲染进程退出：%s / code=%s" % (status, code)))
        except Exception:                                      # noqa: BLE001
            pass

        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    # ---- 主题 ----

    def apply_theme(self, theme: str) -> None:
        if theme not in ("light", "dark") or theme == self._theme:
            return
        self._theme = theme
        self.pal = palette(theme)
        self.setStyleSheet(_style(self.pal))
        self._rebuild_console()

    # ---- 控制台页 ----

    def _build_console(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_errors = QPushButton("只看错误")
        self.btn_errors.setCheckable(True)
        self.btn_errors.toggled.connect(self._on_error_filter)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._clear_console)
        self.lbl_console = QLabel("0 条")
        self.lbl_console.setObjectName("devHint")
        bar.addWidget(self.btn_errors)
        bar.addWidget(btn_clear)
        bar.addWidget(self.lbl_console)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(MAX_CONSOLE)
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        f = self.console.font()
        f.setFamily("Consolas")
        f.setPointSize(9)
        self.console.setFont(f)
        lay.addWidget(self.console, 1)
        return w

    def _note(self, text: str) -> None:
        """窗口自己产生的说明行（加载、崩溃之类），不进 Recorder。"""
        if not hasattr(self, "console"):
            return
        ts = time.strftime("%H:%M:%S")
        self.console.appendHtml(
            '<span style="color:%s">%s</span> <i style="color:%s">· %s</i>'
            % (self.pal["muted"], ts, self.pal["accent"], html.escape(text)))

    def _console_line(self, e: ConsoleEntry) -> None:
        color = {"error": self.pal["bad"], "warning": self.pal["warn"],
                 "info": self.pal["muted"]}.get(e.level, self.pal["text"])
        ts = time.strftime("%H:%M:%S", time.localtime(e.ts))
        src = ""
        if e.source:
            src = "%s:%d" % (os.path.basename(e.source.split("?")[0]), e.line)
        body = html.escape(e.message).replace("\n", "<br/>")
        self.console.appendHtml(
            '<span style="color:%s">%s</span> <b style="color:%s">[%s]</b> %s'
            '<span style="color:%s"> %s</span>'
            % (self.pal["muted"], ts, color, e.level, body,
               self.pal["muted"], html.escape(src) if src else ""))

    def _rebuild_console(self) -> None:
        self.console.clear()
        for e in self.recorder.since_console(0):
            if self._only_errors and e.level != "error":
                continue
            self._console_line(e)

    def _on_error_filter(self, on: bool) -> None:
        self._only_errors = bool(on)
        self._rebuild_console()

    def _clear_console(self) -> None:
        self.recorder.clear_console()
        self._console_seq = 0
        self.console.clear()
        self.lbl_console.setText("0 条")

    # ---- 网络页 ----

    def _build_net(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._clear_net)
        btn_perf = QPushButton("读取已加载资源")
        btn_perf.setToolTip("从页面性能条目里补齐开发者模式开启之前的请求")
        btn_perf.clicked.connect(self._load_perf)
        btn_copy = QPushButton("复制地址")
        btn_copy.clicked.connect(self._copy_net_url)
        self.lbl_net = QLabel("0 条")
        self.lbl_net.setObjectName("devHint")
        for b in (btn_clear, btn_perf, btn_copy):
            bar.addWidget(b)
        bar.addWidget(self.lbl_net)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.net = QTableWidget(0, len(self.NET_COLS))
        self.net.setHorizontalHeaderLabels(self.NET_COLS)
        self.net.verticalHeader().setVisible(False)
        self.net.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.net.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.net.setAlternatingRowColors(True)
        self.net.setShowGrid(False)
        hh = self.net.horizontalHeader()
        hh.setStretchLastSection(True)
        self.net.setColumnWidth(0, 68)
        self.net.setColumnWidth(1, 84)
        self.net.setColumnWidth(2, 56)
        self.net.setColumnWidth(3, 74)
        self.net.setColumnWidth(4, 62)
        self.net.cellDoubleClicked.connect(lambda r, c: self._copy_net_url())
        lay.addWidget(self.net, 1)
        return w

    def _net_add(self, method: str, rtype: str, url: str, size: int = -1,
                 duration: float = -1.0, stamp: float | None = None) -> None:
        row = self._net_rows.get(url)
        if row is not None and row < self.net.rowCount():
            # 同一地址被性能条目补全：就地填大小/耗时，不重复记一行
            self._set_net_cell(row, 3, _size_text(size))
            self._set_net_cell(row, 4,
                               "—" if duration < 0 else "%d ms" % duration)
            return
        row = self.net.rowCount()
        self.net.insertRow(row)
        for col in range(len(self.NET_COLS)):
            self.net.setItem(row, col, QTableWidgetItem(""))
        ts = time.strftime("%H:%M:%S",
                           time.localtime(stamp if stamp is not None else time.time()))
        vals = (ts, rtype, method, _size_text(size),
                "—" if duration < 0 else "%d ms" % duration, url)
        for col, v in enumerate(vals):
            self._set_net_cell(row, col, v)
        self._net_rows[url] = row
        # 表里不要无限长：超过上限就砍最旧的
        if self.net.rowCount() > MAX_NET:
            self.net.removeRow(0)
            self._net_rows = {u: r - 1 for u, r in self._net_rows.items() if r > 0}

    def _set_net_cell(self, row: int, col: int, text: str) -> None:
        item = self.net.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            self.net.setItem(row, col, item)
        item.setText(text)
        item.setToolTip(text)

    def _clear_net(self) -> None:
        self.recorder.clear_net()
        self._net_seq = 0
        self.net.setRowCount(0)
        self._net_rows.clear()
        self.lbl_net.setText("0 条")

    def _copy_net_url(self) -> None:
        row = self.net.currentRow()
        if row < 0:
            return
        item = self.net.item(row, len(self.NET_COLS) - 1)
        if item is not None:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(item.text())
            self.lbl_foot.setText("已复制地址：" + redact(item.text()))

    def _load_perf(self) -> None:
        def done(raw):
            value = _as_json(raw)
            if not isinstance(value, list):
                self.lbl_foot.setText("没有读到性能条目（页面可能还没加载完）")
                return
            for e in value:
                if not isinstance(e, dict):
                    continue
                self._net_add("GET", str(e.get("type") or "resource"),
                              redact(str(e.get("name") or "")),
                              int(e.get("size") or 0), float(e.get("dur") or 0))
            self.lbl_net.setText("%d 条" % self.net.rowCount())
            self.lbl_foot.setText("已从页面性能条目补齐 %d 条" % len(value))

        self.page.runJavaScript(JS_PERF, done)

    # ---- 布局页 ----

    def _build_layout(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        btn_scan = QPushButton("重新扫描")
        btn_scan.clicked.connect(self._scan_layout)
        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText("按标签 / 类名 / 选择器过滤")
        self.edit_filter.textChanged.connect(self._filter_layout)
        self.lbl_layout = QLabel("0 个元素")
        self.lbl_layout.setObjectName("devHint")
        bar.addWidget(btn_scan)
        bar.addWidget(self.edit_filter, 1)
        bar.addWidget(self.lbl_layout)
        lay.addLayout(bar)

        tip = QLabel("这里只给一份坐标尺寸快照（用来快速定位谁把它挤走了）。"
                     "盒模型、计算样式、层级与实时改动请用「打开完整开发者工具」的 Elements。")
        tip.setObjectName("devHint")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.layout_tbl = QTableWidget(0, len(self.LAY_COLS))
        self.layout_tbl.setHorizontalHeaderLabels(self.LAY_COLS)
        self.layout_tbl.verticalHeader().setVisible(False)
        self.layout_tbl.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.layout_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.layout_tbl.setAlternatingRowColors(True)
        self.layout_tbl.setShowGrid(False)
        self.layout_tbl.horizontalHeader().setStretchLastSection(True)
        self.layout_tbl.setColumnWidth(0, 420)
        self.layout_tbl.setColumnWidth(1, 96)
        self.layout_tbl.setColumnWidth(2, 96)
        self.layout_tbl.setColumnWidth(3, 76)
        self.layout_tbl.itemSelectionChanged.connect(self._highlight_row)
        lay.addWidget(self.layout_tbl, 1)
        return w

    def _scan_layout(self) -> None:
        def done(raw):
            value = _as_json(raw)
            if not isinstance(value, list):
                self.lbl_foot.setText("扫描失败：页面还没准备好")
                return
            self.layout_tbl.setRowCount(0)
            for e in value:
                if not isinstance(e, dict):
                    continue
                r = self.layout_tbl.rowCount()
                self.layout_tbl.insertRow(r)
                name = "<%s>" % e.get("tag", "?")
                if e.get("id"):
                    name += "#" + str(e["id"])
                if e.get("cls"):
                    name += "." + str(e["cls"])
                if e.get("depth"):
                    name = "  " * min(int(e["depth"]), 8) + name
                vals = (name,
                        "%s×%s" % (e.get("w"), e.get("h")),
                        "%s,%s" % (e.get("x"), e.get("y")),
                        str(e.get("display") or ""),
                        str(e.get("of") or ""))
                for c, v in enumerate(vals):
                    item = QTableWidgetItem(v)
                    if c == 0:
                        item.setData(Qt.ItemDataRole.UserRole, e.get("sel"))
                        item.setToolTip(str(e.get("sel") or ""))
                    self.layout_tbl.setItem(r, c, item)
            self.lbl_layout.setText("%d 个元素" % self.layout_tbl.rowCount())
            self.lbl_foot.setText("已扫描：点击任意一行会在页面里高亮该元素")

        self.page.runJavaScript(JS_LAYOUT, done)

    def _filter_layout(self, text: str) -> None:
        needle = (text or "").strip().lower()
        shown = 0
        for r in range(self.layout_tbl.rowCount()):
            item = self.layout_tbl.item(r, 0)
            hit = True
            if needle and item is not None:
                hay = (item.text() + " " + str(item.toolTip())).lower()
                hit = needle in hay
            self.layout_tbl.setRowHidden(r, not hit)
            if hit:
                shown += 1
        self.lbl_layout.setText("%d / %d 个元素"
                                % (shown, self.layout_tbl.rowCount()))

    def _highlight_row(self) -> None:
        row = self.layout_tbl.currentRow()
        if row < 0:
            return
        item = self.layout_tbl.item(row, 0)
        if item is None:
            return
        sel = item.data(Qt.ItemDataRole.UserRole)
        if not sel:
            return

        def done(res):
            if res != "ok":
                self.lbl_foot.setText("没能定位到该元素（页面可能已经重绘）：%s" % res)

        self.page.runJavaScript(JS_HILITE + "(%s)" % json.dumps(str(sel)), done)

    # ---- 环境页 ----

    def _build_env(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        self.env_rows: dict[str, QLabel] = {}
        for key, title in (("url", "页面地址"), ("title", "页面标题"),
                           ("viewport", "视口"), ("dpr", "设备像素比"),
                           ("ready", "就绪状态"), ("nodes", "DOM 节点数"),
                           ("ua", "User Agent")):
            row = QHBoxLayout()
            row.setSpacing(10)
            k = QLabel(title)
            k.setObjectName("devHint")
            k.setFixedWidth(96)
            v = QLabel("—")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(k)
            row.addWidget(v, 1)
            lay.addLayout(row)
            self.env_rows[key] = v

        bar = QHBoxLayout()
        bar.setSpacing(6)
        btn_reload = QPushButton("重新加载")
        btn_reload.clicked.connect(
            lambda: self.page.triggerAction(QWebEnginePage.WebAction.Reload))
        btn_cache = QPushButton("清缓存并重载")
        btn_cache.clicked.connect(self._clear_cache_reload)
        btn_tools = QPushButton("内核开发者工具")
        btn_tools.setToolTip("Chromium 自带的完整开发者工具：Elements / Sources / "
                             "Application / Performance 都在里面。"
                             "会额外启动一个渲染实例（约 100~200MB），"
                             "关掉窗口即释放。")
        if self._open_inspector_cb is not None:
            btn_tools.clicked.connect(lambda: self._open_inspector_cb())
        else:
            btn_tools.setEnabled(False)
        btn_copy = QPushButton("复制诊断信息")
        btn_copy.clicked.connect(self._copy_diag)
        for b in (btn_reload, btn_cache, btn_tools, btn_copy):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)

        hint = QLabel("轻量窗口：控制台消息与请求只保留最近 %d / %d 条，"
                      "布局扫描上限 1200 个可见元素。"
                      "完整能力（Elements / Sources / Network 瀑布图 / Application）"
                      "在「打开完整开发者工具」里。"
                      % (MAX_CONSOLE, MAX_NET))
        hint.setObjectName("devHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        return w

    def _refresh_env(self) -> None:
        def done(raw):
            v = _as_json(raw)
            if not isinstance(v, dict):
                return
            self.lbl_url.setText(redact(str(v.get("url") or "")))
            self.env_rows["url"].setText(redact(str(v.get("url") or "")))
            self.env_rows["title"].setText(str(v.get("title") or "—"))
            self.env_rows["viewport"].setText("%s × %s"
                                              % (v.get("vw"), v.get("vh")))
            self.env_rows["dpr"].setText(str(v.get("dpr")))
            self.env_rows["ready"].setText(str(v.get("ready")))
            self.env_rows["nodes"].setText(str(v.get("nodes")))
            self.env_rows["ua"].setText(str(v.get("ua") or ""))

        self.page.runJavaScript(JS_ENV, done)

    def _clear_cache_reload(self) -> None:
        try:
            self.page.profile().clearHttpCache()
        except Exception as e:                                 # noqa: BLE001
            self.lbl_foot.setText("清缓存失败：%s" % e)
            return
        self.page.triggerAction(QWebEnginePage.WebAction.Reload)
        self.lbl_foot.setText("已清空 HTTP 缓存并重新加载")

    def _diag_text(self) -> str:
        c_count, n_count = self.recorder.counts()
        errs = [e for e in self.recorder.since_console(0)
                if e.level == "error"][-30:]
        lines = [
            "桌面壳 v%s" % HOST_VERSION,
            "页面地址：%s" % redact(self.lbl_url.text()),
            "视口：%s" % self.env_rows["viewport"].text(),
            "设备像素比：%s" % self.env_rows["dpr"].text(),
            "User Agent：%s" % self.env_rows["ua"].text(),
            "控制台 %d 条 / 请求 %d 条" % (c_count, n_count),
        ]
        if errs:
            lines.append("— 最近的控制台错误 —")
            for e in errs:
                ts = time.strftime("%H:%M:%S", time.localtime(e.ts))
                lines.append("[%s] %s (%s:%d)"
                             % (ts, e.message.replace("\n", " "),
                                os.path.basename(e.source or ""), e.line))
        return "\n".join(lines)

    def _copy_diag(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._diag_text())
        self.lbl_foot.setText("诊断信息已复制到剪贴板")

    # ---- 轮询 ----

    def showEvent(self, e) -> None:                            # noqa: N802
        super().showEvent(e)
        self._tick()
        if not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, e) -> None:                            # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def _on_tab(self, index: int) -> None:
        if index == 1:
            self.lbl_net.setText("%d 条" % self.net.rowCount())
        elif index == 2:
            self._scan_layout()
        elif index == 3:
            self._refresh_env()

    def _tick(self) -> None:
        for e in self.recorder.since_console(self._console_seq):
            self._console_seq = e.seq
            if not self._only_errors or e.level == "error":
                self._console_line(e)
        for e in self.recorder.since_net(self._net_seq):
            self._net_seq = e.seq
            self._net_add(e.method, e.rtype, e.url, e.size, e.duration, e.ts)

        c, n = self.recorder.counts()
        self.lbl_console.setText("%d 条" % c)
        self.lbl_net.setText("%d 条" % self.net.rowCount())
        self.lbl_foot.setText("控制台 %d · 请求 %d · %s"
                              % (c, n, "开发者模式已开启"
                                 if self.recorder.net_enabled
                                 else "开发者模式已关闭（仅显示历史记录）"))


# ------------------------------------------------------------------ 自测

def selftest_checks() -> list:
    """纯逻辑断言（不构造 WebEngine）。挂在 GUI 自测的两条分支上。"""
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                               ("  " + extra) if extra else ""))
        if not ok:
            failures.append(name)

    base = "http://127.0.0.1:3080/?token=abc"
    cases = [
        ("同源根路径留在内核", "http://127.0.0.1:3080/?token=abc", "internal"),
        ("同源子路径留在内核", "http://127.0.0.1:3080/chat/1", "internal"),
        ("异源交给系统浏览器", "https://platform.deepseek.com/usage", "external"),
        ("同主机不同端口的链接也不留在内核", "http://127.0.0.1:3081/x", "external"),
        ("localhost 与 127.0.0.1 不是同源", "http://localhost:3080/x", "external"),
        ("mailto 交系统处理", "mailto:a@b.c", "external"),
        ("javascript: 不能在外部处理", "javascript:void(0)", "internal"),
        ("空地址忽略", "", "ignore"),
    ]
    bad = []
    for name, url, expect in cases:
        got = nav_disposition(url, base)
        if got != expect:
            bad.append("%s→%s(应 %s)" % (name, got, expect))
    check("外链出口策略（同源留内 / 异源外跳）", not bad, "、".join(bad))

    check("基址未知时不擅自外跳",
          nav_disposition("http://127.0.0.1:3080/x", "") == "internal")

    # --- 剪贴板权限只给我方页面（2026-10-20「点复制没反应」的闸门）---
    # 不授权时 writeText 会以 NotAllowedError 结束，而 DSH 的复制助手
    # 拿到异常直接 return false（不回退 execCommand），表现就是"点了没反应"。
    clip_cases = [
        ("同源放行", "http://127.0.0.1:3080/", "http://127.0.0.1:3080/?token=t", True),
        ("换端口不放行", "http://127.0.0.1:3081/", "http://127.0.0.1:3080/", False),
        ("换协议不放行", "https://127.0.0.1:3080/", "http://127.0.0.1:3080/", False),
        ("异源不放行", "https://platform.deepseek.com/", "http://127.0.0.1:3080/", False),
        ("localhost 与 127.0.0.1 不同源", "http://localhost:3080/",
         "http://127.0.0.1:3080/", False),
        ("基址未知时只给 loopback", "http://127.0.0.1:3080/", "", True),
        ("基址未知时不信任异源", "https://evil.example/", "", False),
    ]
    bad = []
    for name, origin, b, expect in clip_cases:
        got = clipboard_origin_allowed(origin, b)
        if got != expect:
            bad.append("%s→%s(应 %s)" % (name, got, expect))
    check("剪贴板权限只给同源页面", not bad, "、".join(bad))
    check("剪贴板权限覆盖 writeText 对应的 Feature",
          QWebEnginePage.Feature.ClipboardReadWrite in CLIPBOARD_FEATURES)

    # 真调一遍权限处理器（用桩替掉 QWebEnginePage 的部分，不建 WebEngine）
    captured = []

    class _StubPage:
        def _base(self):
            return base

        def setFeaturePermission(self, origin, feature, policy):  # noqa: N802
            captured.append((origin.toString(), feature, policy))

    stub = _StubPage()
    grant = QWebEnginePage.PermissionPolicy.PermissionGrantedByUser
    deny = QWebEnginePage.PermissionPolicy.PermissionDeniedByUser
    ShellPage._on_feature_permission(
        stub, QUrl("http://127.0.0.1:3080/"),
        QWebEnginePage.Feature.ClipboardReadWrite)
    ShellPage._on_feature_permission(
        stub, QUrl("https://evil.example/"),
        QWebEnginePage.Feature.ClipboardReadWrite)
    ShellPage._on_feature_permission(
        stub, QUrl("http://127.0.0.1:3080/"),
        QWebEnginePage.Feature.Geolocation)
    check("权限处理器：同源剪贴板放行，异源与其它权限拒绝",
          [c[2] for c in captured] == [grant, deny, deny],
          "、".join(str(c[2]).rsplit(".", 1)[-1] for c in captured))

    # 令牌打码：日志里不能出现明文 token
    r = redact(base)
    check("日志里令牌被打码", "abc" not in r and "token=***" in r, r)
    check("打码不影响其它参数",
          redact("http://h/p?token=x&a=1") == "http://h/p?token=***&a=1")

    # 记录器是跨线程的，必须能安全并发写
    rec = Recorder()
    def worker(n: int) -> None:
        for i in range(50):
            rec.add_console("log", "t%d-%d" % (n, i), i, "x.js")
            rec.add_net("GET", "xhr", "http://h/%d/%d" % (n, i))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c, n = rec.counts()
    check("记录器并发写入不丢条目", c == 200 and n == 200, "%d / %d" % (c, n))
    seqs = [e.seq for e in rec.since_console(0)]
    check("序号严格递增且不重复", all(b > a for a, b in zip(seqs, seqs[1:])),
          "%d 条" % len(seqs))
    # 序号在两类记录间共享（都从同一个计数器取），所以"新条目"要按实际
    # 序号切，不能按条数推。
    check("增量读取只返回新条目",
          len(rec.since_console(seqs[149])) == 50,
          str(len(rec.since_console(seqs[149]))))

    # --- 2026-10-19 线上故障的回归断言 ---
    # 当时误以为 QWebEngineNewWindowRequest 有 accept()/reject()，调用直接抛
    # AttributeError，而它发生在"交给系统浏览器"之前 —— 表现就是点了没反应。
    from PySide6.QtWebEngineCore import QWebEngineNewWindowRequest
    check("新窗口请求只有 openIn，没有 accept/reject",
          hasattr(QWebEngineNewWindowRequest, "openIn")
          and not hasattr(QWebEngineNewWindowRequest, "reject")
          and not hasattr(QWebEngineNewWindowRequest, "accept"))
    # 直接看字节码里有没有把 reject/accept 当成属性去取——比 grep 源码可靠
    # （源码里出现这几个字可能只是注释在讲这段历史）。
    import dis as _dis
    touched = {ins.argval for ins in _dis.get_instructions(ShellPage._on_new_window)
               if ins.opname in ("LOAD_ATTR", "LOAD_METHOD", "LOAD_GLOBAL")}
    check("_on_new_window 不调用 accept/reject",
          "reject" not in touched and "accept" not in touched,
          "、".join(sorted(map(str, touched & {"reject", "accept"}))))

    # --- 2026-10-21「长任务后页面变空白」的页面监护 ---
    # 阈值与限流是这段逻辑仅有的可错点：太灵敏会在渲染进程忙时误重载
    # （打断用户正在跑的对话），太迟钝等于没有。
    class _WatchdogStub:
        def __init__(self):
            self._reload_requests = deque()
            self._hb_blank = 0
            self._loaded_ok = True
            self.recoveries = 0
            self.escalations = 0
            self.logs = []

        def _may_recover(self, why):
            return ShellPage._may_recover(self, why)

        def _shell_log(self, msg):
            self.logs.append(msg)

        def _notify(self, msg):
            pass

        def _recover_page(self, force_base=False):
            self.recoveries += 1

        def _escalate_to_host(self, why):
            self.escalations += 1
            self.logs.append("escalate:" + why)

    wd = _WatchdogStub()
    ShellPage._hb_done(wd, 0)
    ShellPage._hb_done(wd, 0)
    early = wd.recoveries
    ShellPage._hb_done(wd, 0)
    one = wd.recoveries
    ShellPage._hb_done(wd, 9)          # 页面有内容 → 计数归零
    reset = wd._hb_blank == 0
    for _ in range(3):                 # 再连续 3 次空 → 第 2 次恢复
        ShellPage._hb_done(wd, 0)
    two = wd.recoveries
    for _ in range(3):                 # 第 3 次
        ShellPage._hb_done(wd, 0)
    three = wd.recoveries
    for _ in range(9):                 # 继续崩：120s 内已到 3 次，此后必须停手
        ShellPage._hb_done(wd, 0)
    check("白屏要连续 3 次才判（两次空不动）", early == 0, str(early))
    check("第三次连续为空触发恢复", one == 1, str(one))
    check("页面正常后计数归零", reset)
    check("120 秒内最多恢复 3 次",
          two == 2 and three == 3 and wd.recoveries == 3,
          "%d / %d / %d" % (two, three, wd.recoveries))
    check("超出上限时写清原因", any("次数过多" in m for m in wd.logs))
    # 2026-10-24：自动恢复用尽不能就此停手——升级为「重建 Web 视图」，
    # 那是唯一能治"合成表面丢失"的手段（宿主自己会限流）。
    check("恢复次数用尽后升级为重建视图", wd.escalations >= 1,
          "%d 次" % wd.escalations)

    # --- 2026-10-21「点下载 session 没反应」---
    # QtWebEngine 对没人接的 `downloadRequested` **静默取消**，而前端
    # `a.download` 的 click 早已同步返回 —— "没反应"是唯一症状。
    # 这里钉三件事：名字要洗、取消要显式 cancel、完成后要告诉用户存哪。
    check("下载文件名清洗（非法字符 / 设备名 / 空名）",
          browser_safe_filename("a/b:c*?.zip") == "a_b_c__.zip"
          and browser_safe_filename("CON.txt").startswith("_")
          and browser_safe_filename("") == "download.bin")

    import tempfile as _tf
    _dl_dir = _tf.mkdtemp(prefix="dl-selftest-")
    open(os.path.join(_dl_dir, "x.zip"), "w", encoding="utf-8").close()
    check("同名下载不静默覆盖",
          unique_download_path(_dl_dir, "x.zip").endswith("x (1).zip"),
          os.path.basename(unique_download_path(_dl_dir, "x.zip")))

    class _Sig:
        def __init__(self):
            self.fn = None

        def connect(self, fn):
            self.fn = fn

        def emit(self, *a):
            if self.fn is not None:
                self.fn(*a)

    class _FakeProfile:
        def __init__(self):
            self.downloadRequested = _Sig()

    class _FakeReq:
        def __init__(self, name="dsh-session-abc.zip"):
            self.name = name
            self.dir = ""
            self.fname = ""
            self.accepted = False
            self.cancelled = False
            self.stateChanged = _Sig()

        def downloadFileName(self):
            return self.name

        def suggestedFileName(self):
            return self.name

        def setDownloadDirectory(self, d):
            self.dir = d

        def setDownloadFileName(self, n):
            self.fname = n

        def accept(self):
            self.accepted = True

        def cancel(self):
            self.cancelled = True

        def interruptReason(self):
            return (QWebEngineDownloadRequest.DownloadInterruptReason
                    .FileAccessDenied)

    S = QWebEngineDownloadRequest.DownloadState
    target = os.path.join(_dl_dir, "saved.zip")
    prof = _FakeProfile()
    notes: list[str] = []
    DownloadManager(prof, choose_path=lambda _n: target,
                    notify=notes.append, log=lambda _s: None)
    r1 = _FakeReq()
    prof.downloadRequested.emit(r1)
    check("下载请求被接受并落到所选目录",
          r1.accepted and not r1.cancelled
          and os.path.normcase(r1.dir) == os.path.normcase(_dl_dir)
          and r1.fname == "saved.zip",
          "%s / %s" % (r1.dir, r1.fname))
    check("开始下载时有反馈", any("正在下载" in n for n in notes))
    r1.stateChanged.emit(S.DownloadCompleted)
    check("下载完成后告知保存位置", any("已保存到" in n for n in notes),
          notes[-1] if notes else "")

    prof2 = _FakeProfile()
    r2 = _FakeReq()
    DownloadManager(prof2, choose_path=lambda _n: "", notify=notes.append,
                    log=lambda _s: None)
    prof2.downloadRequested.emit(r2)
    check("用户取消时显式 cancel（不留悬空请求）",
          r2.cancelled and not r2.accepted)

    prof3 = _FakeProfile()
    r3 = _FakeReq()
    notes3: list[str] = []
    DownloadManager(prof3, choose_path=lambda _n: os.path.join(_dl_dir, "f3.zip"),
                    notify=notes3.append, log=lambda _s: None)
    prof3.downloadRequested.emit(r3)
    r3.stateChanged.emit(S.DownloadInterrupted)
    check("下载中断会告知原因", any("未完成" in n for n in notes3),
          notes3[-1] if notes3 else "")

    return failures
