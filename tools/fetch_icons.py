# -*- coding: utf-8 -*-
"""从 Lucide 抓取需要的图标，生成内联 SVG 模块 dsh_icons.py。

为什么要内联而不是运行时读文件
------------------------------
打包成单文件（Nuitka onefile）时，运行时按路径读资源**不会自动带上**资源目录。
本项目为图标已经踩过一次：`--windows-icon-from-ico` 只把图标写进 exe 的 PE 资源，
而 `QIcon("assets/xxx.ico")` 是去文件系统读的，结果打包后图标静默失效（不报错）。
内联成源码常量彻底绕开这类问题，还顺带能按状态色动态改色。

图标来源
--------
Lucide —— https://lucide.dev ，**ISC 许可**，可商用、可修改，需保留许可声明。
取单个 SVG 走 jsdelivr（unpkg 会 302，要额外 -L）。

用法：
    python tools/fetch_icons.py            # 重新生成 dsh_icons.py
    python tools/fetch_icons.py --check    # 只校验已生成的模块是否齐全
"""
from __future__ import annotations

import io
import os
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dsh_icons.py")

CDN = "https://cdn.jsdelivr.net/npm/lucide-static@latest/icons/{}.svg"

#: 名字 -> 用途说明。改这里就能增删图标。
ICONS: dict[str, str] = {
    # 左侧导航
    "layout-dashboard": "概览",
    "package": "本机部署",
    "plug": "连接",
    "activity": "状态检测",
    "info": "关于",
    # 窗口按钮
    "x": "关闭",
    "minus": "最小化",
    # 状态
    "circle-check": "正常",
    "circle-x": "异常",
    "triangle-alert": "警告",
    "loader-circle": "检测中",
    "circle-dashed": "未检测",
    # 动作
    "refresh-cw": "重新检测",
    "external-link": "在浏览器打开",
    "copy": "复制",
    "chevron-right": "进入 / 展开",
    "folder-open": "打开目录",
}

#: 模块里除图标外的公共部分。用普通字符串，避免 f-string 里花括号转义地狱。
TEMPLATE_HEAD = '''# -*- coding: utf-8 -*-
"""内联 SVG 图标集（自动生成，勿手改）。

由 `tools/fetch_icons.py` 从 **Lucide**（https://lucide.dev ，ISC 许可）抓取。
Lucide 的图形可商用、可修改，需保留本声明。

为什么内联成源码
----------------
打包成单文件后，运行时按路径读资源不会自动带上资源目录——本项目为此踩过坑
（图标在源码里正常、打包后静默失效）。内联彻底绕开，还顺带能按状态色改色。

改色的关键
----------
Lucide 用 `stroke="currentColor"`，而 **QtSvg 没有 CSS 上下文、不解析 currentColor**，
所以渲染前必须把 currentColor 替换成实色。见 `recolor()`。
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

'''

TEMPLATE_TAIL = '''

def recolor(svg: str, color: str) -> bytes:
    """把 currentColor 换成实色。

    必须替换：QtSvg 不解析 currentColor，不换就一律渲染成黑色。
    """
    return (svg.replace('stroke="currentColor"', 'stroke="%s"' % color)
               .replace('fill="currentColor"', 'fill="%s"' % color)).encode("utf-8")


def render(name: str, size: int, color: str, dpr: float = 1.0) -> QImage:
    """按名字渲染成 ARGB32 图像（透明底）。dpr 用于 HiDPI。"""
    px = max(1, int(round(size * dpr)))
    img = QImage(px, px, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    svg = SVG.get(name)
    if not svg:
        return img
    r = QSvgRenderer(QByteArray(recolor(svg, color)))
    if not r.isValid():
        return img
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    r.render(p)
    p.end()
    return img


def pixmap(name: str, size: int, color: str, dpr: float = 1.0) -> QPixmap:
    """按名字取图标并上色。

    **名字不存在时返回真正的空 QPixmap（isNull() 为真）**，而不是"尺寸对但全透明"的
    图——后者会让名字打错变成一个看不见的图标，属于静默失败。
    """
    if name not in SVG:
        return QPixmap()
    pm = QPixmap.fromImage(render(name, size, color, dpr))
    pm.setDevicePixelRatio(dpr)
    return pm


def qicon(name: str, color: str, size: int = 20) -> QIcon:
    """做 QIcon（按钮、菜单用）。

    一次塞进 1x / 1.5x / 2x 三档位图——高分屏上 QIcon 会自己挑最合适的那张，
    否则 100% 缩放的位图在 150% 缩放下会发虚。
    名字不存在时返回空 QIcon（isNull() 为真），别让它变成隐形图标。
    """
    if name not in SVG:
        return QIcon()
    ic = QIcon()
    for dpr in (1.0, 1.5, 2.0):
        ic.addPixmap(pixmap(name, size, color, dpr))
    return ic


def ink_ratio(name: str, size: int = 64, color: str = "#000000") -> float:
    """已上色像素占比——用于自测。

    判据是两头而不是中间：
      · 接近 0    -> 没渲染出来（多半是 currentColor 没替换，或 SVG 坏了）
      · 接近 1    -> 退化成实心块
    正常区间很宽（约 2%~60%），因为图标本身的粗细差异很大：
    `minus` 就是一条横线（约 6%），`layout-dashboard` 是方格阵（约 41%）。
    **别用"低于 10% 就算异常"这种阈值**——会把正常的细线图标全判死。
    """
    img = render(name, size, color)
    if img.width() == 0:
        return 0.0
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 20:
                n += 1
    return n / (img.width() * img.height())


def missing(names) -> list:
    """返回名字列表里缺失的那些，供自测断言用。"""
    return [n for n in names if n not in SVG]


#: 界面用色。集中在这里，避免各页各写一套。
COLOR_OK = "#2ea043"        # 正常
COLOR_BAD = "#d64545"       # 异常
COLOR_WARN = "#c98a00"      # 警告
COLOR_MUTED = "#8b93a1"     # 次要文字 / 未检测
COLOR_TEXT = "#e8eaee"      # 主文字（深色面板）
COLOR_TEXT_DIM = "#9aa3b2"  # 次级文字
COLOR_ACCENT = "#4d6bfe"    # 强调 / 选中
COLOR_PANEL = "#1b1f27"     # 面板底
COLOR_SIDEBAR = "#161a20"   # 侧栏底
COLOR_LINE = "#2a3038"      # 分隔线
'''


def fetch(name: str) -> str:
    url = CDN.format(name)
    with urllib.request.urlopen(url, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    if "<svg" not in text:
        raise RuntimeError("拿到的不是 SVG: " + url)
    return text


def minify(svg: str) -> str:
    """压成一行：去掉许可注释与换行。保留 currentColor（改色的抓手）。"""
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
    svg = re.sub(r">\s+<", "><", svg)
    svg = re.sub(r"\s+", " ", svg).strip()
    return svg


def main() -> int:
    if "--check" in sys.argv:
        if not os.path.isfile(OUT):
            print("[FAIL] 还没有生成 dsh_icons.py")
            return 1
        src = io.open(OUT, encoding="utf-8").read()
        missing = [n for n in ICONS if ('"%s":' % n) not in src]
        print("已生成图标 %d 个；缺失 %s" % (len(ICONS), "、".join(missing) or "无"))
        return 1 if missing else 0

    entries: list[tuple[str, str]] = []
    failed: list[str] = []
    for name in ICONS:
        try:
            body = minify(fetch(name))
            entries.append((name, body))
            print("  取到 %-18s %4d 字节" % (name, len(body)))
        except Exception as e:                                   # noqa: BLE001
            failed.append(name)
            print("  [FAIL] %-18s %s" % (name, e))

    if failed:
        print("\n有图标没取到：%s" % "、".join(failed))
        print("不写出文件——残缺的图标集会让界面缺图，比直接失败更难发现。")
        return 1

    lines = []
    for name, body in entries:
        lines.append('    "%s":' % name)
        lines.append('        "%s",' % body.replace("\\", "\\\\").replace('"', '\\"'))
    total = sum(len(b) for _n, b in entries)
    src = (TEMPLATE_HEAD
           + "#: 名字 -> 一行 SVG 源码。合计 %d 字节。\nSVG: dict[str, str] = {\n" % total
           + "\n".join(lines)
           + "\n}\n"
           + TEMPLATE_TAIL)

    io.open(OUT, "w", encoding="utf-8", newline="\n").write(src)
    print("\n已写出 %s（%d 个图标，%d 字节）" % (OUT, len(entries), len(src)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
