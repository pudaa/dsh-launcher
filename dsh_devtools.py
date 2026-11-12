# -*- coding: utf-8 -*-
"""开发者模式（兼容入口）。

2026-11-12 拆分
--------------
这个文件原有 2400 行：页面子类、下载处理、观测记录、开发者窗口、自测全在
一起。现在实现分成四个模块，这里只保留**重新导出**与**自测**：

    dsh_ui/web/recorder.py     控制台与网络事件缓冲
    dsh_ui/web/page.py         页面子类、弹层补帧、外链与剪贴板策略
    dsh_ui/web/downloads.py    下载拦截与落盘
    dsh_ui/devtools/window.py  日志台与 DevTools 宿主

`dsh_gui_qt.py` 一直以 `dsh_devtools.xxx` 的形式取名字，所以这里必须把公共名
继续导出，否则主窗口和四个自测脚本会一起断。
"""
from __future__ import annotations

from dsh_ui.web.recorder import *    # noqa: F401,F403
from dsh_ui.web.page import *        # noqa: F401,F403
from dsh_ui.web.downloads import *   # noqa: F401,F403
from dsh_ui.devtools.window import * # noqa: F401,F403

import ast as _ast                  # noqa: F401  自测里要用
import os as _os                    # noqa: F401

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
