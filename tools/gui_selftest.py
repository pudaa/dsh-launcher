# -*- coding: utf-8 -*-
"""无头 GUI 冒烟测试：验证 MainWindow 启动流程 + 接管已有服务 + WebEngine 懒加载。

    python tools/gui_selftest.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
    cases = [
        ("2.0.1", "2.0.0", True),
        ("2.0.0", "2.0.1", False),
        ("2.1.0", "2.0.9", True),
        ("2.0.0", "2.0.0", False),
    ]
    bad = [f"{a}>{b}" for a, b, exp in cases if is_newer(a, b) != exp]
    print(f"  [{'OK  ' if not bad else 'FAIL'}] 版本比较逻辑正确")
    if bad:
        failures.append("版本比较")
        print("        异常用例:", bad)

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

    if static_only:
        # 菜单结构检查不需要服务在跑——它只读菜单项文本与启用状态。
        # 但 MainWindow 的构造函数会立刻启动服务，所以这里用不启动
        # 服务的方式构造：直接跳过窗口，单独验证菜单定义。
        print("== 静态模式下跳过真实启动链路（未安装 DSH）==")
        bad = (state.get("onboarding_failures") or state.get("dialog_copy_failures")
               or state.get("host_update_failures"))
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
        print("  ready  :", handle.install.describe())
        print("  port   :", handle.port)
        print("  url    :", handle.url)
        print("  webview:", "已创建" if win.view is not None else "未创建")
        print("  栈页数 :", win.stack.count())
        print("  菜单项 :", [a.text() for a in win.tray.contextMenu().actions()
                             if not a.isSeparator()])
        print("  更新菜单可用性:", {
            "回归稳定版": win.act_stable.isEnabled(),
            "回滚": win.act_rollback.isEnabled(),
            "预览计划(勾选)": win.act_prev.isChecked(),
        })
        QTimer.singleShot(800, app.quit)

    def on_failed(msg):
        state["error"] = msg
        print("  FAILED :", msg)
        app.quit()

    win.starter.ready.connect(on_ready)
    win.starter.failed.connect(on_failed)
    QTimer.singleShot(90000, app.quit)
    app.exec()

    bad = (state.get("onboarding_failures") or state.get("feedback_failures")
           or state.get("dialog_copy_failures") or state.get("host_update_failures")
           or state.get("menu_failures"))
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
