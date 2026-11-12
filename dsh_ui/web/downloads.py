"""下载处理：接住 `a[download]`、选路径、失败可读。"""
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
from dsh_host.logutil import host_log
# 字体与调色板是面板那套设计令牌，开发者窗口沿用同一个来源，
# 免得两个窗口的等宽字体各写各的。
from dsh_panel import FONT, palette



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

_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

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
