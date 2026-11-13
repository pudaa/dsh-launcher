# -*- coding: utf-8 -*-
"""入口与界面层共用的零碎工具。

为什么要有这个文件
----------------
`MainWindow` 拆成多个 mixin 之后，它们都要用 `ASSET_DIR`、`icon_for`、
`resource_report` 这些工具。如果这些还留在 `dsh_gui_qt.py` 里，
mixin 就只能反向 import 入口——循环依赖。所以先把"跟窗口无关的工具"
收到这里。

`ROOT` 的算法变了一点：以前是入口文件所在目录，现在是**包的上一级**
（源码跑 = 项目根；打包后 = onefile 解包根）。两者都指向 `assets/` 的父目录，
所以 `_asset_dir()` 的候选顺序仍然成立。
"""
from __future__ import annotations

import os
import sys

from PySide6.QtGui import QIcon

from dsh_host import config, selfupdate
from dsh_host.version import HOST_NAME


APP_TITLE = "DSH — DeepSeek Harness"

SINGLETON_ID = "DSH-Web-singleton"

ROOT = os.path.dirname(os.path.abspath(__file__))

def _asset_dir() -> str:
    """定位 assets 目录。**必须兼容打包后的布局。**

    这个函数存在的理由（2026-09-21 踩坑）
    -------------------------------------
    打包命令原先只有 `--windows-icon-from-ico=assets/icon.ico`，
    那只是把图标塞进 **exe 自身的 PE 资源**（资源管理器看的那个）。
    而运行时 `QIcon("assets/xxx.ico")` 是去**文件系统**读的 ——
    Nuitka onefile 不会自动带上 assets/ 目录，于是 QIcon 拿到空对象，
    `isNull()` 为真，图标切换**静默失效**（回落到 exe 资源里的那个）。

    所以两件事缺一不可：
      1. 打包命令要有 `--include-data-dir=assets=assets`
      2. 这里要能在"解包目录 / exe 同目录 / 源码目录"里找到 assets

    候选顺序覆盖三种运行形态：源码直跑、onefile 解包、standalone 目录。
    """
    bases: list[str] = []
    try:
        bases.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:                                          # pragma: no cover
        pass
    # Nuitka 打包后 __compiled__ 存在；frozen 覆盖其他打包器
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        bases.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    bases.append(os.getcwd())
    for b in bases:
        p = os.path.join(b, "assets")
        if os.path.isdir(p):
            return p
    # 全都找不到就返回首个候选下的 assets —— 后续 os.path.isfile 判空即可。
    # 不要在这里抛异常：图标缺失不该阻止应用启动。
    return os.path.join(bases[0] if bases else ".", "assets")

ASSET_DIR = _asset_dir()

ICON_PATH = os.path.join(ASSET_DIR, "icon.ico")
ICON_FOR_DARK_BG = os.path.join(ASSET_DIR, "icon-light.ico")
ICON_FOR_LIGHT_BG = os.path.join(ASSET_DIR, "icon-dark.ico")

def icon_for(dark_bg: bool) -> QIcon:
    """按底色深浅挑图标。两套都找不到就退回内置单色图标。"""
    path = ICON_FOR_DARK_BG if dark_bg else ICON_FOR_LIGHT_BG
    if not os.path.isfile(path):
        path = ICON_PATH
    return QIcon(path) if os.path.isfile(path) else QIcon()

def resource_report() -> tuple[str, bool]:
    """检查运行环境里的资源与能力是否齐备。返回 (报告文本, 是否通过)。

    只做纯粹的自检，不涉及业务逻辑。
    """
    packaged = ("__compiled__" in globals()) or bool(getattr(sys, "frozen", False))
    lines = [
        f"{HOST_NAME} 产物自检",
        "  sys.frozen       = %s" % getattr(sys, "frozen", False),
        "  __compiled__     = %s" % ("__compiled__" in globals()),
        "  __file__         = %s" % globals().get("__file__", "(无)"),
        "  argv[0]          = %s" % (sys.argv[0] if sys.argv else "(无)"),
        "  sys.executable   = %s" % getattr(sys, "executable", "(无)"),
        "  ASSET_DIR        = %s" % ASSET_DIR,
        "  ASSET_DIR 存在    = %s" % os.path.isdir(ASSET_DIR),
    ]
    ok = True
    for label, p in (("icon.ico ", ICON_PATH),
                     ("icon-light", ICON_FOR_DARK_BG),
                     ("icon-dark ", ICON_FOR_LIGHT_BG)):
        exists = os.path.isfile(p)
        size = os.path.getsize(p) if exists else 0
        ic = QIcon(p) if exists else QIcon()
        loadable = not ic.isNull()
        if not (exists and loadable):
            ok = False
        lines.append("  %s  存在=%-5s 大小=%-7d 可加载=%s  %s"
                     % (label, exists, size, loadable, p))

    # --- 自更新可用性 ---
    # 这条专门防一个会静默失效的坑：Nuitka 不设 sys.frozen，
    # 若 running_as_exe() 只看 frozen，打包版会被判成"源码运行" →
    # 自更新菜单项被禁用 → 功能直接死掉。必须在产物上验。
    try:
        supported, reason = selfupdate.self_update_supported()
        exe = selfupdate.current_exe()
    except Exception as e:                                     # noqa: BLE001
        supported, reason, exe = False, "%s: %s" % (type(e).__name__, e), "?"
    lines.append("  自更新可用        = %s%s"
                 % (supported, ("  (%s)" % reason) if reason else ""))
    lines.append("  current_exe      = %s" % exe)
    if packaged and not supported:
        ok = False
        lines.append("  ↑ 打包版里自更新应当可用，否则菜单项会被禁用")
    if packaged and not exe.lower().endswith(".exe"):
        ok = False
        lines.append("  ↑ 打包版里 current_exe 应指向 .exe")

    # --- 内联 SVG 图标链路 ---
    # 控制面板的图标是**内联 SVG**，靠 PySide6.QtSvg 在运行时渲染。
    # 这是本项目第一次在打包产物里用到 QtSvg：如果打包时 QtSvg 的库没被收进去，
    # 源码里一切正常、打出来的 exe 却起不来（或图标全部空白）。
    # 资源树里有没有 SVG 文件是看不出来的（我们本来就不读文件），所以只能
    # **真的渲染一个图标**来验。
    try:
        from dsh_ui import icons as dsh_icons
        probe_name = "circle-check"
        ratio = dsh_icons.ink_ratio(probe_name, 48, "#ffffff")
        good = 0.02 < ratio < 0.60
        icon_ok = not dsh_icons.missing([probe_name])
        lines.append("  图标数            = %d" % len(dsh_icons.SVG))
        lines.append("  SVG 渲染上色比例   = %.3f（%s）"
                     % (ratio, "正常" if good else "异常"))
        if not (good and icon_ok):
            ok = False
            lines.append("  ↑ QtSvg 渲染异常：打包时可能漏收了 QtSvg 的库")
    except Exception as e:                                     # noqa: BLE001
        ok = False
        lines.append("  内联图标不可用     = %s: %s" % (type(e).__name__, e))
        lines.append("  ↑ 面板图标会全部空白；多半是打包漏收了 PySide6.QtSvg")

    lines.append("  结论             = %s" % ("通过" if ok else "**失败**"))
    return "\n".join(lines), ok

def _parse_probe_color(value) -> tuple[int, int, int] | None:
    """把 JS 探针返回的 "r,g,b" 解析成元组。解析失败返回 None。"""
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 3:
        return None
    try:
        rgb = tuple(int(p) for p in parts)
    except ValueError:
        return None
    if any(c < 0 or c > 255 for c in rgb):
        return None
    return rgb

def requested_service_port() -> int:
    """这次启动要用哪个端口去连/接管服务。

    **优先用更新重启时旧实例通过命令行传进来的实际端口。**
    理由：新实例靠 config 里的端口去探测并接管已有服务，但旧实例可能是
    "端口被占、改用系统分配"启动的，config 里存的不一定是实际端口——
    那样新实例会另起一个服务，把旧服务变成孤儿。由旧实例把实际端口
    直接告诉它，这条路才可靠。
    """
    return selfupdate.attach_port_from_argv() or int(config.get("port") or 0)
