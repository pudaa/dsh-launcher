# -*- coding: utf-8 -*-
"""无头 GUI 冒烟测试：验证 MainWindow 启动流程 + 接管已有服务 + WebEngine 懒加载。

    python tools/gui_selftest.py
"""
import os
import sys
import ctypes
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 隔离配置目录：config 的数据根是 %LOCALAPPDATA%\DSH-Web，是**用户的真实配置**。
# 本脚本会构造 MainWindow，其中的菜单回调可能写配置——必须重定向到沙箱，
# 否则"跑个测试"就会改掉用户的设置。
# 教训来源：titlebar_e2e.py 曾把 adaptive_titlebar 写成 false，导致功能被关。
_SANDBOX = os.path.join(tempfile.gettempdir(), "dsh-launcher-test-cfg")
os.makedirs(_SANDBOX, exist_ok=True)
os.environ["LOCALAPPDATA"] = _SANDBOX

# 输出强制 UTF-8。默认 stdout 编码跟随系统区域设置——在 cp1252 的机器上
# （CI 的 windows runner 就是这样）打印中文会直接 UnicodeEncodeError 崩掉，
# 而且报错位置看起来像是脚本本身有问题，实际只是控制台编码。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from PySide6.QtCore import QTimer                    # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel   # noqa: E402

import dsh_gui_qt as g                               # noqa: E402
from dsh_host import contract                        # noqa: E402

state: dict = {}


def _row_texts(dlg) -> list[tuple[str, str]]:
    out = []
    for i in range(dlg.rows_box.count()):
        w = dlg.rows_box.itemAt(i).widget()
        labels = w.findChildren(QLabel)
        out.append((labels[0].text(), labels[1].text()))
    return out


def onboarding_checks() -> list[str]:
    """用合成报告验证引导窗口的三种状态渲染，不触发任何安装动作。"""
    failures: list[str] = []

    bare = contract.EnvironmentReport(detail="未检测到 Node.js 运行环境，需要先安装。")
    has_node = contract.EnvironmentReport(
        node=r"C:\Program Files\nodejs\node.exe", node_version="v24.19.0",
        npm_cli=r"C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js",
        detail="Node.js 已就绪，但未安装 DeepSeek Harness。")
    ready = contract.EnvironmentReport(
        node=r"C:\Program Files\nodejs\node.exe", node_version="v24.19.0",
        npm_cli=r"C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js",
        install=contract.DshInstall(
            node=r"C:\Program Files\nodejs\node.exe", bin_js=r"C:\p\bin.js",
            pkg_root=r"C:\p", prefix=r"C:\p2", npm_cli=r"C:\npm-cli.js",
            version="0.1.6-alpha.2", source="测试"))

    cases = [
        ("裸机（缺 Node）", bare, {"go": True, "site": True}),
        ("仅缺 DSH", has_node, {"go": True, "site": False}),
        ("环境已就绪", ready, {"go": False, "site": False}),
    ]
    for name, rep, expect in cases:
        dlg = g.OnboardingDialog(None, rep)
        dlg.show()
        rows = _row_texts(dlg)
        ok = (dlg.go_btn.isEnabled() == expect["go"]
              and dlg.site_btn.isVisible() == expect["site"])
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
        for n, t in rows:
            print(f"         {n:20} {t}")
        print(f"         开始安装按钮可用={dlg.go_btn.isEnabled()}（期望 {expect['go']}）"
              f" 官网按钮可见={dlg.site_btn.isVisible()}（期望 {expect['site']}）")
        if not ok:
            failures.append(name)
        dlg.close()
        dlg.deleteLater()
    return failures


def feedback_checks(win) -> list[str]:
    """回归：手动「检查更新」必须产生应用内反馈。

    出过的问题：检查结果只走 Windows 托盘气泡，用户点完菜单什么都没看到，
    以为程序没反应。所以这里断言——手动路径**无论哪种结果**都要有应用内反馈，
    而自动路径不能弹窗（否则启动就弹一堆）。
    """
    from dsh_host import updater
    failures: list[str] = []
    seen: list[tuple[str, str]] = []

    win._info = lambda t, *a, **k: seen.append(("info", t))
    win._warn = lambda t, *a, **k: seen.append(("warn", t))
    win._show_update_dialog = lambda t, *a, **k: seen.append(("dialog", t))

    no_update = updater.UpdateInfo(
        current="0.1.5-rc.2", stable="0.1.5-rc.2", alpha="0.1.6-alpha.2",
        target="0.1.5-rc.2", channel="latest", action="none")
    has_update = updater.UpdateInfo(
        current="0.1.5-rc.2", stable="0.1.5-rc.2", alpha="0.1.6-alpha.2",
        target="0.1.6-alpha.2", channel="alpha", action="upgrade")

    cases = [
        ("手动·无更新 → 应用内提示", lambda: win._on_check(no_update, True), "info"),
        ("手动·有更新 → 打开更新窗口", lambda: win._on_check(has_update, True), "dialog"),
        ("手动·检查失败 → 应用内告警", lambda: win._on_check_failed("模拟失败", True), "warn"),
        ("自动·无更新 → 不打扰", lambda: win._on_check(no_update, False), None),
        ("自动·检查失败 → 不打扰", lambda: win._on_check_failed("模拟失败", False), None),
    ]
    for name, fn, expect in cases:
        seen.clear()
        fn()
        got = seen[-1][0] if seen else None
        ok = got == expect
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name} → 实际 {got or '无反馈'}")
        if not ok:
            failures.append(name)
    return failures


def update_dialog_copy_checks() -> list[str]:
    """回归：更新对话框必须把「数据安全」讲清楚。

    老大的问题是"更新会不会丢聊天记录"。答案不能只活在文档里——
    用户点更新时看到的那段文字才是他真正会读的东西。
    """
    from dsh_host import updater
    failures: list[str] = []

    upgrade = updater.UpdateInfo(
        current="0.1.5-rc.2", stable="0.1.5-rc.2", alpha="0.1.6-alpha.2",
        target="0.1.6-alpha.2", channel="alpha", action="upgrade")
    downgrade = updater.UpdateInfo(
        current="0.1.6-alpha.2", stable="0.1.5-rc.2", alpha="0.1.6-alpha.2",
        target="0.1.5-rc.2", channel="latest", action="downgrade")

    dlg = g.UpdateDialog(None, upgrade, None)
    text = dlg.headline.text()
    ok = "不会改动你的会话记录" in text
    print(f"  [{'OK  ' if ok else 'FAIL'}] 升级说明含数据安全承诺")
    if not ok:
        failures.append("升级数据安全文案")
        print("        实际:", text.replace("\n", " / "))

    labels = dlg.findChildren(QLabel)
    paths = [l.text() for l in labels if "会话记录、登录状态与设置存放于" in l.text()]
    ok = bool(paths)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 升级说明给出数据目录位置（便于自行备份）")
    if not ok:
        failures.append("数据目录位置")
    dlg.close()
    dlg.deleteLater()

    dlg2 = g.UpdateDialog(None, downgrade, None)
    text2 = dlg2.headline.text()
    ok = "打不开新版本产生的会话记录" in text2 and "不会被删除" in text2
    print(f"  [{'OK  ' if ok else 'FAIL'}] 回滚说明含格式兼容风险与「不删除」承诺")
    if not ok:
        failures.append("回滚风险文案")
        print("        实际:", text2.replace("\n", " / "))
    dlg2.close()
    dlg2.deleteLater()
    return failures


def menu_structure_checks(win) -> list[str]:
    """回归：托盘菜单的分组与语义（2026-09-20 重构）。

    重构要解决的问题：
      1. 「停止后台服务」与「退出」语义不同但用户意图相同 —— 只留「退出」
      2. DSH 更新与桌面壳更新混在一起 —— 必须能一眼分辨换的是哪一个

    这几条断言是防止将来又退回去。
    """
    failures: list[str] = []
    texts = [a.text() for a in win.tray.contextMenu().actions() if not a.isSeparator()]

    # 1. 「停止后台服务」不应再作为独立菜单项存在
    ok = not any("停止后台服务" == t for t in texts)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 不再有独立的「停止后台服务」菜单项")
    if not ok:
        failures.append("残留停止服务菜单项")

    # 2. DSH 更新与桌面壳更新必须分别有明确标识
    ok = any("DSH" in t and "更新" in t for t in texts) and \
        any(("桌面壳" in t or "Launcher" in t) and "更新" in t for t in texts)
    print(f"  [{'OK  ' if ok else 'FAIL'}] DSH 更新与桌面壳更新分开呈现")
    if not ok:
        failures.append("两类更新未区分")
        print("        实际菜单:", texts)

    # 3. 「退出」必须存在且是退出语义
    ok = any(t == "退出" for t in texts)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 保留「退出」入口")
    if not ok:
        failures.append("缺少退出入口")

    # 4. 关于项存在（版本信息有地方可查）
    ok = any("关于" in t for t in texts)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 提供关于/版本信息入口")
    if not ok:
        failures.append("缺少关于入口")

    # 4b. 控制面板入口存在
    #     托盘 menu 装不下"看状态 / 读提示"这类内容，面板是它们的唯一去处；
    #     这个入口一旦丢了，面板就等于不存在。
    ok = any("控制面板" in t for t in texts)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 提供控制面板入口")
    if not ok:
        failures.append("缺少控制面板入口")
        print("        实际菜单:", texts)

    # 4c. 设置类项**不应**再出现在托盘（已搬进控制面板）
    #     这条是防回退：托盘只该放"一步到位"的动作，设置与有风险的版本管理
    #     动作应该在面板里、带说明文字。加回去就等于又变回原来的样子。
    MOVED = ("加入预览计划", "标题栏跟随界面配色", "回归稳定版",
             "回滚 DSH 到上一版本", "准备运行环境…")
    still_there = [t for t in texts if t in MOVED]
    print(f"  [{'OK  ' if not still_there else 'FAIL'}] 设置类项已从托盘移出")
    if still_there:
        failures.append("托盘残留设置项")
        print("        仍在托盘里:", still_there)

    # 5. 退出时确实会停服务（不再有菜单项，但行为必须还在）
    stopped = []
    win._stop_service = lambda *a, **k: (stopped.append("stop"), 1)[1]
    win.tray.hide = lambda *a, **k: None
    real_quit = g.QApplication.quit
    g.QApplication.quit = lambda *a, **k: stopped.append("quit")
    try:
        win._quit()
    finally:
        g.QApplication.quit = real_quit
    ok = "stop" in stopped and "quit" in stopped
    print(f"  [{'OK  ' if ok else 'FAIL'}] 退出时会连带停止后台服务")
    if not ok:
        failures.append("退出未停服务")
    return failures


def host_update_checks() -> list[str]:
    """桌面壳自更新的纯逻辑校验（不联网、不下载）。"""
    from dsh_host import selfupdate as su
    from dsh_host.version import HOST_VERSION, is_newer
    failures: list[str] = []

    # 1. 版本比较
    #    预发布段必须按 semver 语义比，不能比字符串：字典序下
    #    `beta.10` < `beta.9`，会让第 10 个 beta 被当成"比当前旧"
    #    而漏掉更新提示（2026-10-19 实测过这个错判）。
    cases = [
        ("2.0.1", "2.0.0", True),
        ("2.0.0", "2.0.1", False),
        ("2.1.0", "2.0.9", True),
        ("2.0.0", "2.0.0", False),
        # 预发布 < 同号正式版
        ("2.4.0-beta.1", "2.4.0", False),
        ("2.4.0", "2.4.0-beta.1", True),
        # 预发布之间：序号按数值，段数少的更小
        ("2.4.0-beta.2", "2.4.0-beta.1", True),
        ("2.4.0-beta.10", "2.4.0-beta.9", True),
        ("2.4.0-beta.1", "2.4.0-beta", True),
        ("2.4.0-alpha.9", "2.4.0-beta.1", False),
        # 数字段优先级低于字母段（semver §11）
        ("2.4.0-2", "2.4.0-alpha", False),
        ("2.4.0-alpha", "2.4.0-2", True),
        # 带构建元数据时只看前面的版本
        ("2.4.0+build9", "2.4.0", False),
    ]
    bad = [f"{a}>{b}" for a, b, exp in cases if is_newer(a, b) != exp]
    print(f"  [{'OK  ' if not bad else 'FAIL'}] 版本比较逻辑正确")
    if bad:
        failures.append("版本比较")
        print("        异常用例:", bad)

    # 1b. GitHub API 限额时的兜底（2026-10-19 老大连续检查撞上 403 rate limit）。
    #     API 未登录只有 60 次/小时/IP；releases/latest 的 302 与 releases.atom
    #     都不占配额。这里用假数据钉住解析与降级顺序，不联网。
    atom = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/1/v9.9.9</id>
    <link rel="alternate" type="text/html"
          href="https://github.com/pudaa/dsh-launcher/releases/tag/v9.9.9"/>
    <title>v9.9.9 — 测试版本</title>
    <updated>2026-10-19T03:17:56Z</updated>
    <content type="html">&lt;h2&gt;标题&lt;/h2&gt;&lt;p&gt;正文&lt;/p&gt;</content>
  </entry>
</feed>"""
    rel = su._parse_atom_release(atom)
    ok = (rel is not None and rel.version == "9.9.9" and rel.tag == "v9.9.9"
          and rel.has_asset and rel.asset.name == su.HOST_ASSET_NAME
          and rel.asset.url.endswith("/releases/download/v9.9.9/DSH-Web.exe")
          and "标题" in rel.notes and "正文" in rel.notes
          and rel.notes_format == "html"
          and rel.published_at == "2026-10-19")
    print(f"  [{'OK  ' if ok else 'FAIL'}] Atom feed 可解析出 tag / 说明 / 资产地址")
    if not ok:
        failures.append("Atom 解析")
        print("        rel=", rel)

    orig_json, orig_read = su._http_json, su._http_read
    try:
        # API 全部失败；302 与 Atom 都可用 → 必须返回 Atom 的说明
        su._http_json = lambda *a, **k: None

        def fake_read(url, timeout=20):
            if url.endswith("/releases/latest"):
                return ("https://github.com/pudaa/dsh-launcher/releases/tag/v9.9.9", b"")
            if url.endswith("/releases.atom"):
                return ("https://github.com/pudaa/dsh-launcher/releases.atom",
                        atom.encode("utf-8"))
            return None

        su._http_read = fake_read
        rel = su.fetch_latest_release(timeout=1)
        ok = (rel is not None and rel.version == "9.9.9"
              and rel.has_asset and "正文" in rel.notes)
        print(f"  [{'OK  ' if ok else 'FAIL'}] API 限额时自动降级到页面/Atom")
        if not ok:
            failures.append("API 降级")

        # 连 Atom 都拿不到时，302 的 tag 仍足以发现新版本
        def fake_read_no_atom(url, timeout=20):
            if url.endswith("/releases/latest"):
                return ("https://github.com/pudaa/dsh-launcher/releases/tag/v9.9.9", b"")
            return None

        su._http_read = fake_read_no_atom
        rel = su.fetch_latest_release(timeout=1)
        ok = rel is not None and rel.version == "9.9.9" and rel.has_asset
        print(f"  [{'OK  ' if ok else 'FAIL'}] Atom 不可用时仍能从 302 取到版本")
        if not ok:
            failures.append("302 降级")
    finally:
        su._http_json, su._http_read = orig_json, orig_read

    # 1c. **三条获取路径的预发布过滤必须一致**（2026-10-19 老大要求）。
    #     平时 latest 接口本身就跳过预发布，所以这类 bug 只在"API 撞限额、
    #     降级到列表/页面"时暴露——那是用户端最难复现、也最不该被推荐的时刻。
    beta_item = {
        "tag_name": "v9.9.9-beta.1", "prerelease": False,
        "body": "beta", "html_url": "https://example.invalid/beta",
        "published_at": "2026-10-19T00:00:00Z", "assets": [],
    }
    stable_item = {
        "tag_name": "v9.9.8", "prerelease": False,
        "body": "stable", "html_url": "https://example.invalid/stable",
        "published_at": "2026-10-18T00:00:00Z", "assets": [],
    }
    two_entries = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <link rel="alternate" href="https://github.com/pudaa/dsh-launcher/releases/tag/v9.9.9-beta.1"/>
    <title>beta</title><updated>2026-10-19T00:00:00Z</updated>
  </entry>
  <entry>
    <link rel="alternate" href="https://github.com/pudaa/dsh-launcher/releases/tag/v9.9.8"/>
    <title>stable</title><updated>2026-10-18T00:00:00Z</updated>
  </entry>
</feed>"""

    def fake_json(url, timeout=20):
        if url.endswith("/releases/latest"):
            return None                      # 模拟 latest 拿不到
        return [beta_item, stable_item]      # 列表：beta 在前，稳定版在后

    def fake_read_beta_latest(url, timeout=20):
        if url.endswith("/releases/latest"):
            # 模拟"最近一次发布就是 beta"——302 会指向它
            return ("https://github.com/pudaa/dsh-launcher/releases/tag/v9.9.9-beta.1", b"")
        if url.endswith("/releases.atom"):
            return ("https://github.com/pudaa/dsh-launcher/releases.atom",
                    two_entries.encode("utf-8"))
        return None

    consistency = []
    try:
        # A. 列表路径：跳过 beta 取后面的稳定版
        su._http_json = fake_json
        su._http_read = lambda *a, **k: None
        r = su.fetch_latest_release(timeout=1)
        consistency.append(("列表跳过 beta", r is not None and r.version == "9.9.8"))

        # B. 降级路径：302 指向 beta 时也要退到稳定版
        su._http_json = lambda *a, **k: None
        su._http_read = fake_read_beta_latest
        r = su.fetch_latest_release(timeout=1)
        consistency.append(("302 指向 beta 时退到稳定版",
                            r is not None and r.version == "9.9.8"))

        # C. 整条链路只有 beta 时，宁可返回 None（提示"无法获取"），
        #    也不能把测试版推给稳定用户
        su._http_json = lambda *a, **k: [beta_item]
        su._http_read = lambda *a, **k: None
        r = su.fetch_latest_release(timeout=1)
        consistency.append(("只有 beta 时不返回 beta", r is None))

        # D. 反向断言：明确要预发布时，列表路径必须能拿到 beta
        su._http_json = fake_json
        r = su.fetch_latest_release(timeout=1, include_prerelease=True)
        consistency.append(("要求预发布时能拿到 beta",
                            r is not None and r.version == "9.9.9-beta.1"))

        # E. 反向断言：降级路径同样（302 指向 beta 时直接返回它）
        su._http_json = lambda *a, **k: None
        su._http_read = fake_read_beta_latest
        r = su.fetch_latest_release(timeout=1, include_prerelease=True)
        consistency.append(("要求预发布时 302 的 beta 可用",
                            r is not None and r.version == "9.9.9-beta.1"))
    finally:
        su._http_json, su._http_read = orig_json, orig_read

    bad = [name for name, ok in consistency if not ok]
    print(f"  [{'OK  ' if not bad else 'FAIL'}] 预发布过滤在三条路径上一致")
    if bad:
        failures.append("预发布过滤一致性")
        print("        异常用例:", bad)

    # 1d. 两种"没有更新"必须给出**不同**答复（2026-10-19 老大定：链路一致优先）。
    #     - 连通正常、只有测试版 → "已是最新"：预发布是否存在不该改变结论，
    #       否则"刷新前能看到 beta、刷新后只能看到正式版"会让行为漂移。
    #     - 完全拿不到数据 → "无法获取"：不能伪装成"已是最新"，那是静默失败。
    only_beta_ok = offline_ok = False
    msg1 = msg2 = ""
    try:
        su._http_json = lambda *a, **k: [beta_item]
        su._http_read = lambda *a, **k: None
        _, msg1 = su.check_host_update(timeout=1)
        only_beta_ok = "已是最新" in msg1

        su._http_json = lambda *a, **k: None
        su._http_read = lambda *a, **k: None
        _, msg2 = su.check_host_update(timeout=1)
        offline_ok = "无法获取" in msg2
    finally:
        su._http_json, su._http_read = orig_json, orig_read

    ok = only_beta_ok and offline_ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] 只有测试版报「已是最新」，拿不到数据才报「无法获取」")
    if not ok:
        failures.append("更新检查文案")
        print(f"        只有 beta={only_beta_ok}  全失败={offline_ok}"
              f"  实际: {msg1[:40]!r} / {msg2[:40]!r}")


    # 1e. 富文本视图**没有** `appendPlainText`（2026-10-20 老大实测事故）。
    #     2.3.1 把更新对话框的日志控件从 QPlainTextEdit 换成 NotesView 时，
    #     接线没跟着改：点「下载并更新」当场 AttributeError；而异常发生在
    #     按钮已经禁用之后，用户看到的是"进入更新状态，但一个字的进度都没有"。
    #     这里同时钉两头：控件要有自己的追加方法，两个对话框里不许再出现旧方法名。
    import inspect

    import dsh_theme
    leaks = [cls.__name__ for cls in (g.UpdateDialog, g.HostUpdateDialog)
             if "appendPlainText" in inspect.getsource(cls)]
    probe = dsh_theme.NotesView()
    probe.show_notes("## 版本说明\n正文一段", "markdown")
    probe.append_line("已下载 12.3 / 106.9 MB  (11%)")
    plain = probe.toPlainText()
    append_ok = ("已下载 12.3" in plain) and ("版本说明" in plain)
    ok = (hasattr(dsh_theme.NotesView, "append_line")
          and not leaks and append_ok)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 更新对话框的进度接线用富文本视图自己的方法")
    if not ok:
        failures.append("更新进度接线")
        print(f"        append_line={hasattr(dsh_theme.NotesView, 'append_line')}"
              f"  追加可见={append_ok}  残留旧调用={leaks}")

    # 1f. 「重新安装 DSH」入口（2026-10-20）：半升级的唯一自助修复路径。
    #     三个要素缺一不可：对话框存在、能在安装途中挡住关闭（中途退出会留下
    #     "卸了没装回去"的残局）、面板动作真的接到主窗口。
    ok = (hasattr(g, "ReinstallDialog")
          and hasattr(g.ReinstallDialog, "closeEvent")
          and "reinstall_dsh" in inspect.getsource(g.MainWindow._panel_action))
    print(f"  [{'OK  ' if ok else 'FAIL'}] 提供「重新安装 DSH」入口且安装中不可关闭")
    if not ok:
        failures.append("重新安装入口")
        print(f"        ReinstallDialog={hasattr(g, 'ReinstallDialog')}"
              f"  closeEvent={hasattr(getattr(g, 'ReinstallDialog', None), 'closeEvent')}"
              f"  动作接线={'reinstall_dsh' in inspect.getsource(g.MainWindow._panel_action)}")

    # 2. 源码运行时不支持自更新（避免替换掉开发的 py 文件）
    supported, reason = su.self_update_supported()
    ok = (supported is False) and bool(reason)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 源码运行时禁用自更新并给出原因")
    if not ok:
        failures.append("源码运行未禁用自更新")

    # 3. 生成的替换脚本关键要素齐全
    script = su.build_apply_script(r"C:\apps\DSH-Web.exe", r"C:\tmp\new.exe", 1234)
    must = [
        ("反斜杠路径", r"C:\apps\DSH-Web.exe"),
        ("等待进程", "PID eq %PID%"),
        ("同目录重命名而非复制", "move /y"),
        ("失败回滚", "回滚"),
        ("自删除在最后", 'del "%~f0"'),
    ]
    missing = [name for name, frag in must if frag not in script]
    print(f"  [{'OK  ' if not missing else 'FAIL'}] 替换脚本要素齐全")
    if missing:
        failures.append("替换脚本要素")
        print("        缺失:", missing)

    # 4. 自删除不能出现在中间（否则 cmd 读不到后续行）
    idx = script.find('del "%~f0"')
    tail = script[idx:]
    ok = idx > 0 and "\n" not in tail.split("\n", 1)[1].replace("exit /b 0", "").strip()
    print(f"  [{'OK  ' if ok else 'FAIL'}] 自删除位于脚本末尾（cmd 逐行读取的约束）")
    if not ok:
        failures.append("自删除位置")
        print("        del 之后仍有可执行行:", repr(tail[:120]))

    # 5. 正斜杠路径必须被规范化掉（move 对正斜杠会静默失败）
    ok = "/" not in script.split("setlocal")[1].split("\n")[2]
    print(f"  [{'OK  ' if ok else 'FAIL'}] 路径已规范化为反斜杠")
    if not ok:
        failures.append("路径未规范化")

    # 6. PID 为 0 时不生成等待循环
    s0 = su.build_apply_script(r"C:\a\b.exe", r"C:\t\n.exe", 0)
    ok = "waitloop" not in s0
    print(f"  [{'OK  ' if ok else 'FAIL'}] PID 无效时不生成等待循环")
    if not ok:
        failures.append("PID=0 仍生成等待循环")

    # 7. 下载体积下限能拦住错误页
    ok = su.MIN_EXE_BYTES >= 1024 * 1024
    print(f"  [{'OK  ' if ok else 'FAIL'}] 有下载体积下限校验")
    if not ok:
        failures.append("缺少体积校验")

    # 8. 启动 bat 的 creationflags 不能带 DETACHED_PROCESS
    #    ------------------------------------------------------------------
    #    这是一条**踩过才知道**的断言。MSDN 明写 `CREATE_NO_WINDOW` 与
    #    `DETACHED_PROCESS` 同用时会被忽略，而 DETACHED 让 cmd 完全没有控制台，
    #    于是 bat 里跑的 `tasklist` / `find.exe` / `ping` 各自被分配一个新控制台
    #    **窗口** —— 更新时会冒出一个标题为 find.exe 的窗口，且不会自己关。
    #    （2026-10-01 老大实测报上来。）只断言"有 NO_WINDOW"是不够的，
    #    必须同时断言"没有 DETACHED"，因为这个 bug 正是两者叠加造成的。
    flags = su.BAT_CREATION_FLAGS
    has_nowindow = bool(flags & 0x08000000)
    has_detached = bool(flags & 0x00000008)
    ok = has_nowindow and not has_detached
    print(f"  [{'OK  ' if ok else 'FAIL'}] 启动 bat 用 NO_WINDOW 且不带 DETACHED"
          f"（flags=0x{flags:08X}）")
    if not ok:
        failures.append("bat 启动标志会弹控制台")
        print("        NO_WINDOW=%s DETACHED=%s" % (has_nowindow, has_detached))

    # 9. 上次自更新留下的 .old 能被清理
    import os as _os
    import tempfile as _tf
    d = _tf.mkdtemp(prefix="su-old-")
    try:
        p = _os.path.join(d, "app.exe.old")
        open(p, "wb").write(b"old")
        removed = su.remove_backup(p)
        ok1 = removed == p and not _os.path.exists(p)
        ok2 = su.remove_backup(None) is None          # 没有备份不算错
        ok3 = su.remove_backup(_os.path.join(d, "无此文件.old")) is None
        ok = ok1 and ok2 and ok3
        print(f"  [{'OK  ' if ok else 'FAIL'}] 旧版备份可被清理且缺文件不报错")
        if not ok:
            failures.append("旧版备份清理")
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)

    return failures


def titlebar_checks() -> list[str]:
    """标题栏取色与配色的纯逻辑校验（不碰真实窗口）。"""
    from dsh_host import config, dwm
    failures: list[str] = []

    # 1. 主色调统计能扛住边界输入
    edge = [
        ("空数据", lambda: dwm.dominant_color(b"", 0, 0)),
        ("零宽度", lambda: dwm.dominant_color(b"\x00" * 16, 0, 4)),
        ("全透明", lambda: dwm.dominant_color(bytes([255, 0, 0, 0] * 100), 10, 10)),
    ]
    bad = [n for n, fn in edge if fn() is not None]
    print(f"  [{'OK  ' if not bad else 'FAIL'}] 边界输入不崩且返回 None")
    if bad:
        failures.append("取色边界")
        print("        异常:", bad)

    # 2. 灰底不能盖过主题色 —— 两轮硬隔离
    #    **用真实采样规模造数据**（这一点踩过坑）：真实调用一次采样
    #    约 5w 像素（1200x800 窗口取顶部 6% 高、左右各让开 2%），
    #    拿小图（如 80x8）试会得出差两个数量级的错误门槛。
    SW, SH = 1152, 48                    # = 55296 像素，贴近真实
    CHROMA = (34, 193, 163)              # 青绿主题色

    def strip(n_cols, bg):
        """左起 n_cols 列为主题色，其余为 bg。"""
        row = bytes(CHROMA + (255,)) * n_cols + bytes(bg + (255,)) * (SW - n_cols)
        return row * SH, SW, SH

    px, w, h = strip(576, (30, 30, 30))          # 50% 主题色
    got = dwm.dominant_color(px, w, h)
    ok = got is not None and got[1] > got[0] and got[1] > got[2]
    print(f"  [{'OK  ' if ok else 'FAIL'}] 灰底 50% 主题色 -> 识别出主题色")
    if not ok:
        failures.append("主题色识别")
        print("        实际取到:", got)

    # 2b. 极少量彩色（图标边缘抗锯齿级别）不应主导标题栏
    #     1 列 = 48 像素 = 0.087%，必须被挡掉
    px, w, h = strip(1, (30, 30, 30))
    got = dwm.dominant_color(px, w, h)
    ok = got is not None and abs(got[0] - got[1]) < 8 and abs(got[1] - got[2]) < 8
    print(f"  [{'OK  ' if ok else 'FAIL'}] 0.09% 彩色杂色被挡（退回灰色而非被带偏）")
    if not ok:
        failures.append("杂色门槛")
        print("        实际取到:", got, "（期望接近灰，不该是青绿）")

    # 2c. 纯灰界面应退回灰色方案，而不是返回 None 或乱配色
    px = bytes([0x30, 0x30, 0x30, 0xFF] * (SW * SH))
    got = dwm.dominant_color(px, SW, SH)
    ok = got is not None and abs(got[0] - got[1]) < 8 and abs(got[1] - got[2]) < 8
    print(f"  [{'OK  ' if ok else 'FAIL'}] 纯灰界面退回灰色方案（不返回 None）")
    if not ok:
        failures.append("纯灰回退")
        print("        实际取到:", got)

    # 3. 亮度判据用 BT.601 而非均值（均值会把深蓝判成浅色）
    #    深蓝 (20,30,50)：均值 33 与加权 29 —— 都算深色，看不出差别。
    #    用绿色 (0,200,0)：均值 67（判深），加权 117（仍判深）—— 也看不出。
    #    真正能区分的：亮绿 (0,230,0)。均值 77（判深，错），加权 135（判浅，对）。
    ok_green = not dwm.is_dark((0, 230, 0))
    ok_blue = dwm.is_dark((20, 30, 50))
    ok = ok_green and ok_blue
    print(f"  [{'OK  ' if ok else 'FAIL'}] 亮度用 BT.601 加权（亮绿判浅、深蓝判深）")
    if not ok:
        failures.append("亮度判据")
        print(f"        (0,230,0) is_dark={dwm.is_dark((0,230,0))} 期望 False；"
              f"(20,30,50) is_dark={dwm.is_dark((20,30,50))} 期望 True")

    # 4. 阈值边界的浮点精度（系数和不等于 1.0 导致中灰误判）
    ok = dwm.is_dark((127, 127, 127)) and not dwm.is_dark((128, 128, 128))
    print(f"  [{'OK  ' if ok else 'FAIL'}] 亮度阈值边界正确（127 深 / 128 浅）")
    if not ok:
        failures.append("阈值精度")

    # 5. 文字色必须与底色有对比（不然白底白字）
    dark_bg = (20, 24, 30)
    light_bg = (235, 238, 242)
    fg_d = dwm.contrast_text(dark_bg)
    fg_l = dwm.contrast_text(light_bg)
    ok = (dwm.luminance(fg_d) - dwm.luminance(dark_bg) > 100
          and dwm.luminance(light_bg) - dwm.luminance(fg_l) > 100)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 文字色与底色对比充足（不会白底白字）")
    if not ok:
        failures.append("文字对比度")

    # 5b. 对比度选色（B 方案）：必须是两个候选里对比度高的那个
    #     这是把"亮度过阈值就换色"换成"算 WCAG 对比度择优"后的核心不变式。
    DARK, LIGHT = (24, 26, 31), (240, 242, 245)
    worst, worst_bg = 99.0, None
    for v in range(0, 256, 5):
        bg = (v, v, v)
        pick = dwm.contrast_text(bg)
        best = max(dwm.contrast_ratio(bg, DARK), dwm.contrast_ratio(bg, LIGHT))
        got_r = dwm.contrast_ratio(bg, pick)
        if got_r < best - 1e-9 and got_r < worst:
            worst, worst_bg = got_r, (bg, pick, best)
    ok = worst_bg is None
    print(f"  [{'OK  ' if ok else 'FAIL'}] 对比度选色恒取更优候选（灰阶全扫）")
    if not ok:
        failures.append("对比度择优")
        print("        未取最优:", worst_bg)

    # 5c. 旧阈值判据在灰阶 120-127 选错，新判据必须修好
    #     旧判据在这些点上硬选浅字，对比度只有 3.57~3.94（低于 AA 4.5）
    #     新判据改选深字，回到 4.0~4.35
    fixed = all(dwm.contrast_text((v, v, v)) == DARK for v in (121, 124, 127))
    improved = (dwm.contrast_ratio((127, 127, 127),
                                   dwm.contrast_text((127, 127, 127)))
                > dwm.contrast_ratio((127, 127, 127), LIGHT) + 0.5)
    ok = fixed and improved
    print(f"  [{'OK  ' if ok else 'FAIL'}] 修好旧判据的灰阶 120-127 误选（对比度提升）")
    if not ok:
        failures.append("灰阶误选")
        print(f"        127 灰：新选 {dwm.contrast_text((127,127,127))} "
              f"对比度 {dwm.contrast_ratio((127,127,127), dwm.contrast_text((127,127,127))):.2f}；"
              f"旧选浅字 {dwm.contrast_ratio((127,127,127), LIGHT):.2f}")

    # 5d. WCAG 公式本身：纯黑白应为 21:1，同色应为 1:1
    ok = (abs(dwm.contrast_ratio((255, 255, 255), (0, 0, 0)) - 21.0) < 0.05
          and abs(dwm.contrast_ratio((100, 150, 200), (100, 150, 200)) - 1.0) < 1e-9)
    print(f"  [{'OK  ' if ok else 'FAIL'}] WCAG 对比度公式正确（黑白 21:1、同色 1:1）")
    if not ok:
        failures.append("对比度公式")
        print(f"        黑白={dwm.contrast_ratio((255,255,255),(0,0,0)):.3f} 期望 21.0")

    # 5e. 相对亮度是 gamma 线性化的，不能等于 BT.601 值
    ok = (abs(dwm.relative_luminance((255, 255, 255)) - 1.0) < 1e-6
          and abs(dwm.relative_luminance((0, 0, 0))) < 1e-9
          and abs(dwm.relative_luminance((128, 128, 128)) - 0.2159) < 0.01)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 相对亮度已 sRGB 线性化（中灰≈0.216）")
    if not ok:
        failures.append("相对亮度")
        print(f"        中灰 relative_luminance="
              f"{dwm.relative_luminance((128,128,128)):.4f} 期望≈0.2159")
        print(f"        深底 {dark_bg} → 文字 {fg_d}；浅底 {light_bg} → 文字 {fg_l}")

    # 6. blend 越界要被 clamp
    ok = (dwm.blend((100, 100, 100), (0, 0, 0), 0) == (100, 100, 100)
          and dwm.blend((100, 100, 100), (0, 0, 0), 1) == (0, 0, 0)
          and dwm.blend((100, 100, 100), (0, 0, 0), 5) == (0, 0, 0)
          and dwm.blend((100, 100, 100), (0, 0, 0), -1) == (100, 100, 100))
    print(f"  [{'OK  ' if ok else 'FAIL'}] 混色比例越界被 clamp")
    if not ok:
        failures.append("blend clamp")

    # 6a. **浅色主题不能被拉灰**（老大实机反馈的 bug）
    #     日志铁证：取色 #ffffff 正确，上色却成了 #acadae。
    #     原因是弱化用了"往固定深灰 blend"，纯白被拉成灰。
    #     正确语义是降饱和（往自身灰度拉），中性色应当保持不变。
    neutrals = [(255, 255, 255), (250, 250, 251), (245, 245, 247),
                (0, 0, 0), (18, 18, 18), (128, 128, 128)]
    bad = []
    for c in neutrals:
        got = dwm.desaturate(c, 0.35)
        # 中性色降饱和后，各通道差不应变大，亮度也不该明显变化
        if (abs(dwm.luminance(got) - dwm.luminance(c)) > 3
                or dwm.saturation(got) > dwm.saturation(c) + 0.02):
            bad.append((dwm.to_hex(c), dwm.to_hex(got)))
    print(f"  [{'OK  ' if not bad else 'FAIL'}] 中性色降饱和后保持不变（浅色主题不拉灰）")
    if bad:
        failures.append("中性色拉灰")
        print("        被改动的:", bad)

    # 6b. 饱和色应被降饱和，但**亮度基本不变**
    vivid = (0x22, 0xC1, 0xA3)
    got = dwm.desaturate(vivid, 0.35)
    ok = (dwm.saturation(got) < dwm.saturation(vivid) - 0.1
          and abs(dwm.luminance(got) - dwm.luminance(vivid)) < 3)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 饱和色只降彩度、不明显改明暗")
    if not ok:
        failures.append("饱和色弱化")
        print(f"        {dwm.to_hex(vivid)} 饱和 {dwm.saturation(vivid):.3f}"
              f" -> {dwm.to_hex(got)} 饱和 {dwm.saturation(got):.3f}"
              f"  亮度 {dwm.luminance(vivid):.1f} -> {dwm.luminance(got):.1f}")

    # 6c. desaturate 比例边界
    ok = (dwm.desaturate((200, 50, 50), 0) == (200, 50, 50)
          and dwm.desaturate((200, 50, 50), -1) == (200, 50, 50))
    print(f"  [{'OK  ' if ok else 'FAIL'}] 降饱和比例 0/负值等于不处理")
    if not ok:
        failures.append("desaturate 边界")

    # 6d. 主题跟踪：间隔配置与变更容差
    watch_ms = config.DEFAULTS.get("theme_watch_ms")
    ok = isinstance(watch_ms, int) and watch_ms >= 0
    print(f"  [{'OK  ' if ok else 'FAIL'}] 主题跟踪间隔配置存在（{watch_ms} ms）")
    if not ok:
        failures.append("跟踪间隔配置")

    ok = (g.THEME_CHANGE_TOL > 0 and g._THEME_WATCH_MIN_MS >= 1000)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 变更容差与最小间隔有下限"
          f"（tol={g.THEME_CHANGE_TOL}, min={g._THEME_WATCH_MIN_MS}ms）")
    if not ok:
        failures.append("跟踪下限")

    # 6e. 探针返回值解析（含非法输入）
    cases = [("21,21,23", (21, 21, 23)), ("0,0,0", (0, 0, 0)),
             ("255,255,255", (255, 255, 255))]
    okp = all(g._parse_probe_color(v) == e for v, e in cases)
    badp = [v for v in (None, "", "abc", "1,2", "1,2,3,4", "300,0,0",
                        "-1,0,0", "1;2;3") if g._parse_probe_color(v) is not None]
    ok = okp and not badp
    print(f"  [{'OK  ' if ok else 'FAIL'}] JS 探针返回值解析正确（非法输入返回 None）")
    if not ok:
        failures.append("探针解析")
        print(f"        合法解析={okp} 误判为合法={badp}")

    # 6f. 变更比对：容差内不算变、超出才算变
    def _changed(a, b):
        return not all(abs(a[i] - b[i]) <= g.THEME_CHANGE_TOL for i in range(3))
    ok = (not _changed((255, 255, 255), (253, 254, 255))
          and _changed((255, 255, 255), (21, 21, 23)))
    print(f"  [{'OK  ' if ok else 'FAIL'}] 主题变更判定（白→白不算变，白→深色算变）")
    if not ok:
        failures.append("变更判定")

    # 6b. 性能回归护栏
    #     取色跑在 GUI 线程上，耗时直接变成界面卡顿。优化前逐像素循环
    #     约 20ms，现在是 C 层切片 + translate，约 1.5ms。
    #     阈值故意留得很宽（40ms），只拦"退化回逐像素循环"这种量级的
    #     问题，不做精确计时断言——CI 机器负载抖动会误报。
    import time as _t
    SW2, SH2 = 1152, 48
    _row = b"".join(
        bytes((34, 193, 163, 255) if 100 <= x < 220 else (30, 32, 36, 255))
        for x in range(SW2))
    _buf = _row * SH2
    _n = 5
    _t0 = _t.perf_counter()
    for _ in range(_n):
        dwm.dominant_color(_buf, SW2, SH2)
    _ms = (_t.perf_counter() - _t0) / _n * 1000
    ok = _ms < 40.0
    print(f"  [{'OK  ' if ok else 'FAIL'}] 取色性能未退化（{_ms:.2f} ms < 40ms）")
    if not ok:
        failures.append("取色性能")
        print("        疑似退化为逐像素 Python 循环（优化前约 20ms）")

    # 7. 空白检测（flat_ratio）—— 三层降级取色的基石
    #    未渲染的控件截图整片同色，取出的"主色调"是空白色，
    #    拿它上标题栏比不上色更糟，所以必须能识别出来。
    _flat = bytes((243, 243, 243, 255)) * 500
    _var = bytes((34, 193, 163, 255)) * 250 + bytes((30, 32, 36, 255)) * 250
    fr_flat = dwm.flat_ratio(_flat, 50, 10)
    fr_var = dwm.flat_ratio(_var, 50, 10)
    ok = fr_flat >= dwm.FLAT_RATIO and fr_var < dwm.FLAT_RATIO
    print(f"  [{'OK  ' if ok else 'FAIL'}] 空白检测：纯色 {fr_flat:.3f} / 有内容 {fr_var:.3f}")
    if not ok:
        failures.append("空白检测")

    # 7b. require_variety 必须让纯色返回 None（而不是返回那个空白色）
    ok = (dwm.dominant_color(_flat, 50, 10, require_variety=True) is None
          and dwm.dominant_color(_flat, 50, 10) is not None)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 纯色时 require_variety 返回 None")
    if not ok:
        failures.append("require_variety")

    # 7c. 三层降级取色的骨架必须齐全
    #    单一来源（view.grab()）已证不可靠，必须有降级链。
    need = ["_sample_titlebar_color", "_on_js_theme_color",
            "_sample_by_pixels", "_grab_view_color"]
    missing = [n for n in need if not hasattr(g.MainWindow, n)]
    print(f"  [{'OK  ' if not missing else 'FAIL'}] 三层取色方法齐全（JS/像素/控件）")
    if missing:
        failures.append("取色降级链")
        print("        缺失:", missing)

    # 7d. JS 探针只能用标准 DOM API，不得出现 DSH 私有选择器
    js = getattr(g, "_JS_THEME_PROBE", "")
    uses_std = ("elementFromPoint" in js and "getComputedStyle" in js
                and "parentElement" in js)
    # 私有选择器特征：querySelector 带类名/id、以及 DSH 相关字样
    leaky = [t for t in ("querySelector", "getElementById", "dsh-", "DSH")
             if t in js]
    ok = uses_std and not leaky
    print(f"  [{'OK  ' if ok else 'FAIL'}] JS 探针只用标准 DOM API（无 DSH 私有选择器）")
    if not ok:
        failures.append("JS 探针分层")
        print(f"        标准API={uses_std} 可疑字样={leaky}")

    # 7e. PrintWindow flag 必须是 PW_RENDERFULLCONTENT(2)
    #     用 0 只画客户区，拿不到 DWM 绘制的部分。
    ok = getattr(dwm, "PW_RENDERFULLCONTENT", None) == 2
    print(f"  [{'OK  ' if ok else 'FAIL'}] PrintWindow 使用 PW_RENDERFULLCONTENT")
    if not ok:
        failures.append("PrintWindow flag")

    # 7j. 打包配置与退出码（易回退，且只能在"打包后"暴露问题）
    #
    #     Nuitka 不会自动带上 assets/ —— 少了 --include-data-dir，
    #     图标在源码里能用、打包后静默失效。源码树的测试全绿也测不出。
    wf = os.path.join(ROOT, ".github", "workflows", "release.yml")
    wf_text = open(wf, encoding="utf-8").read() if os.path.isfile(wf) else ""
    ok = "--include-data-dir=assets=assets" in wf_text
    print(f"  [{'OK  ' if ok else 'FAIL'}] 打包命令包含 assets 数据目录")
    if not ok:
        failures.append("打包漏 assets")
        print("        Nuitka 不会自动带上 assets/，图标会静默失效")

    #     **onefile 解包目录必须带版本后缀**（2026-10-01 实测得出）
    #
    #     应用内更新的做法是"替换完文件后、在旧进程还没退出时就拉起新实例"，
    #     所以新旧**必须能同时运行**。而 Nuitka onefile 会把程序解包到一个
    #     目录再跑：共用目录时，旧实例锁着里面那个同名 exe，新实例**根本起不来**
    #     ——实测直接报 `failed to open ... for writing`、退出码 2、0.2 秒就死。
    #     用户看到的是"点了重启，什么都没发生"。
    #
    #     这条只能在打包后暴露，源码树里怎么测都是绿的，所以必须钉住。
    ok = ("--onefile-tempdir-spec=" in wf_text
          and "DSH-Web-runtime-${{ github.ref_name }}" in wf_text)
    print(f"  [{'OK  ' if ok else 'FAIL'}] onefile 解包目录带版本后缀")
    if not ok:
        failures.append("解包目录未按版本隔离")
        print("        新旧版本共用解包目录时，新实例会因文件被锁而起不来")

    #     资源自检必须真的能改变退出码：main() 曾声明 -> None 且裸调，
    #     返回值被丢掉 → 退出码恒 0 → CI 拦不住坏产物。
    src_main = open(os.path.join(ROOT, "dsh_gui_qt.py"),
                    encoding="utf-8").read()
    ok = ("sys.exit(main())" in src_main and "def main() -> int:" in src_main)
    print(f"  [{'OK  ' if ok else 'FAIL'}] main() 返回值驱动退出码")
    if not ok:
        failures.append("退出码未传递")
        print("        裸调 main() 会丢掉资源自检的失败码")

    #     自检本身要能识别"缺资源"（判据存在且指向真实文件）
    ok = callable(getattr(g, "resource_report", None)) and hasattr(
        g, "RESOURCE_CHECK_ENV")
    print(f"  [{'OK  ' if ok else 'FAIL'}] 打包产物资源自检入口存在")
    if not ok:
        failures.append("资源自检缺失")

    #     stdout 必须强制 UTF-8：CI 的 windows runner 是 cp1252，
    #     主程序里打印中文会 UnicodeEncodeError，而**异常会把返回码一起
    #     带走** —— 资源自检因此报过退出码 1（实际是"通过"被异常打断）。
    ok = "sys.stdout.reconfigure(" in src_main
    print(f"  [{'OK  ' if ok else 'FAIL'}] 主程序已强制 stdout UTF-8（cp1252 兜底）")
    if not ok:
        failures.append("stdout 编码")
        print("        CI runner 是 cp1252，打印中文会崩并吞掉返回码")

    # 7f. 图标必须随主题切换（黑色图标在深色标题栏上看不见）
    light_p = g.ICON_FOR_DARK_BG     # 深底用（白图标）
    dark_p = g.ICON_FOR_LIGHT_BG     # 浅底用（深图标）
    have = os.path.isfile(light_p) and os.path.isfile(dark_p)
    differs = False
    if have:
        import hashlib
        differs = (hashlib.md5(open(light_p, "rb").read()).hexdigest()
                   != hashlib.md5(open(dark_p, "rb").read()).hexdigest())
    ic_l = g.icon_for(True)
    ic_d = g.icon_for(False)
    ok = have and differs and not ic_l.isNull() and not ic_d.isNull()
    print(f"  [{'OK  ' if ok else 'FAIL'}] 深浅两套图标存在且不同")
    if not ok:
        failures.append("主题图标")
        print(f"        存在={have} 不同={differs} "
              f"可加载={not ic_l.isNull()}/{not ic_d.isNull()}")

    # 7g. 取色重试节奏：必须是递增退避，且总时长合理（别退化成秒级死等）
    retry = list(getattr(g.MainWindow, "_TB_RETRY_MS", []))
    total = sum(retry)
    ok = (len(retry) >= 3 and all(retry[i] < retry[i + 1]
                                  for i in range(len(retry) - 1))
          and 1000 <= total <= 15000)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 取色重试递增退避（{len(retry)} 次共 {total}ms）")
    if not ok:
        failures.append("取色重试节奏")
        print(f"        实际: {retry}")

    # 7h. 不能残留"固定死等"式取色触发
    #     老大反馈过"加载完网页后有明显延迟"，罪魁就是 QTimer.singleShot(4000)。
    #     注意匹配要带 QTimer. 前缀——docstring 里为说明历史也写了
    #     "singleShot(4000, ...)"，不带前缀会误伤自己。
    src = open(os.path.join(ROOT, "dsh_gui_qt.py"), encoding="utf-8").read()
    ok = ("QTimer.singleShot(4000" not in src
          and "loadFinished.connect(self._on_view_loaded)" in src)
    print(f"  [{'OK  ' if ok else 'FAIL'}] 取色由 loadFinished 触发（无固定死等）")
    if not ok:
        failures.append("取色触发方式")

    # 7i. 降级链与图标方法齐备（借用方法时容易漏掉依赖）
    need2 = ["_on_view_loaded", "_schedule_titlebar_probe", "_set_theme_icon"]
    missing2 = [n for n in need2 if not hasattr(g.MainWindow, n)]
    print(f"  [{'OK  ' if not missing2 else 'FAIL'}] 重试/图标方法齐备")
    if missing2:
        failures.append("方法缺失")
        print("        缺失:", missing2)

    # 8. ctypes 签名必须显式声明
    #    这是踩过的坑：不声明 argtypes 时 HWND 按 C int 封送，
    #    64 位下句柄稍大就传错值。声明是硬要求，不是优化。
    try:
        dwm._dwmapi()
        has_sig = bool(ctypes.windll.dwmapi.DwmSetWindowAttribute.argtypes)
    except Exception:                                       # noqa: BLE001
        has_sig = False
    print(f"  [{'OK  ' if has_sig else 'FAIL'}] DwmSetWindowAttribute 已声明 argtypes")
    if not has_sig:
        failures.append("ctypes 签名")

    # 8. 明确记录"只写属性"的事实，防止后人又拿 Get 当验收标准
    #    DWMWA_CAPTION_COLOR(35) 等不支持 DwmGetWindowAttribute，
    #    对它回读恒得 E_INVALIDARG(0x80070057)，与设置成功与否无关。
    ok = (dwm.DWMWA_CAPTION_COLOR == 35
          and dwm.DWMWA_TEXT_COLOR == 36
          and dwm.DWMWA_BORDER_COLOR == 34
          and dwm.DWMWA_COLOR_DEFAULT == 0xFFFFFFFF)
    print(f"  [{'OK  ' if ok else 'FAIL'}] DWM 属性常量正确（含只写属性语义）")
    if not ok:
        failures.append("DWM 常量")

    return failures


def theme_checks() -> list[str]:
    """DSH 美术转译层（dsh_theme）的回归断言。

    重点不是"好不好看"，而是三件会被静默破坏的事：
      1. 提示窗不能退回系统 QMessageBox（配色/字体全不受控）；
      2. 托盘菜单必须真的套上样式，且圆角前提（半透明底）在；
      3. 转译层的调色板必须与面板同源，不能出现第二份色表。
    """
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")
        if not ok:
            failures.append(name)

    import dsh_panel
    import dsh_theme
    from PySide6.QtWidgets import (QGraphicsDropShadowEffect, QMenu,
                                   QMessageBox)

    pal = dsh_theme.palette("light")
    check("转译层调色板与面板同源",
          all(pal[k] == dsh_panel.palette("light")[k] for k in ("bg", "text", "line")))
    check("圆角表对齐官方 --dsw-radius-*",
          (dsh_theme.RADIUS["sm"], dsh_theme.RADIUS["md"], dsh_theme.RADIUS["lg"],
           dsh_theme.RADIUS["xl"], dsh_theme.RADIUS["panel"]) == (8, 12, 16, 20, 28))
    check("字号阶梯用 px 且含 14/13/12 三档",
          {14, 13, 12} <= {t[0] for t in dsh_theme.TYPE.values()})

    dlg = dsh_theme.MessageDialog(None, "测试标题",
                                  "这是一段正文，用来检查提示窗的字体与配色。", "warn")
    dlg.show()
    QApplication.processEvents()
    check("提示窗不是系统 QMessageBox", not dlg.findChildren(QMessageBox))
    check("提示窗套上了官方样式（非空样式表）", bool(dlg.styleSheet()))
    body = dlg.findChild(g.QLabel, "dlgBody")
    check("正文用了 dlgBody 字级", body is not None)
    check("主按钮是 primary 形态", dlg.btn_ok.objectName() == "primary")
    # 像素级：窗口底色必须等于**当前主题**的 bg 令牌
    expect = dsh_theme.palette_now()["bg"].lower()
    img = dlg.grab().toImage()
    c = img.pixelColor(max(2, img.width() // 2), max(2, img.height() - 6))
    check("提示窗底色等于官方 bg 令牌", c.name().lower() == expect,
          "%s（应 %s）" % (c.name(), expect))
    dlg.close()

    menu = QMenu()
    dsh_theme.apply_menu(menu, dsh_theme.palette("dark"))
    check("菜单套上了样式", "border-radius" in menu.styleSheet())
    check("菜单开了半透明底（圆角前提）",
          menu.testAttribute(g.Qt.WidgetAttribute.WA_TranslucentBackground))
    # 半透明圆角 + 原生窗口阴影 = 阴影按矩形裁切，表现为右下黑边。
    check("菜单关掉了原生窗口阴影",
          bool(menu.windowFlags() & g.Qt.WindowType.NoDropShadowWindowHint))
    check("菜单是无边框窗口（阴影不再沿矩形边缘画）",
          bool(menu.windowFlags() & g.Qt.WindowType.FramelessWindowHint))
    # 真正的元凶（2026-10-19 实测）：QWindows11Style::polish() 给每个 QMenu
    # 装了 QGraphicsDropShadowEffect(blur 3, offset 3,3)。效果画在窗口内部，
    # 而窗口和菜单同大 → 右/下被硬裁一条。它不是 DWM 阴影，
    # NoDropShadowWindowHint / DWMWA_NCRENDERING_POLICY 都管不到。
    check("菜单已挂上「显示时清阴影」的钩子",
          bool(getattr(menu, "_dsh_shadow_hooked", False)))
    eff = QGraphicsDropShadowEffect(menu)
    eff.setBlurRadius(3)
    eff.setOffset(3, 3)
    menu.setGraphicsEffect(eff)
    check("strip_shadow 能移除 Qt 的裁切阴影",
          bool(dsh_theme.strip_shadow(menu)) and menu.graphicsEffect() is None)
    check("strip_shadow 没有阴影时安全返回",
          dsh_theme.strip_shadow(menu) is False)
    menu.deleteLater()

    # 真实显示路径：polish() 在 show 时才跑（此时才装阴影），showEvent 清掉。
    live = dsh_theme.Menu(pal=dsh_theme.palette("light"))
    # 要够高：像素扫描只取直边中段，太矮的菜单整段都在圆角里。
    for i in range(9):
        live.addAction("菜单项 %d" % i)
    # 先手动装一个同样的投影：offscreen 平台用的是 fusion，QWindows11Style
    # 不会自己装；这样断言在任何平台上都能真正验证 showEvent 的清理。
    manual = QGraphicsDropShadowEffect(live)
    manual.setBlurRadius(3)
    manual.setOffset(3, 3)
    live.setGraphicsEffect(manual)
    live.show()
    QApplication.processEvents()
    check("菜单显示后不带 Qt 裁切阴影", live.graphicsEffect() is None,
          repr(live.graphicsEffect()))
    # 像素级反向断言：右/下 3px 内不应有"半透明且暗"的像素。
    # 修复前 offset(3,3) 的阴影正是以 a≈180、rgb≈59 出现在整条直边里；
    # 只扫中间 60% —— 圆角处的描边抗锯齿也会产生半透明像素，那是正常的。
    img = live.grab().toImage()
    dark = 0
    y0, y1 = img.height() // 5, img.height() * 4 // 5
    for x in range(max(0, img.width() - 3), img.width()):
        for y in range(y0, y1):
            c = img.pixelColor(x, y)
            if 20 < c.alpha() < 240 and c.red() < 200:
                dark += 1
    x0, x1 = img.width() // 5, img.width() * 4 // 5
    for y in range(max(0, img.height() - 3), img.height()):
        for x in range(x0, x1):
            c = img.pixelColor(x, y)
            if 20 < c.alpha() < 240 and c.red() < 200:
                dark += 1
    check("菜单右下没有半透明暗边（阴影未裁切）", dark == 0,
          "半透明暗像素 %d" % dark)
    live.close()

    # 更新说明：markdown / html 都要渲染，不能把标记原样打出来。
    notes = dsh_theme.NotesView(pal=dsh_theme.palette("light"))
    notes.show_notes("## 标题\n\n正文 **加粗**\n\n    print(1)\n", "markdown")
    plain = notes.document().toPlainText()
    check("更新说明 markdown 已渲染（无 ## / 代码围栏）",
          "标题" in plain and "##" not in plain and "```" not in plain)
    check("更新说明代码字体已换成 Cascadia Mono",
          "Courier New" not in notes.document().toHtml())
    notes.show_notes("<p>正文</p><script>alert(1)</script>"
                     "<img src='https://example.com/x.png'>", "html")
    check("更新说明 HTML 去掉了 script / img",
          "alert" not in notes.document().toPlainText()
          and "<img" not in notes.document().toHtml().lower())
    check("更新说明视图只读", notes.isReadOnly())

    # 防止以后有人把菜单换回裸 QMenu：裸 QMenu 的 show() 不经过 aboutToShow。
    src = open(os.path.join(ROOT, "dsh_gui_qt.py"), encoding="utf-8").read()
    check("托盘菜单走 dsh_theme.Menu（不退回裸 QMenu）",
          "dsh_theme.Menu()" in src and "QMenu()" not in src)

    return failures


def terminal_checks() -> list[str]:
    """开发者面板「终端」页：能否读到输出、整段复制、暂停/归位、搜索。"""
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")
        if not ok:
            failures.append(name)

    import dsh_console
    import dsh_devtools
    import dsh_theme

    buf = dsh_console.ConsoleBuffer()
    buf.append_text("INFO 启动完成\n")
    buf.append_text("[ERROR:CONSOLE(1)] boom 失败\n")
    view = dsh_devtools.TerminalView(dsh_theme.palette("light"), console=buf)
    view.resize(900, 500)
    view.show()
    QApplication.processEvents()
    view.tick()

    text = view.editor.toPlainText()
    check("终端页读到采集缓冲", "ERROR:CONSOLE" in text and "启动完成" in text)
    check("高亮器已挂上", view.highlighter is not None)
    check("状态栏报出错误数", "错误 1" in view.status.text(), view.status.text())

    view._copy_all()
    clip = QApplication.clipboard().text()
    check("复制全部拿到完整历史（可连续粘贴）",
          "boom" in clip and clip.count("\n") >= 1, "%d 字符" % len(clip))

    view.btn_follow.setChecked(False)
    buf.append_text("暂停期间的一行\n")
    view.tick()
    check("暂停时不再自动追加到底部",
          "暂停期间的一行" in view.editor.toPlainText())  # 仍会入缓冲
    check("暂停状态文字提示新输出", "已暂停" in view.status.text(), view.status.text())
    view.jump_to_end()
    check("回到底部恢复跟随", view.btn_follow.isChecked())

    view.search.setText("boom")
    view._search()
    check("搜索能选中命中项", "boom" in view.editor.textCursor().selectedText(),
          view.editor.textCursor().selectedText()[:20])

    view.apply_theme(dsh_theme.palette("dark"))
    check("终端页能跟随主题切换", view.pal["bg"] == dsh_theme.palette("dark")["bg"])
    view.close()
    return failures


def main() -> int:
    #: CI 里没有装 DSH，起不了真实服务。设此变量只跑不依赖 DSH 的部分
    #: （引导窗口渲染、对话框文案、自更新纯逻辑），
    #: 外加不需要服务的菜单结构检查。
    static_only = os.environ.get("DSH_GUI_SELFTEST_STATIC") == "1"

    app = QApplication([])

    print("== 首次运行引导窗口（合成报告，不触发安装）==")
    state["onboarding_failures"] = onboarding_checks()
    print()

    print("== 更新对话框的数据安全说明 ==")
    state["dialog_copy_failures"] = update_dialog_copy_checks()
    print()

    print("== 桌面壳自更新（纯逻辑，不联网）==")
    state["host_update_failures"] = host_update_checks()
    print()

    print("== 标题栏取色与配色（纯逻辑）==")
    state["titlebar_failures"] = titlebar_checks()
    print()

    print("== 控制面板（无边框固定尺寸 / 图标 / 页签 / 像素级底色）==")
    import dsh_panel
    state["panel_failures"] = dsh_panel.selftest_checks()
    print()

    print("== 美术转译层（提示窗 / 托盘菜单 / 令牌同源）==")
    state["theme_failures"] = theme_checks()
    print()

    print("== 开发者模式（外链出口策略 / 记录器）==")
    import dsh_devtools
    state["devtools_failures"] = dsh_devtools.selftest_checks()
    print()

    print("== 终端输出采集（切行 / 环形 / 打码）==")
    import dsh_console
    state["console_failures"] = dsh_console.selftest_checks()
    print()

    print("== 开发者面板终端页（复制 / 暂停 / 归位 / 搜索）==")
    state["terminal_failures"] = terminal_checks()
    print()

    if static_only:
        # 菜单结构检查不需要服务在跑——它只读菜单项文本与启用状态。
        # 但 MainWindow 的构造函数会立刻启动服务，所以这里用不启动
        # 服务的方式构造：直接跳过窗口，单独验证菜单定义。
        print("== 静态模式下跳过真实启动链路（未安装 DSH）==")
        bad = (state.get("onboarding_failures") or state.get("dialog_copy_failures")
               or state.get("host_update_failures") or state.get("titlebar_failures")
               or state.get("panel_failures") or state.get("devtools_failures")
               or state.get("theme_failures") or state.get("console_failures")
               or state.get("terminal_failures"))
        if bad:
            print("\n用例失败：", "、".join(bad))
            return 1
        print("\nGUI 静态检查通过（未覆盖真实启动链路）")
        return 0

    win = g.MainWindow()
    win.show()

    print("== 手动「检查更新」的反馈路径 ==")
    state["feedback_failures"] = feedback_checks(win)
    print()

    print("== 托盘菜单结构与语义 ==")
    state["menu_failures"] = menu_structure_checks(win)
    print()

    win.starter.progress.connect(lambda s: print("  progress:", s))

    def on_ready(handle):
        state["handle"] = handle
        state["ready_checks_done"] = False
        print("  ready  :", handle.install.describe())
        print("  port   :", handle.port)
        print("  url    :", handle.url)
        print("  webview:", "已创建" if win.view is not None else "未创建")
        # 右键必须交还给网页。默认情况下 QWebEngineView 每次右键都弹它自己的
        # 标准菜单（刷新/保存），而且那是在 widget 层生成的、**网页 preventDefault
        # 拦不住**，会盖住网页自己的右键菜单。（老大实测反馈过。）
        if win.view is not None:
            pol = win.view.contextMenuPolicy()
            ok = pol == g.Qt.ContextMenuPolicy.NoContextMenu
            print(f"  [{'OK  ' if ok else 'FAIL'}] webview 右键已交还网页"
                  f"（contextMenuPolicy={pol.name}）")
            if not ok:
                state.setdefault("webview_failures", []).append("右键菜单未交还网页")

        # 外链策略：异源必须被拦下并交给"外部打开"回调，同源必须留在内核里。
        # 这两条是 2026-10-01 那个"点超链接没反应"的直接回归断言。
        from PySide6.QtCore import QUrl
        from PySide6.QtWebEngineCore import QWebEnginePage
        nav = QWebEnginePage.NavigationType.NavigationTypeLinkClicked
        opened: list = []
        real_open = win.page._open_external
        win.page._open_external = lambda u: opened.append(u.toString())
        try:
            ext = win.page.acceptNavigationRequest(
                QUrl("https://example.com/account"), nav, True)
            inner = QUrl(handle.url)
            inner.setPath("/chat")
            same = win.page.acceptNavigationRequest(inner, nav, True)
        finally:
            win.page._open_external = real_open
        ok = (ext is False and same is True and bool(opened)
              and "example.com" in opened[0])
        print(f"  [{'OK  ' if ok else 'FAIL'}] 外链交系统浏览器、同源留在内核"
              f"（external={ext} same={same} opened={opened[:1]}）")
        if not ok:
            state.setdefault("webview_failures", []).append("外链策略未生效")

        ok = (isinstance(win.page, dsh_devtools.ShellPage)
              and win.interceptor is not None)
        print(f"  [{'OK  ' if ok else 'FAIL'}] 页面已换成 ShellPage 且挂了网络记录器")
        if not ok:
            state.setdefault("webview_failures", []).append("页面策略未挂上")

        # --- 资源纪律：开发者模式关闭时不该有任何采集在跑 ---
        # 老大的硬要求："没启用就不该占资源"。这里逐项验证接线而不是"标志位"。
        import dsh_console
        from PySide6.QtWebEngineCore import QWebEnginePage as _Page

        err_lvl = _Page.JavaScriptConsoleMessageLevel.ErrorMessageLevel
        check_off = []
        ok = dsh_console.capture() is None
        print(f"  [{'OK  ' if ok else 'FAIL'}] 关闭时不安装进程输出采集"
              f"（thread={'无' if ok else '有'}）")
        if not ok:
            state.setdefault("webview_failures", []).append("关闭时仍在采集进程输出")

        ok = win.page.capture_console is False
        print(f"  [{'OK  ' if ok else 'FAIL'}] 关闭时页面不采集控制台")
        if not ok:
            state.setdefault("webview_failures", []).append("关闭时仍采集控制台")

        before = len(win.recorder.since_console(0))
        win.page.javaScriptConsoleMessage(err_lvl, "off-probe", 1, "probe.js")
        ok = len(win.recorder.since_console(0)) == before
        print(f"  [{'OK  ' if ok else 'FAIL'}] 关闭时控制台消息不入库")
        if not ok:
            state.setdefault("webview_failures", []).append("关闭时仍记录控制台")

        win._apply_dev_mode(True)
        try:
            ok = dsh_console.capture() is not None and win.page.capture_console is True
            print(f"  [{'OK  ' if ok else 'FAIL'}] 开启后采集与页面记录同时就位")
            if not ok:
                state.setdefault("webview_failures", []).append("开启后采集未就位")
            win.page.javaScriptConsoleMessage(err_lvl, "on-probe", 1, "probe.js")
            ok = any(e.message == "on-probe" for e in win.recorder.since_console(0))
            print(f"  [{'OK  ' if ok else 'FAIL'}] 开启后控制台消息能入库")
            if not ok:
                state.setdefault("webview_failures", []).append("开启后未记录控制台")
        finally:
            win._apply_dev_mode(False)

        ok = dsh_console.capture() is None and win.page.capture_console is False
        print(f"  [{'OK  ' if ok else 'FAIL'}] 再次关闭后采集被真正停掉")
        if not ok:
            state.setdefault("webview_failures", []).append("关闭后采集未停止")

        # 新窗口请求：Qt 的 QWebEngineNewWindowRequest **没有** accept()/reject()，
        # 误调会抛异常，而异常点在"交给浏览器"之前 → 表现是"点了没反应"。
        # 用一个只有 requestedUrl() 的假对象来钉住这条：不许再碰别的成员。
        class _FakeReq:
            def __init__(self, u):
                self._u = QUrl(u)

            def requestedUrl(self):
                return self._u

        popup: list = []
        real_open = win.page._open_external
        win.page._open_external = lambda u: popup.append(u.toString())
        detail = ""
        try:
            win.page._on_new_window(_FakeReq("https://platform.deepseek.com/usage"))
            ok = any("platform.deepseek.com" in u for u in popup)
        except Exception as e:                                 # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        finally:
            win.page._open_external = real_open
        print(f"  [{'OK  ' if ok else 'FAIL'}] target=_blank 的异源链接交给浏览器"
              f"{('  ' + detail) if detail else ''}")
        if not ok:
            state.setdefault("webview_failures", []).append("新窗口请求未交浏览器")
        print("  栈页数 :", win.stack.count())
        print("  菜单项 :", [a.text() for a in win.tray.contextMenu().actions()
                             if not a.isSeparator()])
        print("  更新菜单可用性:", {
            "回归稳定版": win.act_stable.isEnabled(),
            "回滚": win.act_rollback.isEnabled(),
            "预览计划(勾选)": win.act_prev.isChecked(),
        })
        state["ready_checks_done"] = True
        QTimer.singleShot(800, app.quit)

    def on_failed(msg):
        state["error"] = msg
        print("  FAILED :", msg)
        app.quit()

    win.starter.ready.connect(on_ready)
    win.starter.failed.connect(on_failed)
    QTimer.singleShot(90000, app.quit)
    app.exec()

    # 失败项集中在这里汇总。**静态与非静态两条路径必须查同一批 key**——
    # 之前非静态这条路漏了 panel_failures，等于面板自测在冒烟测试里形同虚设。
    bad = (state.get("onboarding_failures") or state.get("feedback_failures")
           or state.get("dialog_copy_failures") or state.get("host_update_failures")
           or state.get("titlebar_failures") or state.get("menu_failures")
           or state.get("panel_failures") or state.get("webview_failures")
           or state.get("devtools_failures") or state.get("theme_failures")
           or state.get("console_failures") or state.get("terminal_failures"))
    # on_ready 里一旦抛异常，后面的检查会被静默跳过、测试却仍然"通过"。
    # 用一个完成标记把这种情况变成失败。
    if "handle" in state and not state.get("ready_checks_done"):
        bad = list(bad or []) + ["ready 检查未跑完（on_ready 中途异常）"]
    if "handle" in state and not bad:
        print("\nGUI 冒烟测试通过")
        return 0
    if bad:
        print("\n用例失败：", "、".join(bad))
        return 1
    print("\nGUI 冒烟测试失败：", state.get("error", "超时"))
    return 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)      # QWebEngine 的收尾线程不保证干净退出，直接结束进程
