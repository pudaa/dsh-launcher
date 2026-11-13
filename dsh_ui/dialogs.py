# -*- coding: utf-8 -*-
"""对话框：引导、更新、重装、数据任务。

为什么单独成文件（2026-11-12 拆分）
----------------------------------
都是"拦一下、说句话、点个按钮"的窗口，与主窗口只有 parent 关系。
放在主窗口旁边会让人以为它们是主窗口的一部分。
"""

from __future__ import annotations
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)

from dsh_ui import theme as dsh_theme
from dsh_host import config, contract, provision, selfupdate, updater
from dsh_host.logutil import host_log
from dsh_host.version import HOST_NAME, HOST_VERSION

from .workers import (DataTaskWorker, DiagnoseWorker, HostUpdateWorker,
                      NotesWorker, ProvisionWorker, UpdateWorker)


class OnboardingDialog(QDialog):
    """首次运行引导：把缺的运行环境补齐。

    文档要求：文案面向使用者。用户不需要知道 npm prefix、node_modules 是什么，
    这些只进日志区；正文只说"需要准备什么、要多久、能不能离开"。
    """

    def __init__(self, parent, report):
        super().__init__(parent)
        self.setWindowTitle("首次运行准备")
        self.setMinimumWidth(660)
        self.resize(700, 540)
        self.report = report
        self.succeeded = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(14)

        intro = QLabel("运行 DSH 需要先准备好运行环境。点「开始安装」，程序会自动完成。")
        intro.setObjectName("dlgBody")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        holder = QWidget()
        self.rows_box = QVBoxLayout(holder)
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(6)
        lay.addWidget(holder)

        self.hint = QLabel()
        self.hint.setObjectName("dlgHint")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("安装过程会显示在这里")
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.site_btn = QPushButton("打开 Node.js 官网")
        self.site_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(provision.NODE_WEBSITE)))
        self.recheck_btn = QPushButton("重新检测")
        self.recheck_btn.clicked.connect(self._recheck)
        self.go_btn = QPushButton("开始安装")
        self.go_btn.clicked.connect(self._start)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        for b in (self.site_btn, self.recheck_btn, self.go_btn, self.close_btn):
            row.addWidget(b)
        lay.addLayout(row)

        self.go_btn.setObjectName("primary")
        dsh_theme.style_dialog(self)
        self._render()

    # -------------------------------------------------- 渲染

    def _render(self):
        while self.rows_box.count():
            item = self.rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for r in self.report.rows():
            line = QWidget()
            h = QHBoxLayout(line)
            h.setContentsMargins(0, 0, 0, 0)
            name = QLabel(r["name"])
            name.setMinimumWidth(190)
            state = QLabel(r["text"])
            state.setStyleSheet("color: %s;" % ("#0F6E56" if r["ok"] else "#A32D2D"))
            h.addWidget(name)
            h.addWidget(state)
            h.addStretch(1)
            self.rows_box.addWidget(line)

        self.hint.setText(self.report.detail or "")
        self.site_btn.setVisible(bool(self.report.need_node))
        self.go_btn.setText("环境已就绪" if self.report.ready else "开始安装")
        self.go_btn.setEnabled(not self.report.ready)

    def _set_busy(self, busy: bool):
        for b in (self.go_btn, self.recheck_btn, self.close_btn):
            b.setEnabled(not busy)
        if not busy:
            self._render()

    # -------------------------------------------------- 动作

    def _start(self):
        self._set_busy(True)
        self.log.clear()
        self.log.appendPlainText("开始准备运行环境…")
        self.hint.setText("正在准备…")
        self._worker = ProvisionWorker()
        self._worker.line.connect(self.log.appendPlainText)
        self._worker.phase.connect(lambda _k, text: self.hint.setText(text))
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, msg: str):
        self.log.appendPlainText("\n" + msg)
        if ok:
            self.succeeded = True
            QTimer.singleShot(1200, self.accept)
            return
        self._set_busy(False)
        self._recheck()

    def _recheck(self):
        self._set_busy(True)
        self.recheck_btn.setText("检测中…")
        self._diag = DiagnoseWorker()
        self._diag.done.connect(self._on_recheck)
        self._diag.start()

    def _on_recheck(self, rep):
        self.report = rep
        self.recheck_btn.setText("重新检测")
        self._set_busy(False)
        if rep.ready:
            self.succeeded = True
            self.log.appendPlainText("环境已就绪，正在启动。")
            QTimer.singleShot(800, self.accept)

class UpdateDialog(QDialog):
    def __init__(self, parent, info: updater.UpdateInfo, handle):
        super().__init__(parent)
        self.setWindowTitle("DSH 更新")
        self.setMinimumWidth(620)
        self.resize(640, 460)
        self.info = info
        self.handle = handle
        self.new_handle = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        self.headline = QLabel()
        self.headline.setObjectName("dlgBody")
        self.headline.setWordWrap(True)
        lay.addWidget(self.headline)

        home = contract.resolve_home() or "（由 DSH 自行决定）"
        detail = QLabel(
            f"当前版本　{info.current}\n"
            f"目标版本　{info.target}（{updater.channel_label(info.channel)}）\n"
            f"更新方式　npm install -g {updater.PKG_NAME}@{info.target}\n"
            f"\n"
            f"你的会话记录、登录状态与设置存放于：\n{home}\n"
            f"（如需自行备份，复制上面这个目录即可）"
        )
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail.setObjectName("dlgHint")
        lay.addWidget(detail)

        self.log = dsh_theme.NotesView()
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.notes_btn = QPushButton("查看更新说明")
        self.notes_btn.clicked.connect(self._load_notes)
        self.go_btn = QPushButton("开始更新")
        self.go_btn.clicked.connect(self._start)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.notes_btn)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

        self.go_btn.setObjectName("primary")
        dsh_theme.style_dialog(self)
        self._refresh_headline()

    def _refresh_headline(self):
        # 目标是稳定版、但预览通道上还躺着更新的版本时，必须顺口说一句。
        # 否则用户切回稳定版后，会以为官方没有更新的东西了——0.1.7-rc.x
        # 被漏掉时就是这个体感。
        newer = self.info.newer_preview()
        hint = ""
        if newer and self.info.channel == "latest":
            hint = ("\n\n另外：预览通道上已有更新的 %s。"
                    "如需获取，请在托盘菜单里勾选「加入预览计划」。" % newer)

        if self.info.action == "downgrade":
            label = "稳定版" if self.info.channel == "latest" else "旧版本"
            self.headline.setText(
                f"将把 DSH 从 {self.info.current} 切回{label} {self.info.target}。\n\n"
                "注意：旧版本可能打不开新版本产生的会话记录。"
                "这些记录不会被删除，只是旧版本读不了——这是上游的格式版本机制，"
                "我们无法规避。" + hint)
        else:
            extra = ""
            if self.info.channel != "latest":
                extra = ("这是%s版，官方明示可能存在破坏性变更。"
                         % updater.channel_short(self.info.channel))
            self.headline.setText(
                f"将把 DSH 从 {self.info.current} 更新到 {self.info.target}。{extra}\n\n"
                "更新只替换程序本身，不会改动你的会话记录、登录状态和设置。" + hint)

    def _load_notes(self):
        self.notes_btn.setEnabled(False)
        self.log.setPlainText("正在获取更新说明（GitHub，可能较慢）…")
        self._notes = NotesWorker(self.info.target)
        self._notes.ready.connect(self._on_notes)
        self._notes.start()

    def _on_notes(self, text: str):
        self.notes_btn.setEnabled(True)
        if not text:
            self.log.setPlainText("未能获取更新说明（网络原因或该版本无发布说明）。")
            return
        self.log.show_notes(text, "markdown")

    def _start(self):
        self.go_btn.setEnabled(False)
        self.notes_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.log.clear()
        self._worker = UpdateWorker(self.info.target, self.handle,
                                    int(config.get("port") or 0), config.service_log())
        self._worker.line.connect(self.log.append_line)
        self._worker.line.connect(host_log)      # 更新全过程也进桌面壳日志
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, detail: str, new_handle):
        self.new_handle = new_handle
        self.close_btn.setEnabled(True)
        host_log("更新%s：%s" % ("成功" if ok else "失败", detail.replace("\n", " | ")[:400]))
        if ok:
            self.log.append_line("\n更新完成：DSH %s" % detail)
            QTimer.singleShot(1200, self.accept)
        else:
            self.log.append_line("\n更新失败，已尝试回滚：\n" + detail)
            self.go_btn.setEnabled(True)
            self.notes_btn.setEnabled(True)

class HostUpdateDialog(QDialog):
    """桌面壳自身更新的确认与进度窗口。

    与 UpdateDialog 分开，因为两者做的事完全不同：
    那个换的是 DSH 程序（npm 装），这个换的是**正在运行的这个 exe**
    （下载替换 + 重启）。合成一个窗口会让人以为更新完 DSH 界面就该变。
    """

    RESTART_NOW = 100          # 自定义返回值，区别于 Accepted / Rejected

    def __init__(self, parent, rel):
        super().__init__(parent)
        self.setWindowTitle(f"{HOST_NAME}更新")
        self.setMinimumWidth(620)
        self.resize(640, 440)
        self.rel = rel
        self._prepared = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        headline = QLabel(
            f"{HOST_NAME}有新版本 v{rel.version}（当前 v{HOST_VERSION}）。\n\n"
            f"这是{HOST_NAME}自身的更新，与 DSH 无关——\n"
            "更新后你的 DSH 版本、会话记录、登录状态都不受影响。")
        headline.setWordWrap(True)
        headline.setObjectName("dlgBody")
        lay.addWidget(headline)

        size_txt = ""
        if rel.asset and rel.asset.size:
            size_txt = f"（约 {rel.asset.size / 1048576:.1f} MB）"
        detail = QLabel(
            f"下载地址　GitHub Release {size_txt}\n"
            f"替换方式　下载完成后自动替换当前程序并重启\n"
            f"\n"
            f"更新过程会关闭本窗口。若替换失败，旧版本会保留为\n"
            f"　　{selfupdate.current_exe()}.old\n"
            f"可手工改回。")
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail.setObjectName("dlgHint")
        lay.addWidget(detail)

        self.log = dsh_theme.NotesView()
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.notes_btn = QPushButton("查看更新说明")
        self.notes_btn.clicked.connect(self._load_notes)
        self.go_btn = QPushButton("下载并更新")
        self.go_btn.clicked.connect(self._start)
        self.close_btn = QPushButton("稍后")
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.notes_btn)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

        self.go_btn.setObjectName("primary")
        dsh_theme.style_dialog(self)

        if rel.notes:
            self.log.show_notes(rel.notes, rel.notes_format)

    def _load_notes(self):
        self.notes_btn.setEnabled(False)
        if self.rel.notes:
            self.log.show_notes(self.rel.notes, self.rel.notes_format)
        else:
            self.log.show_notes("", "text")
        self._append_meta()

    def _append_meta(self):
        self.log.append_line(
            "\n开通时间　%s\n项目地址　%s"
            % (self.rel.published_at or "（未知）", self.rel.html_url))

    def _start(self):
        self.go_btn.setEnabled(False)
        self.notes_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.log.clear()
        self._worker = HostUpdateWorker(self.rel)
        self._worker.line.connect(self.log.append_line)
        self._worker.line.connect(host_log)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, msg: str):
        if not ok:
            self.log.append_line("\n更新失败：\n" + msg)
            self.go_btn.setEnabled(True)
            self.notes_btn.setEnabled(True)
            self.close_btn.setEnabled(True)
            return

        self._prepared = True
        self.log.append_line("\n" + msg)
        # 替换**已经做完了**（在这个窗口还开着的时候完成的），所以"重启"这一步
        # 只是把新实例拉起来 + 自己退出，是瞬时的。
        # 仍然给一个明确的按钮而不是自动退出：静默消失会让用户以为程序崩了。
        self.go_btn.setText("立即重启完成更新")
        self.go_btn.setEnabled(True)
        try:
            self.go_btn.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.go_btn.clicked.connect(self._restart)
        self.close_btn.setEnabled(True)
        self.close_btn.setText("稍后手动重启")

    def _restart(self):
        self.done(self.RESTART_NOW)

class ReinstallDialog(QDialog):
    """重新安装 DSH（修复半升级 / 损坏的安装）。

    为什么单独一个窗口，不复用 UpdateDialog
    ------------------------------------
    UpdateDialog 的每句文案都建立在"目标版本 ≠ 当前版本"之上（升级 / 降级 /
    旧版兼容性警告）。重装的目标就是当前版本，套进去会得到"升级到同一个版本"
    这种自相矛盾的说法。这里只需要三件事：说清会做什么、显示进度、报结果。

    背景（2026-10-20）
    ------------------
    `npm install -g` 可能只升主包、把精确版本的子依赖留在旧版。表现是
    "版本号正常，界面却一堆接口 404 / 账号信息读不出来"。这个入口让用户
    不必自己敲 npm 就能把安装重新做一遍。
    """

    def __init__(self, parent, version: str, handle):
        super().__init__(parent)
        self.setWindowTitle("重新安装 DSH")
        self.setMinimumWidth(600)
        self.resize(620, 420)
        self.version = version
        self.handle = handle
        self.new_handle = None
        self._worker = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        _inst = getattr(handle, "install", None)
        _where = ""
        if _inst is not None:
            _src = "私有安装" if "私有" in str(_inst.source) else "命令行那份（公共）"
            _where = "\n\n安装位置：%s\n（%s）" % (_inst.prefix, _src)
        headline = QLabel(
            "将重新安装 DeepSeek Harness v%s（就是当前这个版本）。%s"
            % (version, _where))
        headline.setObjectName("dlgBody")
        headline.setWordWrap(True)
        lay.addWidget(headline)

        detail = QLabel(
            "用途：修复「版本号看着正常、界面却报接口 404 或账号信息读不出来」"
            "这类更新没装干净的情况。\n\n"
            "过程：停止服务 → 卸载旧包 → 重新安装 → 校验各组件版本 → 重启服务。\n"
            "聊天记录、登录状态和设置都在 DSH_HOME 里，不会被改动。")
        detail.setObjectName("dlgHint")
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(detail)

        self.log = dsh_theme.NotesView()
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.go_btn = QPushButton("开始重新安装")
        self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self._start)
        self.close_btn = QPushButton("取消")
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

        dsh_theme.style_dialog(self)

    def _start(self):
        self.go_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.log.clear()
        self.log.append_line("开始重新安装 v%s…" % self.version)
        self._worker = UpdateWorker(self.version, self.handle,
                                    int(config.get("port") or 0),
                                    config.service_log(), fresh=True)
        self._worker.line.connect(self.log.append_line)
        self._worker.line.connect(host_log)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, detail: str, new_handle):
        self.new_handle = new_handle
        self.close_btn.setEnabled(True)
        host_log("重新安装%s：%s"
                 % ("成功" if ok else "未完成", detail.replace("\n", " | ")[:400]))
        if ok:
            self.log.append_line("\n重新安装完成：DSH %s" % detail)
            self.go_btn.setText("完成")
            self.go_btn.setEnabled(True)
            try:
                self.go_btn.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            self.go_btn.clicked.connect(self.accept)
        else:
            self.log.append_line("\n重新安装未完成：\n" + detail)
            self.go_btn.setEnabled(True)

    def closeEvent(self, event):                                  # noqa: N802
        # 安装过程不能被打断：中途退出会留下"卸载了但没装回去"的状态，
        # 那比原来更糟。运行期间窗口点叉一律不响应。
        if self._worker is not None and self._worker.isRunning():
            event.ignore()
            return
        super().closeEvent(event)

class DataTaskDialog(QDialog):
    """带进度条的数据操作窗口（迁移 / 清理 / 私有副本）。

    为什么不复用 ReinstallDialog：那个窗口的文案全是"重装 DSH"，进度反馈靠
    一行行日志。这里用户要判断的是"还要等多久"，所以必须有**真实进度条**，
    外加一行正在处理的文件（让人觉得它真的在动，而不是在假转圈）。

    运行期间不允许关窗：复制到一半被关掉会留下"半个新目录"，那是最难收拾的状态。
    """

    def __init__(self, parent, title: str, headline: str, hint: str,
                 worker: DataTaskWorker):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.resize(580, 340)
        self._worker = worker
        self.result_payload = None
        self.ok = False
        #: 进展文案先攒着，任务结束后再落 host.log。
        #: 为什么不能边跑边写（2026-10-21 冒烟测试发现）：`host_log()` 写在
        #: `data_root()/logs/host.log` —— 而"清缓存"正要把 logs 目录删掉。
        #: 一边删目录、一边往里面 open(..., "a") 写新文件，Windows 上会撞
        #: "文件被占用"，删不干净；迁移数据目录时路径还会中途换成新根，
        #: 那几行日志会直接落空。攒到收尾一次性写，两个问题一起没有。
        self._pending_log: list[str] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        head = QLabel(headline)
        head.setObjectName("dlgBody")
        head.setWordWrap(True)
        lay.addWidget(head)

        tip = QLabel(hint)
        tip.setObjectName("dlgHint")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)          # 数文件之前是不确定进度
        self.bar.setTextVisible(True)
        self.bar.setFormat("正在统计文件数…")
        lay.addWidget(self.bar)

        self.now = QLabel("")
        self.now.setObjectName("dlgHint")
        self.now.setWordWrap(False)
        lay.addWidget(self.now)

        self.log = dsh_theme.NotesView()
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = QPushButton("请稍候…")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

        dsh_theme.style_dialog(self)

        worker.line.connect(self._on_line)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_done)
        worker.finished.connect(self._on_thread_finished)

    # ---- 信号 ----

    def _on_line(self, msg: str) -> None:
        self.log.append_line(msg)
        self._pending_log.append(msg)

    def _flush_log(self) -> None:
        """收尾时把攒下的进展一次性写进 host.log。"""
        for msg in self._pending_log:
            host_log(msg)
        self._pending_log.clear()

    def _on_progress(self, done: int, total: int, name: str = "") -> None:
        if name:
            # 只保留文件名本身：全路径太长会把窗口撑宽
            self.now.setText(name if len(name) <= 60 else "…" + name[-57:])
        if total <= 0:
            self.bar.setRange(0, 0)
            self.bar.setFormat("%d 个文件…" % done)
            return
        self.bar.setRange(0, total)
        self.bar.setValue(min(done, total))
        self.bar.setFormat("%v / %m 个文件（%p%%）")

    def _on_done(self, ok: bool, detail: str, payload) -> None:
        self.ok = bool(ok)
        self.result_payload = payload
        self._flush_log()
        if ok:
            self.bar.setRange(0, 1)
            self.bar.setValue(1)
            self.bar.setFormat("完成")
            self.close_btn.setText("关闭")
            self.log.append_line("完成。")
        else:
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
            self.bar.setFormat("未完成")
            self.close_btn.setText("关闭")
            self.log.append_line("未完成：\n" + detail)
            host_log("数据任务失败：%s" % detail.replace("\n", " | ")[:400])
        self.close_btn.setEnabled(True)

    def _on_thread_finished(self) -> None:
        # QThread 结束后必须有一个明确的收尾点（见记忆里的 Qt 陷阱：
        # 线程活着退出解释器会让 Qt abort）。等它自己结束再放行。
        self._worker.wait(2000)

    def closeEvent(self, event):                                  # noqa: N802
        if self._worker.isRunning():
            event.ignore()               # 复制中途关窗会留下半个目录
            return
        self._worker.wait(2000)
        super().closeEvent(event)
