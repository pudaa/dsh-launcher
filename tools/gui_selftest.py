# -*- coding: utf-8 -*-
"""无头 GUI 冒烟测试：验证 MainWindow 启动流程 + 接管已有服务 + WebEngine 懒加载。

    python tools/gui_selftest.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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

    win._info = lambda t: seen.append(("info", t))
    win._warn = lambda t: seen.append(("warn", t))
    win._show_update_dialog = lambda t: seen.append(("dialog", t))

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


def main() -> int:
    app = QApplication([])

    print("== 首次运行引导窗口（合成报告，不触发安装）==")
    state["onboarding_failures"] = onboarding_checks()
    print()

    print("== 更新对话框的数据安全说明 ==")
    state["dialog_copy_failures"] = update_dialog_copy_checks()
    print()

    win = g.MainWindow()
    win.show()

    print("== 手动「检查更新」的反馈路径 ==")
    state["feedback_failures"] = feedback_checks(win)
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
           or state.get("dialog_copy_failures"))
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
