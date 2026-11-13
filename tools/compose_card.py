# -*- coding: utf-8 -*-
"""把真实截图嵌进 MOSS 生成的背景里，产出 Readme 用的功能图。

分工说明
--------
  · **背景**（深色、标题、装饰线条）由 MOSS 生成——配色取自 DSH 官方令牌
    （深色底 #151517、强调色 #679efe），手工排版做不出那种质感；
  · **截图**必须是真实渲染，不能生成——图里的界面细节就是证据；
  · **说明文字**放 README 正文，不塞进图里：图内空间有限，中文长句挤在
    底部会很难看，而正文里的文字还能被搜索、被翻译。

    python tools/compose_card.py <bg.png> <shot.png> <out.png>
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

_APP = None


def compose(bg_path: str, shot_path: str, out_path: str,
            top_ratio: float = 0.185, side_ratio: float = 0.075,
            bottom_ratio: float = 0.035) -> None:
    global _APP
    if QGuiApplication.instance() is None:
        _APP = QGuiApplication(sys.argv[:1])

    bg = QImage(bg_path)
    shot = QImage(shot_path)
    if bg.isNull() or shot.isNull():
        raise SystemExit("读不到输入图")

    BH, BW = bg.height(), bg.width()
    top = int(BH * top_ratio)            # 标题区由 MOSS 画好，不侵占
    side = int(BW * side_ratio)
    bottom = int(BH * bottom_ratio)
    box_w = BW - side * 2
    box_h = BH - top - bottom

    # 截图按框内等比缩放，居中
    scale = min(box_w / shot.width(), box_h / shot.height())
    sw, sh = int(shot.width() * scale), int(shot.height() * scale)
    shot = shot.scaled(sw, sh, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation).convertToFormat(
        QImage.Format.Format_ARGB32_Premultiplied)
    x = (BW - sw) // 2
    y = top + (box_h - sh) // 2

    p = QPainter(bg)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    # 一圈柔和的外发光，让浅色窗口从深色背景里"浮"起来
    # 发光弱一点：太亮会像霓虹灯，只是让浅色窗口和深色背景有过渡就够了
    for i in range(14, 0, -1):
        alpha = int(4 + (14 - i) * 1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(103, 158, 254, alpha))
        p.drawRoundedRect(QRectF(x - i, y - i, sw + i * 2, sh + i * 2), 14 + i, 14 + i)

    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, sw, sh), 14, 14)
    p.save()
    p.setClipPath(path)
    p.drawImage(x, y, shot)
    p.restore()
    p.setPen(QPen(QColor(255, 255, 255, 38), 1))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(x, y, sw, sh), 14, 14)

    p.end()
    bg.save(out_path, "PNG")
    print("已生成 %s  %dx%d" % (out_path, BW, BH))


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 3:
        raise SystemExit("用法：compose_card.py <bg.png> <shot.png> <out.png>")
    compose(a[0], a[1], a[2])
