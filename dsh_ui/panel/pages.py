"""面板的六个页面。每页一个类，`build()` 只跑一次，之后靠 `refresh()`。

页面之间不互相引用：需要跳转就通过 `PanelHost` 暴露的动作。
"""
from __future__ import annotations
from .styles import *          # noqa: F401,F403
from .styles import _css, _label  # noqa: F401  (import * 不带下划线名)
from .widgets import *         # noqa: F401,F403
from .workers import *         # noqa: F401,F403
from .host import *            # noqa: F401,F403  (guide_links)

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

import dsh_icons as icons
from dsh_host import (compat, config, contract, dwm, netenv, procmem, provision,
                      remote, storage, updater)
from dsh_host.logutil import host_log
from dsh_host.version import HOST_NAME, HOST_REPO, HOST_VERSION


class BasePage(QWidget):
    """页面基类。`on_show()` 在每次被切到时调用（含懒创建后的第一次）。

    **内容放在 QScrollArea 里**，这是被真机反馈逼出来的：
    窗口是固定 600 高的，页面内容一旦超出，QVBoxLayout 会把子项压到**低于它们
    的最小高度**——表现是按钮被压扁、文字被裁。加滚动区之后内容再多也只是出现
    滚动条，不会挤压任何控件。
    """

    def __init__(self, host: PanelHost, pal: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("pageScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 原生条一律关掉：常驻可见且占内容宽度，改用下面的浮动指示条。
        # 关掉只影响"条显不显示"，**滚轮照常能滚**。
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 视口默认会用调色板的 Base 色填底，在深色主题下会露出一块浅色。
        # 全局样式表里的 `QWidget { background: transparent }` 已经覆盖了它，
        # 这里再关掉自动填充，双保险。
        self.scroll.viewport().setAutoFillBackground(False)

        self.content = QWidget()
        self.content.setObjectName("pageContent")
        self.content.setAutoFillBackground(False)
        lay = QVBoxLayout(self.content)
        # 内容留白放大：上一版被指出"文本与容器边界太近"
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(14)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll)

        #: 浮动滚动指示条（浮在视口上，不占宽度）
        self.hint = ScrollHint(self.scroll, pal)

        self.layout_ = lay
        self.host = host
        self.pal = pal
        self._built = False

    def on_show(self) -> None:
        if not self._built:
            self._built = True
            self.build()
            self._fit_buttons()

    def on_hide(self) -> None:
        """页面被切走或面板收起时调用。有周期任务的页面覆盖它。

        默认什么都不做——只有真正在后台持续花钱的页面才需要停，
        比如概览页的内存采样线程。
        """

    def _fit_buttons(self) -> None:
        """给按钮一个可靠的下限尺寸。

        真机反馈过"按钮大小不合理、文字被内边距遮盖"。根因是内容超出固定窗口
        高度时布局把子项压到低于最小高度——**加滚动区是主修**（见类文档），
        这里再给一道保险，顺带让同一排按钮高度一致，观感也整齐。

        （不用 QSS 的 min-height：那是内容区下限，padding 与 border 会再叠上去，
        算出来的总高比预期大一截，反而更难看。）
        """
        for b in self.findChildren(QPushButton):
            if b.objectName() in ("action", "primary"):
                b.setMinimumHeight(30)
                b.setMinimumWidth(76)

    def build(self) -> None:                                       # pragma: no cover
        pass

    def apply_palette(self, pal: dict) -> None:
        self.pal = pal
        self.hint.set_palette_colors(pal)

    def shutdown(self, timeout_ms: int = 8000) -> bool:
        """收尾钩子。有后台线程的页面覆盖它。返回是否收尾干净。"""
        return True

class OverviewPage(BasePage):
    """概览：状态一眼看清 + 更新与外观设置。

    托盘 menu 里那些**设置**性质的项搬到了这里，托盘只留必要动作。
    """

    FLAGS = (
        ("prerelease_opt_in", "加入预览计划",
         "跟随官方的候选与尝鲜版本线。官方明示预览版可能含破坏性变更。"),
        ("adaptive_titlebar", "标题栏跟随界面配色",
         "让窗口标题栏跟随 DSH 界面的主色。需要 Windows 11。"),
    )

    def build(self) -> None:
        self.layout_.addWidget(_label("概览", "h1"))

        inst = self.host.install()
        self.kv_shell = KeyValueRow(f"{HOST_NAME}版本", HOST_VERSION, self.pal)
        self.kv_dsh = KeyValueRow("DSH 版本",
                                  inst.version if inst else "未检测", self.pal)
        self.kv_src = KeyValueRow("DSH 来源", inst.source if inst else "—", self.pal)
        self.kv_svc = KeyValueRow("后台服务", "—", self.pal)
        c1 = Card("当前状态", self.pal)
        for w in (self.kv_shell, self.kv_dsh, self.kv_src, self.kv_svc):
            c1.add(w)
        self.layout_.addWidget(c1)

        self.layout_.addWidget(self._build_mem_card())

        # 只留一个主动作。更新、外观、开发者、数据目录都搬去了「常规」——
        # 以前这些全堆在概览里，用户想看个端口都得先划过一屏设置（2026-10-20 重排）。
        c2 = Card("进入工作", self.pal)
        c2.add_text("更新与外观、开发者选项、数据目录都在「常规」页。")
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(16, 4, 16, 10)
        row.setSpacing(8)
        btn_web = QPushButton("打开 DSH 界面")
        btn_web.setObjectName("primary")
        btn_web.clicked.connect(lambda: self.host.open_web())
        row.addWidget(btn_web)
        row.addStretch(1)
        c2.add(holder)
        self.layout_.addWidget(c2)

        self.layout_.addStretch(1)
        self.refresh()

    # ---- 内存占用（2026-11-12）----
    #
    # 老大在任务管理器里数内存时被迫"到处找再相加"：壳、QtWebEngineProcess、
    # node 的映像名各不相同，任务管理器按映像名分行；而全机的 node.exe 还混着
    # 别的产品。这里按**进程树**归拢成三块（壳 / 网页 / 服务），和 mem.log
    # 用的是同一套口径，省得界面和日志各说一套。
    #
    # 只显示**私有内存**：工作集会被系统随时裁剪（最小化就掉到十几 MB），
    # 拿它加总既不准，也会让人误判"内存被释放了"。

    def _build_mem_card(self) -> Card:
        card = Card(
            "内存占用", self.pal,
            help_text="只统计私有内存：任务管理器默认那列是工作集，"
                      "窗口最小化时会掉到几十 MB，看着像释放了，实际没有。\n"
                      "「主进程」含界面与浏览器内核；「网页」是渲染进程；"
                      "「后台服务」是 DSH 自己拉起的 Node 进程。")

        # 合计单独一行，但**必须和下面三块走同一条左对齐线**。
        # 第一版把合计做成右对齐的大字，结果数字一左一右，
        # 看起来像两张表拼在一起（2026-11-12 截图核对当场改掉）。
        # 层次靠字重而不是靠位置：合计加粗，位置与分项一致。
        self.kv_mem_total = KeyValueRow("合计", "—", self.pal)
        self.kv_mem_total.v.setObjectName("kv_v_strong")
        self.kv_mem_total.v.style().unpolish(self.kv_mem_total.v)
        self.kv_mem_total.v.style().polish(self.kv_mem_total.v)
        card.add(self.kv_mem_total)

        self.mem_rows: dict[str, KeyValueRow] = {}
        for key, title in (("host", "主进程"), ("webengine", "网页"),
                           ("node", "后台服务")):
            r = KeyValueRow(title, "—", self.pal)
            self.mem_rows[key] = r
            card.add(r)
        # 「其他」平时是 0，只在真有不归类的子进程时才出现，免得常年挂一行 0 MB
        self.mem_row_other = KeyValueRow("其他", "—", self.pal)
        self.mem_row_other.setVisible(False)
        card.add(self.mem_row_other)

        # 说明压成一行：概览页固定 600 高，多一行就把下面的「进入工作」挤出首屏，
        # 而那句提示又必须留在界面上（工作集和私有内存混淆是真实发生过的误判）。
        self._mem_worker = None
        return card

    def _start_mem(self) -> None:
        w = getattr(self, "_mem_worker", None)
        if w is None:
            w = MemWorker(parent=self)
            w.sample.connect(self._apply_mem)
            self._mem_worker = w
        w.start_sampling()

    def _stop_mem(self, timeout_ms: int = 3000) -> bool:
        w = getattr(self, "_mem_worker", None)
        if w is None or not w.isRunning():
            return True
        return w.stop_sampling(timeout_ms)

    def _apply_mem(self, snap: dict) -> None:
        groups = snap.get("groups") or {}
        total = snap.get("total_private", 0.0)
        count = 1 + len(snap.get("rows") or [])
        self.kv_mem_total.set("%s　·　%d 个进程" % (human_mem(total), count))
        for key, row in self.mem_rows.items():
            g = groups.get(key) or {"private": 0.0, "count": 0}
            n = int(g.get("count") or 0)
            # 没进程时写 "0 MB" 而不是破折号：em dash 在这个字号下只有十几像素，
            # 缩略看跟空白没区别，会被当成"这行没渲染出来"（2026-11-12 亲眼看错一次）。
            if n <= 0:
                row.set("0 MB")
            elif n > 1:
                row.set("%s　·　%d 个进程" % (human_mem(g.get("private") or 0), n))
            else:
                row.set(human_mem(g.get("private") or 0))
        other = groups.get("other") or {"private": 0.0, "count": 0}
        n_other = int(other.get("count") or 0)
        self.mem_row_other.setVisible(n_other > 0)
        if n_other > 0:
            self.mem_row_other.set("%s　·　%d 个进程"
                                   % (human_mem(other.get("private") or 0), n_other))

    def refresh(self) -> None:
        inst = self.host.install()
        h = self.host.handle()
        self.kv_dsh.set(inst.version if inst else "未检测")
        self.kv_src.set(inst.source if inst else "—")
        if h and h.port:
            alive = contract.tcp_open(h.host or "127.0.0.1", h.port, timeout=1.0)
            self.kv_svc.set(f"运行中 · 端口 {h.port}" if alive
                            else f"已停止（端口 {h.port}）")
        else:
            self.kv_svc.set("未启动")
        for name, row in getattr(self, "toggles", {}).items():
            row.set_checked(self.host.flag(name))
        if hasattr(self, "btn_dev"):
            on = self.host.flag("dev_mode")
            for b in (self.btn_dev, self.btn_log):
                b.setEnabled(on)

    def on_show(self) -> None:
        if not self._built:
            super().on_show()
        else:
            self.refresh()
        self._start_mem()

    def on_hide(self) -> None:
        """切走就停采样：没人看的时候不该有线程在数内存。"""
        self._stop_mem()

    def shutdown(self, timeout_ms: int = 8000) -> bool:
        return self._stop_mem(timeout_ms)

    def apply_palette(self, pal: dict) -> None:
        super().apply_palette(pal)
        if not self._built:
            return
        for w in (self.kv_shell, self.kv_dsh, self.kv_src, self.kv_svc):
            w.set_palette_colors(pal)
        for w in list(getattr(self, "mem_rows", {}).values()) + \
                [getattr(self, "mem_row_other", None)]:
            if w is not None:
                w.set_palette_colors(pal)
        for c in self.findChildren(Card):
            c.set_palette_colors(pal)
        for r in getattr(self, "toggles", {}).values():
            r.set_palette_colors(pal)

class GeneralPage(BasePage):
    """常规：更新、外观、开发者、数据目录。

    为什么单独一页（2026-10-20 重排）
    --------------------------------
    这几组都是**设置**，不是"看一眼就走"的状态。以前全塞在概览里，
    概览就变成了"状态卡 + 四组设置"的大杂烩——用户只是想看个端口，
    却要先划过一屏开关和按钮。

    数据目录放在这一页的最下面：它属于"偶尔维护一次"的东西，
    但必须有个固定的、找得到的位置——因为删掉 exe 不会带走数据。
    """

    FLAGS = (
        ("prerelease_opt_in", "加入预览计划",
         "跟随官方的候选与尝鲜版本线。官方明示预览版可能含破坏性变更。"),
        ("adaptive_titlebar", "标题栏跟随界面配色",
         "让窗口标题栏跟随 DSH 界面的主色。需要 Windows 11。"),
        ("webengine_hw_accel", "硬件加速渲染（重启后生效）",
         "默认开启。本项即最终结果，程序不会自行更改。关闭后强制使用软件渲染，"
         "稳定性略高，CPU 占用明显增加。"),
    )

    #: 占用统计的复用期限（秒）。切回本页时先用缓存，过期才重算——
    #: 统计要遍历整棵树（见 UsageWorker），来回切页不该每次都付这个代价。
    USAGE_TTL = 30.0

    def build(self) -> None:
        self.layout_.addWidget(_label("常规", "h1"))
        self.layout_.addWidget(_label(
            "更新、外观、开发者选项，以及数据目录的位置与清理。"))

        # --- 更新与外观 ---
        c1 = Card("更新与外观", self.pal)
        self.toggles = {}
        for name, title, hint in self.FLAGS:
            if name == "adaptive_titlebar" and not dwm.available():
                continue
            row = ToggleRow(title, hint, self.host.flag(name), self.pal,
                            lambda on, n=name: self.host.set_flag(n, on))
            self.toggles[name] = row
            c1.add(row)
        self.layout_.addWidget(c1)

        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(16, 2, 16, 8)
        row.setSpacing(8)
        self.btn_update = QPushButton("检查 DSH 更新")
        self.btn_update.setObjectName("action")
        self.btn_update.clicked.connect(lambda: self.host.action("check_update"))
        self.btn_stable = QPushButton("回归稳定版")
        self.btn_stable.setObjectName("action")
        self.btn_stable.clicked.connect(lambda: self.host.action("go_stable"))
        self.btn_rollback = QPushButton("回滚到上一版本")
        self.btn_rollback.setObjectName("action")
        self.btn_rollback.clicked.connect(lambda: self.host.action("rollback"))
        for b in (self.btn_update, self.btn_stable, self.btn_rollback):
            row.addWidget(b)
        row.addStretch(1)
        self.layout_.addWidget(holder)

        # --- 开发者 ---
        c2 = Card("开发者", self.pal)
        self.toggles["dev_mode"] = ToggleRow(
            "开发者模式",
            "开启后直接打开 Chromium 自带的开发者工具"
            "（Elements / Console / Network / Sources 与浏览器完全一致），"
            "同时开始记录网络请求。",
            self.host.flag("dev_mode"), self.pal,
            lambda on: self.host.set_flag("dev_mode", on))
        c2.add(self.toggles["dev_mode"])

        dev_row = QWidget()
        dl = QHBoxLayout(dev_row)
        dl.setContentsMargins(16, 4, 16, 6)
        dl.setSpacing(8)
        self.btn_dev = QPushButton("开发者工具")
        self.btn_dev.setObjectName("action")
        self.btn_dev.setToolTip("Chromium 自带的完整开发者工具；"
                                "关闭窗口即释放它占用的渲染实例")
        self.btn_dev.clicked.connect(lambda: self.host.action("open_devtools"))
        self.btn_log = QPushButton("轻量日志")
        self.btn_log.setObjectName("action")
        self.btn_log.setToolTip("控制台与请求记录一直保留在内存里，"
                                "工具没开着的时候也能回看报错（布局页只是坐标快照）")
        self.btn_log.clicked.connect(lambda: self.host.action("open_devlog"))
        self.btn_dir = QPushButton("打开日志目录")
        self.btn_dir.setObjectName("action")
        self.btn_dir.clicked.connect(lambda: self.host.open_path(self.host.log_dir()))
        for b in (self.btn_dev, self.btn_log):
            b.setEnabled(self.host.flag("dev_mode"))
            if not self.host.flag("dev_mode"):
                b.setToolTip("先开启上方的开发者模式")
        self.toggles["dev_mode"].btn.toggled.connect(self.btn_dev.setEnabled)
        self.toggles["dev_mode"].btn.toggled.connect(self.btn_log.setEnabled)
        for b in (self.btn_dev, self.btn_log, self.btn_dir):
            dl.addWidget(b)
        dl.addStretch(1)
        c2.add(dev_row)
        self.layout_.addWidget(c2)

        # --- 数据目录 ---
        c3 = Card(
            "数据目录", self.pal,
            help_text="设置、登录状态、日志、私有安装的 DSH 都在这里。"
                      "删除程序本身不会移除这些数据；更换设备或重装前，"
                      "可在此迁移到别处或清空。")
        self.card_data = c3
        self.kv_root = KeyValueRow("位置", self.host.data_dir(), self.pal)
        self.kv_usage = KeyValueRow("占用", "统计中…", self.pal)
        c3.add(self.kv_root)
        c3.add(self.kv_usage)
        self.group_rows: list[KeyValueRow] = []
        dir_row = QWidget()
        dr = QHBoxLayout(dir_row)
        dr.setContentsMargins(16, 4, 16, 10)
        dr.setSpacing(8)
        self.btn_open_dir = QPushButton("打开位置")
        self.btn_open_dir.setObjectName("action")
        self.btn_open_dir.clicked.connect(
            lambda: self.host.open_path(self.host.data_dir()))
        self.btn_move = QPushButton("迁移到其他磁盘…")
        self.btn_move.setObjectName("action")
        self.btn_move.clicked.connect(lambda: self.host.action("migrate_data"))
        self.btn_clear = QPushButton("清理缓存")
        self.btn_clear.setObjectName("action")
        self.btn_clear.setToolTip("只清浏览器缓存与日志，保留设置和登录态")
        self.btn_clear.clicked.connect(lambda: self.host.action("clear_cache"))
        self.btn_wipe = QPushButton("清空全部数据…")
        self.btn_wipe.setObjectName("action")
        self.btn_wipe.setToolTip("删除全部数据，回到首次运行的状态")
        self.btn_wipe.clicked.connect(lambda: self.host.action("clear_all"))
        for b in (self.btn_open_dir, self.btn_move, self.btn_clear, self.btn_wipe):
            dr.addWidget(b)
        dr.addStretch(1)
        c3.add(dir_row)
        self.layout_.addWidget(c3)

        self.layout_.addStretch(1)
        self._usage = None            # 最近一次统计结果
        self._usage_at = 0.0          # 拿到结果时的 time.monotonic()
        self._usage_worker = None
        self.refresh()

    def refresh(self) -> None:
        for name, row in self.toggles.items():
            row.set_checked(self.host.flag(name))
        on = self.host.flag("dev_mode")
        for b in (self.btn_dev, self.btn_log):
            b.setEnabled(on)
        self._refresh_usage()

    def _refresh_usage(self) -> None:
        """占用统计：缓存新鲜就直接渲染，否则丢给后台线程。

        **绝不在界面线程里同步调 `storage.breakdown()`**——那是秒级的全树遍历，
        以前就是它让"切进常规 tab"卡十几秒（见 UsageWorker）。
        """
        if (self._usage is not None
                and time.monotonic() - self._usage_at < self.USAGE_TTL):
            self._render_usage(self._usage)
            return
        w = self._usage_worker
        if w is not None and w.isRunning():
            return            # 已经在算了，算完自己会回填
        if w is not None:
            w.deleteLater()   # 不释放的话，每次 TTL 到期都多一个 QThread 常驻
        if self._usage is None:
            self.kv_usage.set("统计中…")
        self._usage_worker = UsageWorker(self.host.usage, self)
        self._usage_worker.done.connect(self._on_usage)
        self._usage_worker.start()

    def _on_usage(self, info) -> None:
        if not isinstance(info, dict) or "error" in info:
            msg = info.get("error") if isinstance(info, dict) else str(info)
            self.kv_usage.set("统计失败")
            self.kv_usage.setToolTip(str(msg))
            self._usage_at = 0.0        # 下次进来重试
            return
        self._usage = info
        self._usage_at = time.monotonic()
        self.kv_usage.setToolTip("")
        self._render_usage(info)

    def _render_usage(self, info: dict) -> None:
        self.kv_root.set(info.get("root") or self.host.data_dir())
        partial = bool(info.get("partial"))
        # 数字一律粗粒度：文件数上万就按"万"报（见 storage.str_of_count）。
        # 用户的原话是"不需要那么精细"，而 66447 这种精度对"占多大"没有帮助。
        text = "%s · %s 个文件" % (storage.str_of_bytes(info["bytes"]),
                                  storage.str_of_count(info["files"]))
        if partial:
            text = "≥ " + text
            self.kv_usage.setToolTip(
                "文件太多，统计到预算上限就停了（结果偏小）。数字够判断大小了，"
                "需要精确值可以在资源管理器里看目录属性。")
        else:
            self.kv_usage.setToolTip("")
        self.kv_usage.set(text)
        # 明细行重建（组数很少，重建比增删维护简单）
        for r in self.group_rows:
            r.setParent(None)
            r.deleteLater()
        self.group_rows = []
        card = getattr(self, "card_data", None)
        for g in info.get("groups", ()):
            r = KeyValueRow("　" + g["label"],
                            "%s · %s 个文件"
                            % (storage.str_of_bytes(g["bytes"]),
                               storage.str_of_count(g["files"])),
                            self.pal)
            r.setToolTip(g["hint"])
            if card is not None and hasattr(card, "add"):
                card.add(r)
            self.group_rows.append(r)

    def on_show(self) -> None:
        if not self._built:
            super().on_show()          # build() 里已经调过 refresh()
        else:
            self.refresh()

    def shutdown(self, timeout_ms: int = 8000) -> bool:
        """等后台统计线程收尾（QThread 活着退出会让 Qt abort）。"""
        w = self._usage_worker
        if w is not None and w.isRunning() and not w.wait(timeout_ms):
            return False
        return True

    def apply_palette(self, pal: dict) -> None:
        super().apply_palette(pal)
        if not self._built:
            return
        for w in (self.kv_root, self.kv_usage):
            w.set_palette_colors(pal)
        for c in self.findChildren(Card):
            c.set_palette_colors(pal)
        for r in self.toggles.values():
            r.set_palette_colors(pal)

class DeployPage(BasePage):
    """本机部署：**引导**，不代劳。

    为什么从"帮你装"改成"告诉你怎么装"
    ----------------------------------
    替用户装 Node / 装 DSH，意味着我们要跟着上游的下载地址、包名、安装参数一起改，
    上游一动我们就得重新打包发版。改成引导后，网址与命令放在适配表里（可外部覆盖），
    我们只负责**判断每一步成没成**——那是我们自己的契约，不会因为上游换网址而失效。
    """

    def build(self) -> None:
        self.layout_.addWidget(_label("本机部署", "h1"))
        self.layout_.addWidget(_label(
            "这些步骤需要你手动执行；完成后回到「状态检测」页重新检测即可确认。"))

        # 代理提示（2026-10-20）：只在真的有风险时出现——设了代理、且
        # DeepSeek 域名没排除。这时账号功能会"半坏不坏"，用户不可能自己想到。
        _hint = netenv.proxy_hint()
        if _hint:
            _c = Card("网络代理可能影响账号功能", self.pal)
            _c.add_text(_hint)
            self.layout_.addWidget(_c)

        links = guide_links()
        c1 = Card("第一步 · 安装 Node.js 运行环境", self.pal)
        c1.add(KeyValueRow("状态", "见「状态检测」页的 Node.js 一行", self.pal))
        c1.add(self._link_row("下载页", links["node_download"]))
        c1.add(self._copy_row("命令行安装", links["node_winget"]))
        self.layout_.addWidget(c1)

        c2 = Card("第二步 · 安装 DeepSeek Harness", self.pal)
        c2.add(self._copy_row("命令行安装", f"npm install -g {updater.PKG_NAME}"))
        c2.add(self._link_row("项目主页", links["dsh_site"]))
        c2.add(self._link_row("使用文档", links["dsh_docs"]))
        self.layout_.addWidget(c2)

        # 这条原本是托盘里的「准备运行环境…」。挪过来而不是删掉——能力还在，
        # 只是从"日常入口"降级成"不想自己装时的兜底"，且必须点进这一页才看得到。
        c3 = Card(
            "自动安装", self.pal,
            help_text="由本程序自动安装 Node.js 与 DeepSeek Harness，"
                      "装到应用数据目录，与命令行那份互不影响。\n"
                      "需要联网，过程中会显示进度窗口。")
        c3.add_text(f"可以让{HOST_NAME}代劳：自动安装 Node.js 与 DeepSeek Harness。"
                    "装到应用自己的数据目录（和命令行里那份互不影响）。"
                    "需要联网，中途会弹出进度窗口。")
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(16, 4, 16, 10)
        btn = QPushButton("自动准备运行环境")
        btn.setObjectName("action")
        btn.clicked.connect(lambda: self.host.action("provision"))
        lay.addWidget(btn)
        lay.addStretch(1)
        c3.add(holder)
        self.layout_.addWidget(c3)

        # 修复入口。为什么放在部署页而不是概览页：它不是日常动作，
        # 只在"更新过、但界面不对劲"时才需要——那种场景用户第一反应就是
        # 跑到部署页找重装。
        c4 = Card(
            "修复版本不一致", self.pal,
            help_text="更新后若部分组件仍停留在旧版本，会出现接口报错、"
                      "账号信息无法加载等症状，版本号看起来却是正常的。\n"
                      "重新安装会重装全部组件，会话记录、登录状态与设置不受影响。")
        c4.add_text("若出现接口报错或账号信息无法加载，通常是部分组件仍停留在旧版本。"
                    "重新安装即可修复，会话记录、登录状态与设置均不受影响。")
        holder2 = QWidget()
        lay2 = QHBoxLayout(holder2)
        lay2.setContentsMargins(16, 4, 16, 10)
        btn2 = QPushButton("重新安装 DSH")
        btn2.setObjectName("action")
        btn2.clicked.connect(lambda: self.host.action("reinstall_dsh"))
        lay2.addWidget(btn2)
        lay2.addStretch(1)
        c4.add(holder2)
        self.layout_.addWidget(c4)

        self.layout_.addStretch(1)

    def _link_row(self, title: str, url: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(10)
        t = QLabel(title)
        t.setObjectName("kv_k")
        t.setFixedWidth(128)
        v = QLabel(url or "（未配置）")
        v.setObjectName("kv_v")
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.setWordWrap(True)
        btn = QPushButton("打开")
        btn.setObjectName("action")
        btn.setEnabled(bool(url))
        btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        lay.addWidget(t)
        lay.addWidget(v, 1)
        lay.addWidget(btn)
        return w

    def _copy_row(self, title: str, cmd: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(10)
        t = QLabel(title)
        t.setObjectName("kv_k")
        t.setFixedWidth(128)
        v = QLabel(cmd or "（未配置）")
        v.setObjectName("kv_v")
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.setWordWrap(True)
        btn = QPushButton("复制")
        btn.setObjectName("action")
        # 图标显式给尺寸：默认 iconSize 会取样式建议值，和 13px 文字放一起偏高
        btn.setIconSize(QSize(14, 14))
        btn.setIcon(icons.qicon("copy", self.pal["dim"], 14))
        btn.setEnabled(bool(cmd))
        btn.clicked.connect(lambda: self._copy(cmd, btn))
        lay.addWidget(t)
        lay.addWidget(v, 1)
        lay.addWidget(btn)
        return w

    def _copy(self, text: str, btn: QPushButton) -> None:
        QGuiApplication.clipboard().setText(text)
        # 就地反馈，不弹对话框——复制这类动作不该打断用户
        old = btn.text()
        btn.setText("已复制")
        QTimer.singleShot(1200, btn, lambda: btn.setText(old))

class RunModePage(BasePage):
    """运行方式：私有 / 公共 / 远程。

    为什么要能选（2026-10-20）
    ------------------------
    复用用户已有的 DSH 是我们的优势（不重复占磁盘、版本和命令行一致），
    但它有个副作用：**和命令行的 npm 抢同一个 node_modules**。
    2026-10-20 那次"版本号正常、界面却一堆接口 404"，根因就是 npm 只升了主包，
    58 个精确版本的子依赖还留在旧版。

    「私有」把这份安装搬进应用自己的数据目录，与系统完全隔离；
    「公共」保持现在的行为。选择权交给用户：在意磁盘就共用，在意稳定就独立。
    """

    def build(self) -> None:
        self.layout_.addWidget(_label("运行方式", "h1"))
        self.layout_.addWidget(_label(
            "选择本程序使用的 DSH 安装。切换会重启后台服务，"
            "会话记录与登录状态不受影响。"))

        self.kv_mode = KeyValueRow("当前使用", "—", self.pal)
        self.kv_ver = KeyValueRow("实际版本", "—", self.pal)
        c0 = Card("现在", self.pal)
        c0.add(self.kv_mode)
        c0.add(self.kv_ver)
        self.layout_.addWidget(c0)

        # --- 远程 ---
        # 放在"私有/公共"之前：那两个是"用哪一份 DSH 安装"，属于本地细节；
        # 远程是"用哪台机器"，是同一层级的顶层选择，而且日常使用频率最高
        # （配好之后每次只是连一下）。放最后要滚两屏才够到，等于藏起来。
        self.layout_.addWidget(self._build_remote_card())
        self._rebuild_hosts()

        # --- 私有 ---
        c1 = Card(
            "私有安装（推荐）", self.pal,
            help_text="本程序独占管理这份 DSH，升级只作用于它，"
                      "不会因为终端里的 npm 操作变成「主程序升了、组件还在旧版」。\n"
                      "代价是多占用一份磁盘空间。")
        self.kv_priv = KeyValueRow("状态", "—", self.pal)
        c1.add(self.kv_priv)
        c1.add_text("独立安装在应用数据目录，与命令行那份互不影响。")
        h1 = QWidget()
        r1 = QHBoxLayout(h1)
        r1.setContentsMargins(16, 4, 16, 10)
        r1.setSpacing(8)
        self.btn_private = QPushButton("启用私有")
        self.btn_private.setObjectName("primary")
        self.btn_private.clicked.connect(lambda: self.host.action("use_private"))
        r1.addWidget(self.btn_private)
        r1.addStretch(1)
        c1.add(h1)
        self.layout_.addWidget(c1)

        # --- 公共 ---
        c2 = Card(
            "公共安装（与命令行共用）", self.pal,
            help_text="它与终端里的那份 DSH 是同一套文件。你在终端执行升级时，"
                      "这里的组件也会随之变化；两边同时改动同一份文件，"
                      "可能出现版本不一致，表现为界面接口报错。")
        self.kv_pub = KeyValueRow("状态", "—", self.pal)
        c2.add(self.kv_pub)
        c2.add_text("使用命令行里的那份 DSH，不额外占用磁盘。")
        h2 = QWidget()
        r2 = QHBoxLayout(h2)
        r2.setContentsMargins(16, 4, 16, 10)
        r2.setSpacing(8)
        self.btn_public = QPushButton("启用公共")
        self.btn_public.setObjectName("action")
        self.btn_public.clicked.connect(lambda: self.host.action("use_system"))
        r2.addWidget(self.btn_public)
        r2.addStretch(1)
        c2.add(h2)
        self.layout_.addWidget(c2)

        self.layout_.addStretch(1)
        self.refresh()

    # ---- 远程主机（2026-11-12）----
    #
    # 面板在这里只做三件事：列出保存过的主机、发"连/断"的指令、显示主窗口推过来的
    # 状态。**真正的 SSH 连接在主窗口**——面板不该自己去起隧道，否则自测要在
    # 没有 SSH 的机器上跑不动，退出时也没人收尾。

    def _build_remote_card(self) -> Card:
        # 标"实验"是必须的：到发这一版为止，SSH 那条链路只验证到"连不上时的
        # 错误分类是对的"，"复用远端已有实例"还没在真机上跑过（本机的 MSYS
        # 判不准进程身份）。不标的话，用户会以为是成熟功能。
        card = Card(
            "远程 DSH（实验）", self.pal,
            help_text="断开会话后，远端 dsh web 的处置按这台主机的设置执行："
                      "保留（适合跑长任务的服务器）、停止（适合临时连一次的机器），"
                      "或每次询问。\n"
                      "认证使用系统 SSH 的密钥或 agent，本程序不保存密码。\n"
                      "DSH 始终只监听远端本机地址，端口由 SSH 隧道转发，不对外暴露。")
        self.kv_remote = KeyValueRow("当前连接", "未连接", self.pal)
        card.add(self.kv_remote)

        self._hosts_box = QWidget()
        self._hosts_lay = QVBoxLayout(self._hosts_box)
        self._hosts_lay.setContentsMargins(0, 2, 0, 2)
        self._hosts_lay.setSpacing(0)
        card.add(self._hosts_box)

        self.lbl_remote_empty = QLabel("还没有配置主机。")
        self.lbl_remote_empty.setStyleSheet(Template(
            "color: $dim; font-family: $font; font-size: 12px;"
            " padding: 4px 16px 8px 16px;"
        ).substitute(dim=self.pal["dim"], font=FONT))
        card.add(self.lbl_remote_empty)

        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(16, 4, 16, 10)
        row.setSpacing(8)
        self.btn_add_host = QPushButton("添加主机")
        self.btn_add_host.setObjectName("primary")
        self.btn_add_host.clicked.connect(self._add_remote_host)
        self.btn_disconnect = QPushButton("断开")
        self.btn_disconnect.setObjectName("action")
        self.btn_disconnect.clicked.connect(lambda: self.host.remote_disconnect())
        self.btn_disconnect.setEnabled(False)
        row.addWidget(self.btn_add_host)
        row.addWidget(self.btn_disconnect)
        row.addStretch(1)
        card.add(holder)

        card.add_text("远端服务的去留按主机设置执行。DSH 只监听远端本机，"
                      "端口经 SSH 隧道转发。")
        return card

    def _rebuild_hosts(self) -> None:
        while self._hosts_lay.count():
            item = self._hosts_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._remote_rows = {}
        hosts = []
        try:
            hosts = list(self.host.remote_hosts() or [])
        except Exception:                                       # noqa: BLE001
            hosts = []
        for h in hosts:
            row = RemoteHostRow(h, self.pal, self._toggle_remote,
                                self._edit_remote_host)
            self._remote_rows[h.id] = row
            self._hosts_lay.addWidget(row)
        self.lbl_remote_empty.setVisible(not hosts)
        self._apply_remote_state()

    def _apply_remote_state(self) -> None:
        state = {}
        try:
            state = self.host.remote_state() or {}
        except Exception:                                       # noqa: BLE001
            state = {}
        cur = state.get("host_id") or ""
        stage = state.get("stage") or ""
        err = state.get("error") or ""
        connected = bool(state.get("connected"))
        self.kv_remote.set(
            ("已连接：" + state.get("name", "")) if connected
            else (stage or ("失败：" + err.splitlines()[0][:60] if err else "未连接")))
        # 断开只在"真的连上了"才可点：连接过程中打断会留下半开的隧道，
        # 而我们没法在半路取消一个正在阻塞的 SSH 调用。
        self.btn_disconnect.setEnabled(connected)
        for hid, row in getattr(self, "_remote_rows", {}).items():
            if hid == cur:
                row.set_state({"connected": connected, "stage": stage})
            else:
                row.set_state({})

    def refresh_remote(self) -> None:
        """主窗口推送远程状态变化时调用。

        只更新文案、不重建主机行——重建会 deleteLater 掉正在点的那个按钮，
        用户会觉得"点了一下界面闪了一下"。
        """
        if getattr(self, "_remote_rows", None) is None:
            return
        self._apply_remote_state()

    def _toggle_remote(self, host_obj) -> None:
        state = {}
        try:
            state = self.host.remote_state() or {}
        except Exception:                                       # noqa: BLE001
            state = {}
        if state.get("host_id") == host_obj.id and (state.get("connected")
                                                    or state.get("stage")):
            self.host.remote_disconnect()
            return
        self.host.remote_connect(host_obj)
        QTimer.singleShot(200, self._apply_remote_state)

    def _add_remote_host(self) -> None:
        dlg = RemoteHostDialog(self.pal, aliases=remote.ssh_config_aliases(),
                               parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_host is None:
            dlg.deleteLater()
            return
        hosts = list(self.host.remote_hosts() or [])
        hosts.append(dlg.result_host)
        dlg.deleteLater()
        self.host.remote_save(hosts)
        self._rebuild_hosts()

    def _edit_remote_host(self, host_obj) -> None:
        dlg = RemoteHostDialog(self.pal, host=host_obj,
                               aliases=remote.ssh_config_aliases(), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_host is None:
            dlg.deleteLater()
            return
        new = dlg.result_host
        dlg.deleteLater()
        hosts = []
        for h in (self.host.remote_hosts() or []):
            hosts.append(new if h.id == host_obj.id else h)
        self.host.remote_save(hosts)
        self._rebuild_hosts()

    def refresh(self) -> None:
        mode = str(config.get("dsh_source") or "auto")
        label = {"private": "私有安装", "system": "公共安装（命令行那份）"}.get(
            mode, "自动（私有优先）")
        self.kv_mode.set(label)
        inst = self.host.install()
        self.kv_ver.set(("%s · %s" % (inst.version, inst.source)) if inst else "未检测")

        if provision.private_ready():
            self.kv_priv.set("已安装 · %s" % provision.private_version())
        else:
            self.kv_priv.set("未安装")
        self.btn_private.setText("正在使用" if mode == "private" else "启用私有")
        self.btn_private.setEnabled(mode != "private")

        system_hint = "未检测到"
        for src, path, _node in contract.candidate_bin_js(prefer="system"):
            system_hint = "%s · %s" % (src, os.path.basename(path))
            break
        self.kv_pub.set(system_hint)
        self.btn_public.setText("正在使用" if mode == "system" else "启用公共")
        self.btn_public.setEnabled(mode != "system")

        self._apply_remote_state()

    def on_show(self) -> None:
        if not self._built:
            super().on_show()
        else:
            self.refresh()

    def apply_palette(self, pal: dict) -> None:
        super().apply_palette(pal)
        if not self._built:
            return
        for w in (self.kv_mode, self.kv_ver, self.kv_priv, self.kv_pub):
            w.set_palette_colors(pal)
        for row in getattr(self, "_remote_rows", {}).values():
            row.set_palette_colors(pal)
        for c in self.findChildren(Card):
            c.set_palette_colors(pal)

class StatusPage(BasePage):
    """状态检测：只报告，不修复。"""

    #: 自动检测的复用期限（秒）。手动点「重新检测」不受它影响，永远真跑。
    CHECKS_TTL = 30.0

    def build(self) -> None:
        head_w = QWidget()
        head = QHBoxLayout(head_w)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(_label("状态检测", "h1"))
        head.addStretch(1)
        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.setObjectName("primary")
        self.btn_recheck.setIcon(
            icons.qicon("refresh-cw", self.pal["primary_text"], 15))
        self.btn_recheck.clicked.connect(self.run_checks)
        self.btn_channel = QPushButton("查询官方通道")
        self.btn_channel.setObjectName("action")
        self.btn_channel.clicked.connect(self.run_channel)
        head.addWidget(self.btn_channel)
        head.addWidget(self.btn_recheck)
        self.layout_.addWidget(head_w)

        labels = dict(CHECK_ROWS)
        self.rows: dict[str, CheckRow] = {}
        self.cards: list[Card] = []
        for gname, keys in (("运行环境", ("node", "npm", "dsh")),
                            ("数据", ("home", "home_w")),
                            ("服务与版本", ("svc", "channel"))):
            card = Card(gname, self.pal)
            for k in keys:
                row = CheckRow(k, labels[k], self.pal)
                self.rows[k] = row
                card.add(row)
            self.cards.append(card)
            self.layout_.addWidget(card)
        self.layout_.addStretch(1)

        self._worker = None
        self._chan_worker = None
        self._checked_at = 0.0        # 上次自动检测完成的时刻（monotonic）

    def on_show(self) -> None:
        if not self._built:
            super().on_show()
        self.run_checks(auto=True)

    def run_checks(self, auto: bool = False) -> None:
        """跑一遍本地检测。`auto=True` 是"切到本页顺带看一眼"，走缓存。

        为什么要有缓存（2026-10-20 体检）：一次检测会 spawn 一次
        `node --version`（实测 65ms）并对 DSH_HOME 做全树指纹
        （上限 6 万条 / 20 秒）。而它挂在 `on_show()` 上——用户来回切页签
        就反复重跑，纯属白费。手动点「重新检测」永远是**真跑**。
        """
        if auto and self._checked_at and \
                time.monotonic() - self._checked_at < self.CHECKS_TTL:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        for k in LOCAL_KEYS:
            self.rows[k].dot.set_state(STATE_UNKNOWN)
            self.rows[k].value_lbl.setText("检测中…")
        self.btn_recheck.setEnabled(False)
        old = self._worker
        if old is not None:
            old.deleteLater()    # 不释放的话，每次重检都多一个永不回收的 QThread
        self._worker = ChecksWorker(self.host, self)
        self._worker.done.connect(self._on_checks)
        self._worker.start()

    def _on_checks(self, results: list) -> None:
        for res in results:
            row = self.rows.get(res.key)
            if row is None:
                continue     # 例如 "error" 这种额外结果，界面暂不展示
            row.apply(res)
        self._checked_at = time.monotonic()
        self.btn_recheck.setEnabled(True)

    def run_channel(self) -> None:
        """查官方通道。联网，所以单独一个按钮，不跟着自动检测跑。"""
        if self._chan_worker is not None and self._chan_worker.isRunning():
            return
        row = self.rows["channel"]
        row.dot.set_state(STATE_UNKNOWN)
        row.value_lbl.setText("查询中…")
        self.btn_channel.setEnabled(False)
        old = self._chan_worker
        if old is not None:
            old.deleteLater()
        self._chan_worker = ChannelWorker(self.host.install(), self)
        self._chan_worker.done.connect(self._on_channel)
        self._chan_worker.start()

    def _on_channel(self, res) -> None:
        self.rows["channel"].apply(res)
        self.btn_channel.setEnabled(True)

    def shutdown(self, timeout_ms: int = 8000) -> bool:
        """等后台线程跑完。

        为什么必须显式等：QThread 在解释器退出时还活着会让 Qt 直接 abort，
        而 abort 会**丢掉未刷出的 stdout 缓冲**——表现是"脚本没有任何输出、
        退出码非零"，排查时极具误导性（真因在 C++ 层）。CI 也会因此判失败。

        注意 `QThread.quit()` 只退事件循环，**打断不了 run() 里的同步代码**，
        所以这里用 `wait()` 等它自己跑完。
        """
        ok = True
        for attr in ("_worker", "_chan_worker"):
            w = getattr(self, attr, None)
            if w is not None and w.isRunning() and not w.wait(timeout_ms):
                ok = False
        return ok

    def apply_palette(self, pal: dict) -> None:
        super().apply_palette(pal)
        if not self._built:
            return
        for c in self.cards:
            c.set_palette_colors(pal)
        for r in self.rows.values():
            r.set_palette_colors(pal)
        self.btn_recheck.setIcon(icons.qicon("refresh-cw", pal["primary_text"], 15))

class AboutPage(BasePage):
    """关于。"""

    def build(self) -> None:
        self.layout_.addWidget(_label("关于", "h1"))
        inst = self.host.install()
        rel = self.host.host_release()

        c = Card("版本", self.pal)
        c.add(KeyValueRow(HOST_NAME, HOST_VERSION, self.pal))
        c.add(KeyValueRow("DSH", inst.version if inst else "未检测", self.pal))
        c.add(KeyValueRow("可更新到", rel or "（未查询）", self.pal))
        self.layout_.addWidget(c)

        c2 = Card("来源与许可", self.pal)
        c2.add(KeyValueRow("项目仓库", HOST_REPO, self.pal))
        c2.add(KeyValueRow("DSH 依赖", updater.PKG_NAME, self.pal))
        c2.add_text("界面图标来自 Lucide（ISC 许可），已内联为源码，"
                    "运行时不读外部文件。")
        c2.add_text("配色取自 DSH 官方前端的语义令牌，跟随界面主题切换。")
        self.layout_.addWidget(c2)

        c3 = Card("目录", self.pal)
        c3.add(KeyValueRow("数据根", config.data_root(), self.pal))
        c3.add(KeyValueRow("DSH_HOME",
                           contract.resolve_home() or "（由 DSH 自行决定）", self.pal))
        self.layout_.addWidget(c3)
        self.layout_.addStretch(1)


CHECK_ROWS = (
    ("node", "Node.js 运行环境"),
    ("npm", "npm 包管理器"),
    ("dsh", "DeepSeek Harness"),
    ("home", "用户数据目录"),
    ("home_w", "数据目录可写"),
    ("svc", "后台服务"),
    ("channel", "官方版本通道"),
)

LOCAL_KEYS = ("node", "npm", "dsh", "home", "home_w", "svc")
