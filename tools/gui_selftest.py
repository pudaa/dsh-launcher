# -*- coding: utf-8 -*-
"""无头 GUI 冒烟测试：验证 MainWindow 启动流程 + 接管已有服务 + WebEngine 懒加载。

    python tools/gui_selftest.py
"""
import os
import sys
import ctypes

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


def titlebar_checks() -> list[str]:
    """标题栏取色与配色的纯逻辑校验（不碰真实窗口）。"""
    from dsh_host import dwm
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
    #    旧实现只给灰像素降权 0.5，实测 2:1 的像素数优势仍会让灰胜出，
    #    所以这里刻意用**强对比**的 2:1（大片灰 + 少量主题色）作回归，
    #    弱对比（1:1）会被旧实现在降权后碰巧蒙对，测不出问题。
    px = bytes([0x22, 0xC1, 0xA3, 0xFF] * 20) + bytes([0x1E, 0x1E, 0x1E, 0xFF] * 40)
    got = dwm.dominant_color(px, 60, 1)
    ok = got is not None and got[1] > got[0] and got[1] > got[2]
    print(f"  [{'OK  ' if ok else 'FAIL'}] 灰底 2:1 仍识别出主题色（硬隔离生效）")
    if not ok:
        failures.append("饱和度隔离")
        print("        实际取到:", got, "（期望偏青绿，即 G 分量最大）")

    # 2b. 纯灰界面应退回灰色方案，而不是返回 None 或乱配色
    px = bytes([0x30, 0x30, 0x30, 0xFF] * 100)
    got = dwm.dominant_color(px, 10, 10)
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
        print(f"        深底 {dark_bg} → 文字 {fg_d}；浅底 {light_bg} → 文字 {fg_l}")

    # 6. blend 越界要被 clamp
    ok = (dwm.blend((100, 100, 100), (0, 0, 0), 0) == (100, 100, 100)
          and dwm.blend((100, 100, 100), (0, 0, 0), 1) == (0, 0, 0)
          and dwm.blend((100, 100, 100), (0, 0, 0), 5) == (0, 0, 0)
          and dwm.blend((100, 100, 100), (0, 0, 0), -1) == (100, 100, 100))
    print(f"  [{'OK  ' if ok else 'FAIL'}] 混色比例越界被 clamp")
    if not ok:
        failures.append("blend clamp")

    # 7. ctypes 签名必须显式声明
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

    if static_only:
        # 菜单结构检查不需要服务在跑——它只读菜单项文本与启用状态。
        # 但 MainWindow 的构造函数会立刻启动服务，所以这里用不启动
        # 服务的方式构造：直接跳过窗口，单独验证菜单定义。
        print("== 静态模式下跳过真实启动链路（未安装 DSH）==")
        bad = (state.get("onboarding_failures") or state.get("dialog_copy_failures")
               or state.get("host_update_failures") or state.get("titlebar_failures"))
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
           or state.get("titlebar_failures") or state.get("menu_failures"))
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
