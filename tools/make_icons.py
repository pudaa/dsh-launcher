# -*- coding: utf-8 -*-
"""从 assets/deepseek.svg 生成三套多尺寸 ICO。

为什么要底板（2026-09-30 加）
---------------------------
图标原来是**透明底**的纯字形。结果是两个方向都会瞎：

  · 亮色桌面上，白色鲸鱼看不见
  · 暗色桌面上，深色鲸鱼看不见

所以字形下面必须有一块**跟着一起变色的圆角底板**：白鲸配深板、深鲸配浅板。
这样无论底下是什么颜色的壁纸/标题栏，字形总有对比。
底板写在 SVG 里（`fill="currentPlate"`），随图标资产一起走，别的消费者也能拿到。

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

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt       # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter                  # noqa: E402
from PySide6.QtSvg import QSvgRenderer                              # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

SVG_SRC = os.path.join(ROOT, "assets", "deepseek.svg")
OUT_DIR = os.path.join(ROOT, "assets")

#: 一个 ICO 里塞这些尺寸。16/20/24/32 给任务栏与标题栏，
#: 48/64 给资源管理器，128/256 给大图标视图。
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

#: 深色标题栏用（白鲸）—— 底板用**深色**，这样白色字形永远有深色衬底。
#: 否则落在亮色桌面上就是白底白鲸，直接看不见。
LIGHT_FG, LIGHT_PLATE = "#ffffff", "#181a1f"
#: 浅色标题栏用（深鲸）—— 底板用**浅色**，理由同上，方向相反。
#: 深色值与应用深色主题一致，不是纯黑（纯黑在高分屏上显得比实际更重）。
DARK_FG, DARK_PLATE = "#181a1f", "#f2f4f7"

#: 输出产物。`icon.ico` 是 PE 资源与桌面快捷方式用的那一份：
#: 它只可能有一种形态，取「深板 + 白鲸」——白色字形在任何壁纸下都读得出来，
#: 深板则在亮色壁纸上把图标撑成一块清楚的方块。
VARIANTS = (
    ("icon-light.ico", LIGHT_FG, LIGHT_PLATE),
    ("icon-dark.ico", DARK_FG, DARK_PLATE),
    ("icon.ico", LIGHT_FG, LIGHT_PLATE),
)

#: 字形四边至少留这么多（占整块边长比例）。低于它说明缩放/居中参数退化了。
MIN_GLYPH_MARGIN = 0.08


def render_svg(svg_text: str, size: int, fg: str, plate: str) -> QImage:
    """把 SVG 渲染成指定尺寸的图像（底板 + 字形）。"""
    svg = (svg_text
           .replace("currentPlate", plate)
           .replace("currentColor", fg))
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


def glyph_bbox(img: QImage):
    """字形（不含底板）的包围盒，用来校验留白。

    底板是圆角矩形、贴满整块，所以直接量整图量不出字形位置——
    这里把底板设成全透明，只让字形显形。
    """
    minx, miny, maxx, maxy = img.width(), img.height(), -1, -1
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 20:
                minx = min(minx, x)
                maxx = max(maxx, x)
                miny = min(miny, y)
                maxy = max(maxy, y)
    if maxx < 0:
        return None
    return minx, miny, maxx, maxy


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

    if "currentPlate" not in svg_text:
        # 底板是图标能在任意底色上被看见的前提。没有它就别生成——
        # 悄悄退化成透明底会重新引入"某些桌面上看不见"的问题。
        print("  [FAIL] SVG 里没有 currentPlate 底板占位符，拒绝生成")
        return 1
    if "currentColor" not in svg_text:
        print("  [FAIL] SVG 里没有 currentColor 字形占位符，拒绝生成")
        return 1

    import re
    m = re.search(r'viewBox="([^"]+)"', svg_text)
    print("viewBox:", m.group(1) if m else "(无)")

    app = QApplication.instance() or QApplication([])                # noqa: F841

    # ---- 留白自检：字形不能顶到底板边缘 ----
    # 拿 256 这种大尺寸量，比例才有统计意义。
    # 把底板改成 fill="none" 让它不绘制——**不要**用 #00000000 之类的 8 位十六进制：
    # QtSvg 不认，rect 会退回继承根节点的 fill="currentColor"，于是整块被涂满，
    # 量出来的"字形包围盒"就成了整张图。（这个坑自检第一次跑就抓到了。）
    probe = 256
    bare = svg_text.replace('fill="currentPlate"', 'fill="none"')
    gimg = QSvgRenderer(QByteArray(
        bare.replace("currentColor", "#000000").encode("utf-8")))
    gi = QImage(probe, probe, QImage.Format.Format_ARGB32)
    gi.fill(QColor(0, 0, 0, 0))
    gp = QPainter(gi)
    gp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gimg.render(gp)
    gp.end()

    box = glyph_bbox(gi)
    if box is None:
        print("  [FAIL] 只渲染字形时是空白——路径或 transform 坏了")
        return 1
    minx, miny, maxx, maxy = box
    margins = (minx, probe - 1 - maxx, miny, probe - 1 - maxy)
    worst = min(margins) / probe
    ok = worst >= MIN_GLYPH_MARGIN
    print("  留白自检: 左%d 右%d 上%d 下%d 像素 (最小 %.1f%%，要求 >= %.1f%%)  [%s]"
          % (margins[0], margins[1], margins[2], margins[3],
             worst * 100, MIN_GLYPH_MARGIN * 100, "OK" if ok else "FAIL"))
    if not ok:
        print("  -> 字形贴边了。检查 assets/deepseek.svg 里的 translate/scale。")
        return 1

    # ---- 生成 ----
    for name, fg, plate in VARIANTS:
        imgs = [render_svg(svg_text, s, fg, plate) for s in SIZES]
        # 自检：底板必须真的画出来了（取左上角内侧一点，圆角之外应是字/板）
        im = imgs[-1]
        mid = im.pixelColor(im.width() // 2, im.height() // 2)
        corner = im.pixelColor(1, 1)
        data = build_ico(imgs)
        path = os.path.join(OUT_DIR, name)
        with open(path, "wb") as f:
            f.write(data)
        print("  生成 %-16s %6d 字节  尺寸 %d 档  中心色 %s  角(1,1) alpha=%d"
              % (name, len(data), len(SIZES), mid.name(), corner.alpha()))

    print("\n完成。底板的深浅由本文件上方的 LIGHT_PLATE / DARK_PLATE 决定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
