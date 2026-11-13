# -*- coding: utf-8 -*-
"""把 MOSS 生成的横幅与真实截图拼成一张 README 用的成品图。

分工：
  · 横幅（深色 + 中文标题）由 MOSS 生成——美术风格和字形比手工排版好看得多；
  · 截图必须是真实渲染，不能用生成图替代（图里的界面细节就是证据）；
  · 说明文字用 Qt 画——生成模型写长句会出错，而这一层需要绝对准确。

    python tools/compose_hero.py <banner.png> <shot.png> <out.png> <副标题> <说明1> <说明2> <说明3>
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

_APP = None
FAM = "Microsoft YaHei UI"

W = 2200                      # 整图宽：与截图原始宽度一致，细节不损失
SIDE = 100                    # 截图两侧留白
GAP = 56                      # 横幅与截图的间距
NOTE_GAP = 52


def compose(banner_path: str, shot_path: str, out_path: str, subtitle: str,
            notes: list[str]) -> None:
    global _APP
    if QGuiApplication.instance() is None:
        _APP = QGuiApplication(sys.argv[:1])

    banner = QImage(banner_path)
    shot = QImage(shot_path)
    if banner.isNull() or shot.isNull():
        raise SystemExit("读不到输入图")

    # 横幅按整图宽度等比放大。放大后字形会略软，但它是背景层；
    # 真正的细节在下面那张截图上，而截图几乎是 1:1（2200 → 2000）。
    bh = int(banner.height() * W / banner.width())
    banner = banner.scaledToWidth(W, Qt.TransformationMode.SmoothTransformation)

    shot_w = W - SIDE * 2
    shot_h = int(shot.height() * shot_w / shot.width())
    shot = shot.scaledToWidth(shot_w, Qt.TransformationMode.SmoothTransformation)

    note_h = NOTE_GAP * len(notes) + 56
    total_h = bh + GAP + shot_h + note_h

    canvas = QImage(W, total_h, QImage.Format.Format_RGB32)
    canvas.fill(QColor("#ffffff"))
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    p.drawImage(0, 0, banner)

    # 横幅（深色）到内容区（白色）之间直切会很硬，拉一条短渐变过渡。
    fade = QLinearGradient(0, bh - 90, 0, bh)
    fade.setColorAt(0.0, QColor(255, 255, 255, 0))
    fade.setColorAt(1.0, QColor(255, 255, 255, 255))
    p.fillRect(QRect(0, bh - 90, W, 90), fade)

    y = bh + GAP - 40
    # 截图带一圈极淡的边框与细阴影感，白底上才有"窗口"的立体感
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(15, 23, 42, 14))
    p.drawRoundedRect(QRectF(SIDE - 2, y - 2, shot_w + 4, shot_h + 6), 26, 26)
    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(QRectF(SIDE, y, shot_w, shot_h), 24, 24)

    path = QPainterPath()
    path.addRoundedRect(QRectF(SIDE, y, shot_w, shot_h), 24, 24)
    p.save()
    p.setClipPath(path)
    p.drawImage(SIDE, y, shot)
    p.restore()
    p.setPen(QPen(QColor("#e2e8f0"), 2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(SIDE, y, shot_w, shot_h), 24, 24)

    y += shot_h + 36
    f_sub = QFont(FAM); f_sub.setPixelSize(34)
    f_note = QFont(FAM); f_note.setPixelSize(31)

    p.setPen(QColor("#475569")); p.setFont(f_sub)
    p.drawText(QRect(SIDE, y, W - SIDE * 2, 48),
               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle)
    y += 62

    for note in notes:
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#2563eb"))
        p.drawEllipse(QPointF(SIDE + 8, y + 18), 7, 7)
        p.setPen(QColor("#334155")); p.setFont(f_note)
        p.drawText(QRectF(SIDE + 30, y, W - SIDE * 2 - 30, 38),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, note)
        y += NOTE_GAP

    p.end()
    canvas.save(out_path, "PNG")
    print("已生成 %s  %dx%d  %.0f KB" % (out_path, canvas.width(), canvas.height(),
                                       os.path.getsize(out_path) / 1024))


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 7:
        raise SystemExit("用法：compose_hero.py <banner> <shot> <out> <副标题> <说明1> <说明2> <说明3>")
    compose(a[0], a[1], a[2], a[3], a[4:])
