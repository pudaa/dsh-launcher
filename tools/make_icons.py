# -*- coding: utf-8 -*-
"""从 assets/deepseek.svg 生成深浅两套多尺寸 ICO。

为什么要两套
------------
DSH 的暗色主题下标题栏是深色，原来的黑色图标**看不见**。
图标必须跟着主题走：深色标题栏配白色图标，浅色标题栏配深色图标。

为什么手写 ICO 而不是 QImage.save("ICO")
----------------------------------------
Qt 的 ICO 写入只产出**单一尺寸**（256x256 一个条目，实测 270KB）。
而 ICO 的价值就在于一个文件里塞多个尺寸，让 16x16 任务栏和
256x256 大图标都清晰。所以自己拼 ICO：

  ICONDIR      : reserved(2)=0, type(2)=1, count(2)=N
  ICONDIRENTRY : w(1), h(1), colors(1)=0, reserved(1)=0,
                 planes(2)=1, bitCount(2)=32, size(4), offset(4)
                 —— 尺寸 256 要写 0
  数据          : 各尺寸的 PNG 字节，依次排列

现代 Windows 支持 ICO 里直接内嵌 PNG，体积小且保真。

用法：
    python tools/make_icons.py
"""
from __future__ import annotations

import os
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QByteArray, QBuffer, QIODevice           # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter                  # noqa: E402
from PySide6.QtSvg import QSvgRenderer                              # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

SVG_SRC = os.path.join(ROOT, "assets", "deepseek.svg")
OUT_DIR = os.path.join(ROOT, "assets")

#: 一个 ICO 里塞这些尺寸。16/20/24/32 给任务栏与标题栏，
#: 48/64 给资源管理器，128/256 给大图标视图。
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

#: 深色标题栏用的白色图标（纯白）
LIGHT_ICON_FILL = "#ffffff"
#: 浅色标题栏用的深色图标（与 contrast_text 的深色一致，不是纯黑，
#: 纯黑在高分屏上会显得比实际更重）
DARK_ICON_FILL = "#181a1f"


def render_svg(svg_text: str, size: int, fill: str) -> QImage:
    """把 SVG 渲染成指定尺寸的透明底图像。"""
    svg = svg_text.replace("currentColor", fill)
    r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not r.isValid():
        raise RuntimeError("SVG 无效")
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    r.render(p)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def build_ico(images: list[QImage]) -> bytes:
    """把多张图像拼成一个 ICO（每张以 PNG 内嵌）。"""
    payloads = [png_bytes(im) for im in images]
    n = len(payloads)
    header = struct.pack("<HHH", 0, 1, n)
    offset = len(header) + 16 * n
    entries = b""
    for im, data in zip(images, payloads):
        w = 0 if im.width() >= 256 else im.width()
        h = 0 if im.height() >= 256 else im.height()
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32,
                               len(data), offset)
        offset += len(data)
    return header + entries + b"".join(payloads)


def main() -> int:
    if not os.path.isfile(SVG_SRC):
        print("找不到源文件:", SVG_SRC)
        return 1
    svg_text = open(SVG_SRC, encoding="utf-8").read()
    print("源文件:", SVG_SRC)
    print("viewBox:", end=" ")
    import re
    m = re.search(r'viewBox="([^"]+)"', svg_text)
    print(m.group(1) if m else "(无)")

    app = QApplication.instance() or QApplication([])                # noqa: F841

    for fill, name in ((LIGHT_ICON_FILL, "icon-light.ico"),
                       (DARK_ICON_FILL, "icon-dark.ico")):
        imgs = [render_svg(svg_text, s, fill) for s in SIZES]
        # 自检：渲染出来不能是全透明
        opaque = sum(1 for im in imgs
                     if any(im.pixelColor(x, im.height() // 2).alpha() > 0
                            for x in range(0, im.width(), max(1, im.width() // 8))))
        data = build_ico(imgs)
        path = os.path.join(OUT_DIR, name)
        with open(path, "wb") as f:
            f.write(data)
        print("  生成 %-16s %6d 字节  尺寸 %s  有效尺寸数 %d/%d"
              % (name, len(data), ",".join(str(s) for s in SIZES),
                 opaque, len(SIZES)))

    print("\n完成。应用会按标题栏深浅自动切换这两套图标。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
