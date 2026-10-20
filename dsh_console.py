# -*- coding: utf-8 -*-
"""宿主进程输出采集：把打包后"看不见的终端"收进开发者面板。

为什么需要它
------------
打包成 windowed exe 后进程没有控制台：

- Python 的 `sys.stdout` 往往是 `None`，`print` 直接丢；
- QtWebEngine（Chromium）的 C++ 日志写向一个无效的 fd 2，
  控制台里那些 `js: ...`、`[ERROR:CONSOLE] ...` 全部消失。

而这两类输出恰好是定位问题的第一手证据。所以这里在进程启动最早期接管
fd 1 / fd 2：重定向到一根管道，后台线程读出来进环形缓冲，
**有控制台时再回显给原句柄**（开发运行时终端照常能看到）。

三层结构
--------
1. `ConsoleBuffer`：按行切分 + 环形上限 + 级别标注 + 序号，线程安全；
2. `install()`：`os.dup2` 把 fd 1/2 接到管道，起读线程（纯线程，不依赖 Qt，
   所以能在 QApplication 之前装好）；
3. 面板（`dsh_devtools.TerminalView`）按序号增量拉取，自己决定是否跟随。

为什么要"按行 + 序号"而不是直接读文件
------------------------------------
面板需要"暂停后继续"、"只复制新增部分"这类操作，按序号增量取最省事；
同时环形缓冲保证长跑不会吃光内存（默认 8000 行）。
"""
from __future__ import annotations

import codecs
import os
import sys
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from dsh_host import config

#: 环形缓冲上限（行）。够覆盖一次启动 + 一轮操作，又不至于吃内存。
MAX_LINES = 8000

#: 原生输出落盘的上限；超了就在 install 时截断重来。
MAX_FILE_BYTES = 4 * 1024 * 1024

_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+", re.I)

#: 级别判定。顺序有意义：先判错误，再判警告，最后判 js。
_RULES = (
    ("error", re.compile(
        r"\[ERROR|\[FATAL|\bERROR\b|\bFATAL\b|\bCRITICAL\b|Traceback|"
        r"\bException\b|AssertionError|Segmentation fault", re.I)),
    ("warn", re.compile(r"\[WARN|\[WARNING|\bWARN(ING)?\b", re.I)),
    ("js", re.compile(r"^(js:|\[ERROR:CONSOLE|CONSOLE\()", re.I)),
)


def classify(line: str) -> str:
    """给一行输出标级别：error / warn / js / plain。

    只用于**着色**，不参与任何判断——判错了顶多颜色不对。
    """
    for name, rx in _RULES:
        if rx.search(line):
            return name
    return "plain"


def redact(text: str) -> str:
    """令牌打码。控制台里会出现带 token 的服务 URL，粘贴给 agent 前必须遮住。"""
    return _TOKEN_RE.sub(r"\1***", text)


@dataclass
class Entry:
    seq: int
    ts: float
    level: str
    text: str


class ConsoleBuffer:
    """线程安全的环形行缓冲。"""

    def __init__(self, max_lines: int = MAX_LINES):
        self._lock = threading.Lock()
        self._seq = 0
        self._lines: deque[Entry] = deque(maxlen=max_lines)
        self._partial = ""
        self.max_lines = max_lines

    # ---- 写入（任意线程） ----

    def append_text(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            data = self._partial + chunk
            parts = data.split("\n")
            self._partial = parts.pop()          # 最后一段可能不完整
            for raw in parts:
                self._push(raw)
            # 半行不急着进缓冲：等换行或下次 flush

    def _push(self, raw: str) -> None:
        text = redact(raw.rstrip("\r"))
        self._seq += 1
        self._lines.append(Entry(self._seq, time.time(), classify(text), text))

    def flush(self) -> None:
        """把残留的半行也推出去（进程退出、面板手动刷新时用）。"""
        with self._lock:
            if self._partial:
                self._push(self._partial)
                self._partial = ""

    # ---- 读取（主线程） ----

    def since(self, seq: int) -> list[Entry]:
        with self._lock:
            return [e for e in self._lines if e.seq > seq]

    def text_all(self) -> str:
        with self._lock:
            return "\n".join(e.text for e in self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._partial = ""

    def stats(self) -> tuple[int, int]:
        """(行数, 最后序号)"""
        with self._lock:
            return len(self._lines), self._seq


#: 进程级单例。面板直接用它；测试里可以自行构造 ConsoleBuffer。
CONSOLE = ConsoleBuffer()

_capture_lock = threading.Lock()
_capture: "NativeCapture | None" = None


def _move_high(fd: int) -> int:
    """把文件描述符挪到 10 以上，避免与 stdout/stderr 的 1/2 撞号。

    打包后 1/2 可能本来就是关闭的，`os.pipe()` 会顺手占用这两个号；
    那样后面的 `dup2(write_fd, 1)` 会把读端一起覆盖掉，采集变成空转。
    """
    if fd > 2:
        return fd
    for target in range(10, 64):
        try:
            os.dup2(fd, target)
        except OSError:
            continue
        try:
            os.close(fd)
        except OSError:
            pass
        return target
    return fd


class NativeCapture:
    """把 fd 1/2 接到管道，读线程负责入缓冲 + 回显 + 落盘。"""

    def __init__(self, buffer: ConsoleBuffer, log_path: str | None = None):
        self.buffer = buffer
        self.log_path = log_path
        self._read_fd = -1
        self._orig_out = -1
        self._orig_err = -1
        self._thread: threading.Thread | None = None
        self._file = None
        self.started = False

    def start(self) -> bool:
        global _capture
        if os.name != "nt" and not hasattr(os, "pipe"):
            return False
        try:
            read_fd, write_fd = os.pipe()
        except OSError:
            return False
        # **关键**：打包后 1/2 可能本来就是关闭的，os.pipe() 会顺手占用这两个号。
        # 那样后面的 dup2(write_fd, 1) 会把读端一起覆盖掉，采集变成空转。
        # 先无条件把管道两端挪到高位，再接管 1/2。
        read_fd = _move_high(read_fd)
        write_fd = _move_high(write_fd)

        # 先保存原来的句柄；打包后可能根本不存在（无控制台）→ 这时不回显。
        for attr, fd in (("_orig_out", 1), ("_orig_err", 2)):
            try:
                setattr(self, attr, os.dup(fd))
            except OSError:
                setattr(self, attr, -1)

        # 两个 fd 分别接管：打包后其中一个句柄可能是无效的，
        # 但另一个往往还能接上——不能因为一半失败就整体放弃。
        hooked = 0
        for fd in (1, 2):
            try:
                os.dup2(write_fd, fd)
                hooked += 1
                continue
            except OSError:
                pass
            # 目标 fd 可能是"关闭"状态（无控制台时就是这样），dup2 会失败。
            # 先占一个空设备把号占住，再接管它。
            try:
                nul = os.open(os.devnull, os.O_WRONLY)
                os.dup2(nul, fd)
                os.close(nul)
                os.dup2(write_fd, fd)
                hooked += 1
            except OSError:
                pass
        os.close(write_fd)
        if hooked == 0:
            os.close(read_fd)
            return False
        self.hooked_fds = hooked

        self._read_fd = read_fd
        # Python 的 stdout 默认块缓冲：print 的内容会晚一拍才进管道，
        # 现场看起来像"没输出"。改成行缓冲，采集即时。
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None:
                    stream.reconfigure(line_buffering=True)
            except (AttributeError, ValueError, OSError):
                pass
        self._open_log()
        self._thread = threading.Thread(target=self._pump, name="dsh-console-pump",
                                        daemon=True)
        self._thread.start()
        self.started = True
        with _capture_lock:
            _capture = self
        return True

    def _open_log(self) -> None:
        if not self.log_path:
            return
        try:
            if os.path.exists(self.log_path) and \
                    os.path.getsize(self.log_path) > MAX_FILE_BYTES:
                os.remove(self.log_path)
            self._file = open(self.log_path, "ab", buffering=0)
        except OSError:
            self._file = None

    def _pump(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                chunk = os.read(self._read_fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            if self._file is not None:
                try:
                    self._file.write(chunk)
                except OSError:
                    pass
            # 回显给原控制台：只用**一个**句柄。
            # fd 1/2 已经合并进同一根管道，分不出原来的归属；
            # 两个都写会让每一行在终端里出现两次。
            fd = self._orig_out if self._orig_out >= 0 else self._orig_err
            if fd >= 0:
                try:
                    os.write(fd, chunk)
                except OSError:
                    pass
            self.buffer.append_text(decoder.decode(chunk))

    def stop(self) -> None:
        """关闭采集：恢复原 fd，让读线程自然退出。

        顺序很关键：先把 1/2 指回原句柄（或关掉），管道的**写端**随之消失，
        读线程的 `read` 立刻返回 0 → 自己结束。反过来在别的线程里
        `close(read_fd)` 会让阻塞中的 read 行为未定义。

        **恢复完要把自己那几个 fd 也关掉**（2026-10-20 体检）：`_orig_out` /
        `_orig_err` 是 `os.dup()` 出来的副本，`_read_fd` 是管道读端，日志文件
        还有一个 FILE 对象。以前一个都不关，于是"开发者模式"每关一次再开一次
        就漏 3~4 个句柄（进程是长跑的，句柄泄漏迟早会显形）。
        """
        if not self.started:
            return
        self.started = False
        for attr, fd in (("_orig_out", 1), ("_orig_err", 2)):
            orig = getattr(self, attr)
            try:
                if orig >= 0:
                    os.dup2(orig, fd)
                else:
                    os.close(fd)
            except OSError:
                pass
        # 读线程此时应该已经读到 EOF 并退出（写端没了）。等一小会儿再关读端，
        # 免得在别的线程阻塞在 read 上时把 fd 抽走。
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        for attr in ("_orig_out", "_orig_err", "_read_fd"):
            fd = getattr(self, attr, -1)
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            setattr(self, attr, -1)
        self._thread = None
        fh = getattr(self, "_file", None)
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
            self._file = None


def _drain_on_exit() -> None:
    """进程退出前给读线程一点时间把管道里剩下的内容取走。

    读线程是 daemon：解释器一退它就被杀，管道里最后几 KB 会丢——
    而这恰恰可能是崩溃前最关键的一段。
    """
    import time as _t
    _t.sleep(0.05)


def _install_dump_hook() -> None:
    """`DSH_CONSOLE_DUMP=<文件>` 时，若干秒后把缓冲落盘。

    用途只有一个：**验证打包产物里到底抓到了什么**。exe 没有控制台，
    出问题时又不能总让用户开面板念给我们听，这个钩子让现场能一次导出。
    `DSH_CONSOLE_DUMP_WAIT` 控制等待秒数（默认 6）。
    """
    path = os.environ.get("DSH_CONSOLE_DUMP")
    if not path:
        return
    try:
        wait = float(os.environ.get("DSH_CONSOLE_DUMP_WAIT", "6"))
    except ValueError:
        wait = 6.0

    def dump() -> None:
        try:
            CONSOLE.flush()
            with open(path, "w", encoding="utf-8") as f:
                f.write(CONSOLE.text_all() + "\n")
        except OSError:
            pass

    timer = threading.Timer(wait, dump)
    timer.daemon = True
    timer.start()


def install(log_path: str | None = None) -> bool:
    """在进程最早期安装采集（**必须早于 QApplication**）。

    幂等：重复调用只返回第一次的结果。
    """
    global _capture
    with _capture_lock:
        if _capture is not None:
            return _capture.started
    cap = NativeCapture(CONSOLE, log_path or os.path.join(config.log_dir(),
                                                          "console.log"))
    ok = cap.start()
    if ok:
        import atexit
        atexit.register(_drain_on_exit)
    else:
        # 让面板有话说：空白的终端页比"采集没启用"更难判断。
        CONSOLE.append_text(
            "（进程输出采集未启用：无法重定向 stdout/stderr。\n"
            "  终端页只能显示这一行；日志文件仍可在 logs\\ 目录查看。）\n")
    _install_dump_hook()
    return ok


def shutdown() -> None:
    """关掉采集（开发者模式关闭时调用）。

    只停线程与 fd 接管，**不清空**缓冲：重新打开时还能看到关闭前的历史。
    缓冲有 8000 行上限，最坏情况也就几 MB，不会随运行时长增长。
    """
    global _capture
    with _capture_lock:
        cap, _capture = _capture, None
    if cap is not None:
        cap.stop()


def capture() -> NativeCapture | None:
    return _capture


# ------------------------------------------------------------------ 自测

def selftest_checks() -> list:
    """纯逻辑断言：切行、环形上限、级别判定、令牌打码。"""
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                               ("  " + extra) if extra else ""))
        if not ok:
            failures.append(name)

    b = ConsoleBuffer(max_lines=4)
    b.append_text("one\ntwo")
    check("半行不提前入缓冲", len(b.since(0)) == 1, "%d 行" % len(b.since(0)))
    b.append_text(" lines\nthree\n")
    check("跨块能拼成完整行",
          [e.text for e in b.since(0)] == ["one", "two lines", "three"],
          "、".join(e.text for e in b.since(0)))

    b.append_text("ERROR boom\nWARN careful\njs: hello\n")
    levels = {e.text: e.level for e in b.since(0)}
    check("级别判定正确",
          levels.get("ERROR boom") == "error"
          and levels.get("WARN careful") == "warn"
          and levels.get("js: hello") == "js",
          str(levels))

    for i in range(10):
        b.append_text("line%d\n" % i)
    kept = b.since(0)
    check("环形上限生效", len(kept) == 4, "%d 行" % len(kept))
    check("淘汰旧行后序号仍单调",
          [e.seq for e in kept] == sorted(e.seq for e in kept)
          and kept[-1].seq > kept[0].seq)

    b2 = ConsoleBuffer()
    b2.append_text("http://127.0.0.1:9/?token=secret123&x=1\n")
    text = b2.text_all()
    check("令牌在入缓冲前就打码",
          "secret123" not in text and "token=***" in text, text)

    b2.flush()
    check("flush 能吐出没有换行的尾巴", "token=***" in b2.text_all())

    return failures
