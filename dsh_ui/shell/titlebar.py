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
# 下划线开头的工具 star-import 不带。取色回调里用到的这两个必须显式导：
# 缺了会在 runJavaScript 的 C++ 回调里抛 NameError，而 Qt 把异常挂到
# 下一次回调上，报出来是「eventFilter 里的 NameError」——位置完全误导
# （2026-11-13 老大真机遇到的正是这个）。
from dsh_ui.prelude import _parse_probe_color  # noqa: F401
from dsh_ui.workers import (CheckWorker, DataTaskWorker, DiagnoseWorker, HostCheckWorker,
                            HostUpdateWorker, NotesWorker, ProvisionWorker,
                            RemoteConnectWorker, ServiceSwapWorker, StartupWorker,
                            UpdateWorker, VersionReverifyWorker)


THEME_CHANGE_TOL = 6
_THEME_WATCH_MIN_MS = 1500
_JS_THEME_PROBE = r"""
(function () {
  function parse(c) {
    var m = /rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/
              .exec(c || '');
    if (!m) { return null; }
    var a = (m[4] === undefined) ? 1 : parseFloat(m[4]);
    if (!(a > 0.5)) { return null; }          // 半透明及以上不算"看得见的底色"
    return [parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3], 10)];
  }
  function effectiveBg(el) {
    var guard = 0;
    while (el && el.nodeType === 1 && guard++ < 64) {
      var c = parse(getComputedStyle(el).backgroundColor);
      if (c) { return c; }
      el = el.parentElement;
    }
    return null;
  }
  var W = window.innerWidth, H = window.innerHeight;
  if (W < 4 || H < 4) { return null; }
  var ys = [0.015, 0.03, 0.05, 0.08];
  var xs = [0.03, 0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 0.97];
  var counts = {}, best = null, bestN = 0;
  for (var i = 0; i < ys.length; i++) {
    var y = Math.max(1, Math.round(H * ys[i]));
    for (var j = 0; j < xs.length; j++) {
      var x = Math.max(1, Math.round(W * xs[j]));
      var c = effectiveBg(document.elementFromPoint(x, y));
      if (!c) { continue; }
      var k = c.join(',');
      counts[k] = (counts[k] || 0) + 1;
      if (counts[k] > bestN) { bestN = counts[k]; best = c; }
    }
  }
  return best ? best.join(',') : null;
})();
"""


class TitlebarMixin:

    _probe_trace: list = []
    _tb_attempt: int = 0
    _TB_RETRY_MS = (250, 400, 600, 900, 1300, 1800, 2400)

    def _apply_titlebar(self, rgb):
        """把取到的主色上到标题栏。

        四个属性一起设，少一个就会出现违和的细节：
          CAPTION_COLOR  底色
          TEXT_COLOR     标题文字色——不设的话浅底上会白字（读不了）
          BORDER_COLOR   最外圈 1px 边框，见下面「边框」一节
          DARK_MODE      影响系统绘制的关闭按钮/边框高光

        边框**不能直接给底色**（2026-10-24 修）
        --------------------------------------
        早先是把标题栏底色原样传给边框，想让边框和标题栏融为一体。
        但界面本身是白的，取色结果就是 `#ffffff`，边框跟着变纯白 —— 盖在
        另一个纯白应用上时窗口边界彻底消失（实测对比度 1.00，即完全同色）。
        改成走 `dwm.border_shade()`：同族、只差一点明度，亮底压暗、暗底提亮。
        依据与实测数据见该函数的 docstring。
        """
        if not dwm.available() or rgb is None:
            return
        mute = float(config.get("titlebar_mute") or 0.0)
        # 用降饱和（往**自身灰度**拉）而不是往固定深灰 blend：
        # 后者会把纯白拉成灰色，浅色主题下观感明显违和。
        # 详见 dwm.desaturate 的 docstring。
        bg = dwm.desaturate(rgb, mute) if mute else rgb
        fg = dwm.contrast_text(bg)
        dark = dwm.is_dark(bg)
        edge = dwm.border_shade(bg)
        self._last_titlebar_rgb = rgb

        ok = dwm.set_caption_color(self, bg)
        dwm.set_text_color(self, fg)
        dwm.set_border_color(self, edge)
        dwm.set_dark_mode(self, dark)
        # 图标要跟着底色走：深色标题栏配白图标，否则黑图标看不见。
        # 用 try 包住——图标设置失败绝不能影响上色这个主功能。
        try:
            self._set_theme_icon(dark)
        except Exception as e:                                   # noqa: BLE001
            host_log("切换主题图标失败（不影响上色）：%s: %s"
                     % (type(e).__name__, e))
        if ok:
            host_log("标题栏上色：底色 %s 边框 %s（取自界面 %s）文字 %s"
                     % (dwm.to_hex(bg), dwm.to_hex(edge),
                        dwm.to_hex(rgb), dwm.to_hex(fg)))

        # 这个 `dark` 就是"界面当前是深是浅"的既有判定结果，别处（控制面板）直接复用，
        # 不要在面板里再判一次——两处判断迟早会不一致。同时记住，下次启动能直接用。
        self._theme_dark = bool(dark)
        config.set(last_theme_dark=bool(dark))
        # 浮窗是按显示那一刻的配色画的，主题翻了就先收起来，别留一块旧色。
        try:
            dsh_theme.hide_tooltip()
            # 兜底模式的调色板是全局静态量，主题翻了要重新写一次。
            dsh_theme.refresh_tooltips()
        except (AttributeError, RuntimeError):
            pass
        panel = getattr(self, "_panel", None)
        if panel is not None:
            panel.apply_theme("dark" if dark else "light")
        # 开发者窗口也跟主题走（同一套官方令牌，不另调色）
        dev = getattr(self, "_devwin", None)
        if dev is not None:
            dev.apply_theme("dark" if dark else "light")
        insp = getattr(self, "_inspector", None)
        if insp is not None:
            try:
                insp.apply_theme("dark" if dark else "light")
            except RuntimeError:
                pass        # 窗口已销毁
        # 托盘菜单是独立弹窗，不吃窗口样式表，主题变了要重新套一遍
        menu = getattr(self, "_menu", None)
        if menu is not None:
            dsh_theme.apply_menu(menu, dsh_theme.palette_now())
    def _set_theme_icon(self, dark_bg: bool) -> None:
        """按标题栏深浅切换窗口/托盘图标。

        用实例属性记住上次的状态，避免每次重设——重设窗口图标会让
        任务栏图标闪一下，而且托盘图标重建代价更高。
        """
        if getattr(self, "_icon_dark_bg", "unset") == dark_bg:
            return
        ic = icon_for(dark_bg)
        if ic.isNull():
            return
        self._icon_dark_bg = dark_bg
        self.setWindowIcon(ic)
        app = QApplication.instance()
        if app is not None:
            try:
                app.setWindowIcon(ic)
            except Exception:                                      # noqa: BLE001
                pass
        tray = getattr(self, "tray", None)
        if tray is not None:
            try:
                tray.setIcon(ic)
            except Exception:                                      # noqa: BLE001
                pass
    def _sample_titlebar_color(self, attempt: int = 0):
        """取界面主题色，给标题栏上色。**三层降级**。

        为什么必须降级（2026-09-21 实机教训）
        -------------------------------------
        原实现只有一层：`self.view.grab()`。但这是**不可靠**的来源 ——
        QWebEngineView 的网页在独立渲染进程/合成器里画，
        `QWidget.grab()` 不保证能拿到那些像素。实测在某些环境下它返回
        的是 widget 自身的空白背景（整片纯色），取出来的"主题色"是
        空白色，标题栏反而被染成一片灰白。

        所以按可靠性排序依次尝试，任一层成功即止：

          L1  JS 读计算样式    最准——直接拿到界面真正的背景色，
                               与合成方式无关。用的都是标准 DOM API
                               （elementFromPoint + getComputedStyle
                               + 向上找非透明祖先），**不依赖 DSH 的
                               私有类名或层级结构**，所以不违反分层铁律。
          L2  GDI PrintWindow  向系统要窗口真实像素（已验证可靠）。
                               截客户区顶部，兼做纯色校验。
          L3  view.grab()      兜底。同样做纯色校验，纯色则判失败。

        三层都失败就保持系统默认标题栏——**不要拿一个不可信的颜色上色**。

        为什么只取一次
        --------------
        定时器触发一次，不做持续监听：界面主题在使用中基本不变，
        而持续截图会拖慢界面。

        为什么要延迟
        ------------
        `loadFinished` 只代表 DOM 就绪，样式与首绘可能还没完成，
        这时取到的是空白页颜色。所以等一段固定时间再取。
        """
        if not config.get("adaptive_titlebar") or not dwm.available():
            return
        if self.view is None:
            return
        self._tb_attempt = attempt
        self._probe_trace = []
        try:
            self.view.page().runJavaScript(_JS_THEME_PROBE,
                                           self._on_js_theme_color)
        except Exception as e:                                   # noqa: BLE001
            host_log("标题栏取色异常（JS）：%s: %s" % (type(e).__name__, e))
            self._sample_by_pixels()
    def _on_js_theme_color(self, value):
        """L1 回调。value 形如 "30,32,36"，取不到是 None。"""
        rgb = _parse_probe_color(value)
        if rgb:
            host_log("标题栏取色：来源=JS 计算样式 %s" % dwm.to_hex(rgb))
            self._apply_titlebar(rgb)
            return
        self._probe_trace.append("JS 未取到")
        self._sample_by_pixels()
    def _sample_by_pixels(self):
        """L2 GDI PrintWindow -> L3 view.grab()。"""
        # --- L2：窗口真实像素 ---
        try:
            rgb = dwm.capture_window_top(int(self.winId()))
            if rgb:
                host_log("标题栏取色：来源=窗口像素 %s" % dwm.to_hex(rgb))
                self._apply_titlebar(rgb)
                return
            self._probe_trace.append("窗口像素未取到（纯色或截取失败）")
        except Exception as e:                                   # noqa: BLE001
            self._probe_trace.append("窗口像素异常 %s" % type(e).__name__)

        # --- L3：控件截图兜底 ---
        try:
            rgb = self._grab_view_color()
            if rgb:
                host_log("标题栏取色：来源=控件截图 %s" % dwm.to_hex(rgb))
                self._apply_titlebar(rgb)
                return
            self._probe_trace.append("控件截图未取到（纯色）")
        except Exception as e:                                   # noqa: BLE001
            self._probe_trace.append("控件截图异常 %s" % type(e).__name__)

        host_log("标题栏取色三层均失败，保持系统默认标题栏：%s"
                 % "；".join(self._probe_trace))
        # 多半是页面还没画出来（loadFinished 早于首绘）。退避后重试，
        # 而不是立刻放弃或死等。
        self._schedule_titlebar_probe(self._tb_attempt + 1)
    def _grab_view_color(self):
        """L3：截 QWebEngineView 取色。截不到有效内容返回 None。

        两段式判断：**空白判定看整幅**，取色只看顶部一条。
        不能只拿顶部一条判空白——真实界面的顶部可能就是一条纯色工具栏，
        那完全正常，据此判"没渲染"会误杀。

        性能：只拷顶部 6%，不整图 toBytes()（整图 1200x800 是 5.7MB、
        4K 是 33MB）。constBits 的 memoryview 需先 cast("B")——默认
        视图元素是 4 字节结构体，按字节切会算错元素数。
        """
        img = self.view.grab().toImage().convertToFormat(
            QImage.Format.Format_RGBA8888)
        w, h = img.width(), img.height()
        if w <= 0 or h <= 0:
            return None
        mv = img.constBits()
        if isinstance(mv, (bytes, bytearray)):
            mv = memoryview(mv)
        elif getattr(mv, "itemsize", 1) != 1:
            mv = mv.cast("B")
        stride = img.bytesPerLine()      # 不一定等于 w*4（行可能对齐填充）
        pad = max(0, int(w * 0.02))
        x0, span = pad, max(1, w - pad * 2)

        def _rows(y0, n, step=1):
            """取第 y0 行起的 n 行（左右各让开 pad），紧排成 RGB 缓冲。"""
            return b"".join(
                bytes(mv[y * stride + x0 * 4: y * stride + (x0 + span) * 4])
                for y in range(y0, min(y0 + n, h), step))

        # 先判整幅是否一片纯色（没渲染出来的控件背景就是整片同色）。
        # 按行抽样——逐行全转在 4K 下要搬 33MB，而"是否空白"只需
        # 有代表性的采样。上限约 96 行，代价与分辨率解耦。
        step_y = max(1, h // 96)
        full = _rows(0, h, step=step_y)
        sampled = len(range(0, h, step_y))
        if not full or dwm.flat_ratio(full, span, sampled) >= dwm.FLAT_RATIO:
            return None

        rows = max(1, int(h * 0.06))
        top = _rows(0, rows)          # 取色这一条要逐行，保证准确
        if not top:
            return None
        return dwm.dominant_color(top, span, len(top) // (span * 4))
    def _start_theme_watch(self):
        """按配置启动/停止主题跟踪。"""
        ms = int(config.get("theme_watch_ms") or 0)
        if ms > 0:
            ms = max(ms, _THEME_WATCH_MIN_MS)
        if not config.get("adaptive_titlebar") or ms <= 0:
            timer = getattr(self, "_theme_timer", None)
            if timer is not None:
                timer.stop()
            return
        timer = getattr(self, "_theme_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.timeout.connect(self._theme_watch_tick)
            self._theme_timer = timer
        # 已在跑就只保证节拍正确，不重启倒计时——避免频繁 start()
        # 反复重置计时器。
        if not timer.isActive() or timer.interval() != ms:
            timer.start(ms)
            host_log("主题跟踪：每 %d ms 检查一次（仅读计算样式，无截图）" % ms)
    def _theme_watch_tick(self):
        """跟踪 tick：只看 L1，且没变就什么都不做。"""
        if not config.get("adaptive_titlebar") or self.view is None:
            timer = getattr(self, "_theme_timer", None)
            if timer is not None:
                timer.stop()
            return
        # 最小化/隐藏时不看——省掉无谓的渲染进程往返
        if self.isMinimized() or not self.isVisible():
            return
        try:
            self.view.page().runJavaScript(_JS_THEME_PROBE,
                                           self._on_watch_color)
        except Exception:                                       # noqa: BLE001
            pass
    def _on_watch_color(self, value):
        """跟踪回调：与上次上色用的颜色比对，变了才动。"""
        rgb = _parse_probe_color(value)
        if rgb is None:
            return
        last = getattr(self, "_last_titlebar_rgb", None)
        if last is not None and all(
                abs(rgb[i] - last[i]) <= THEME_CHANGE_TOL for i in range(3)):
            return                       # 没变——大多数 tick 走这里
        host_log("标题栏：检测到界面配色变化 %s → %s"
                 % (dwm.to_hex(last) if last else "(无)", dwm.to_hex(rgb)))
        self._apply_titlebar(rgb)
    def _schedule_titlebar_probe(self, attempt: int):
        """安排第 attempt 次取色。"""
        if not config.get("adaptive_titlebar") or not dwm.available():
            return
        if attempt >= len(self._TB_RETRY_MS):
            host_log("标题栏取色：重试 %d 次仍未取到，放弃（保持系统默认）"
                     % attempt)
            return
        QTimer.singleShot(self._TB_RETRY_MS[attempt],
                          lambda a=attempt: self._sample_titlebar_color(a))
    def _toggle_titlebar(self, checked: bool):
        """开关标题栏自适应配色。"""
        config.set(adaptive_titlebar=bool(checked))
        self.act_titlebar.setChecked(bool(checked))
        host_log("标题栏跟随界面配色 → %s" % ("开" if checked else "关"))
        if checked:
            if not dwm.available():
                self._info("当前系统不支持（需要 Windows 11）。")
                return
            # 立刻取一次，不然要等到下次启动才看到效果
            self._sample_titlebar_color()
        else:
            dwm.reset(self)
            # 标题栏交还系统了，图标也要跟着回到系统主题——
            # 否则深色界面配白图标、浅色系统标题栏配白图标，又是一片糊。
            self._set_theme_icon(dwm.system_uses_dark())
        # 开关关闭时 _start_theme_watch 内部会停掉定时器
        self._start_theme_watch()
