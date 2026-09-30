# -*- coding: utf-8 -*-
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

#: 名字 -> 一行 SVG 源码。合计 5881 字节。
SVG: dict[str, str] = {
    "layout-dashboard":
        "<svg class=\"lucide lucide-layout-dashboard\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><rect width=\"7\" height=\"9\" x=\"3\" y=\"3\" rx=\"1\" /><rect width=\"7\" height=\"5\" x=\"14\" y=\"3\" rx=\"1\" /><rect width=\"7\" height=\"9\" x=\"14\" y=\"12\" rx=\"1\" /><rect width=\"7\" height=\"5\" x=\"3\" y=\"16\" rx=\"1\" /></svg>",
    "package":
        "<svg class=\"lucide lucide-package\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z\" /><path d=\"M12 22V12\" /><polyline points=\"3.29 7 12 12 20.71 7\" /><path d=\"m7.5 4.27 9 5.15\" /></svg>",
    "plug":
        "<svg class=\"lucide lucide-plug\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"M12 22v-5\" /><path d=\"M15 8V2\" /><path d=\"M17 8a1 1 0 0 1 1 1v4a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1z\" /><path d=\"M9 8V2\" /></svg>",
    "activity":
        "<svg class=\"lucide lucide-activity\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2\" /></svg>",
    "info":
        "<svg class=\"lucide lucide-info\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><circle cx=\"12\" cy=\"12\" r=\"10\" /><path d=\"M12 16v-4\" /><path d=\"M12 8h.01\" /></svg>",
    "x":
        "<svg class=\"lucide lucide-x\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"M18 6 6 18\" /><path d=\"m6 6 12 12\" /></svg>",
    "minus":
        "<svg class=\"lucide lucide-minus\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"M5 12h14\" /></svg>",
    "circle-check":
        "<svg class=\"lucide lucide-circle-check\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><circle cx=\"12\" cy=\"12\" r=\"10\" /><path d=\"m16 9-5.5 5.5L8 12\" /></svg>",
    "circle-x":
        "<svg class=\"lucide lucide-circle-x\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><circle cx=\"12\" cy=\"12\" r=\"10\" /><path d=\"m15 9-6 6\" /><path d=\"m9 9 6 6\" /></svg>",
    "triangle-alert":
        "<svg class=\"lucide lucide-triangle-alert\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3\" /><path d=\"M12 9v4\" /><path d=\"M12 17h.01\" /></svg>",
    "loader-circle":
        "<svg class=\"lucide lucide-loader-circle\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"M21 12a9 9 0 1 1-6.219-8.56\" /></svg>",
    "circle-dashed":
        "<svg class=\"lucide lucide-circle-dashed\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"M10.1 2.182a10 10 0 0 1 3.8 0\" /><path d=\"M13.9 21.818a10 10 0 0 1-3.8 0\" /><path d=\"M17.609 3.721a10 10 0 0 1 2.69 2.7\" /><path d=\"M2.182 13.9a10 10 0 0 1 0-3.8\" /><path d=\"M20.279 17.609a10 10 0 0 1-2.7 2.69\" /><path d=\"M21.818 10.1a10 10 0 0 1 0 3.8\" /><path d=\"M3.721 6.391a10 10 0 0 1 2.7-2.69\" /><path d=\"M6.391 20.279a10 10 0 0 1-2.69-2.7\" /></svg>",
    "refresh-cw":
        "<svg class=\"lucide lucide-refresh-cw\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8\" /><path d=\"M21 3v5h-5\" /><path d=\"M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16\" /><path d=\"M8 16H3v5\" /></svg>",
    "external-link":
        "<svg class=\"lucide lucide-external-link\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"M15 3h6v6\" /><path d=\"M10 14 21 3\" /><path d=\"M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6\" /></svg>",
    "copy":
        "<svg class=\"lucide lucide-copy\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><rect width=\"14\" height=\"14\" x=\"8\" y=\"8\" rx=\"2\" ry=\"2\" /><path d=\"M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2\" /></svg>",
    "chevron-right":
        "<svg class=\"lucide lucide-chevron-right\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"m9 18 6-6-6-6\" /></svg>",
    "folder-open":
        "<svg class=\"lucide lucide-folder-open\" xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" ><path d=\"m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2\" /></svg>",
}


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
