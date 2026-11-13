# -*- coding: utf-8 -*-
"""把面板截图加工成 README 用的"信息图"。

做法：截图外围加标题与副标题，下方配三条说明（对应图里的三块内容）。
**不在图内画序号**——卡片标题本身就靠左，任何编号都会压住文字；
用"图下的三条说明"代替，读者按从上到下的顺序自然对应，视觉也干净。

    python tools/compose_figure.py <in.png> <out.png> <标题> <副标题> <说明1> <说明2> <说明3>
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

_APP = None
FAM = "Microsoft YaHei UI"
PAD = 60
TOP = 150          # 标题 + 副标题 + 呼吸
BOTTOM_PAD = 40


def compose(src_path: str, out_path: str, title: str, subtitle: str,
            notes: list[str]) -> None:
    # QPainter 画文字要字体引擎，字体引擎要 QGuiApplication。
    # 缺了它不会抛异常，`save()` 直接静默失败——第一次跑就栽在这，白查十分钟。
    global _APP
    if QGuiApplication.instance() is None:
        _APP = QGuiApplication(sys.argv[:1])

    src = QImage(src_path)
    if src.isNull():
        raise SystemExit("读不到输入图：%s" % src_path)
    W, H = src.width(), src.height()
    legend_h = 54 * len(notes)
    OUT_W = W + PAD * 2
    OUT_H = TOP + H + legend_h + BOTTOM_PAD

    canvas = QImage(OUT_W, OUT_H, QImage.Format.Format_RGB32)
    canvas.fill(QColor("#ffffff"))
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    f_title = QFont(FAM); f_title.setPixelSize(48); f_title.setBold(True)
    f_sub = QFont(FAM); f_sub.setPixelSize(30)
    f_note = QFont(FAM); f_note.setPixelSize(30)

    p.setPen(QColor("#0f172a")); p.setFont(f_title)
    p.drawText(QRect(PAD, 34, OUT_W - PAD * 2, 62),
               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
    p.setPen(QColor("#64748b")); p.setFont(f_sub)
    p.drawText(QRect(PAD, 96, OUT_W - PAD * 2, 44),
               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle)

    # 截图右下角会带上 Qt 的尺寸调整手柄（那个小三角），放进 README 很出戏。
    # 面板本身也允许拖拽缩放，但图里不需要展示这个能力。
    src = src.copy()
    grip = QPainter(src)
    grip.fillRect(QRect(W - 26, H - 26, 26, 26), QColor("#f7f8fa"))
    grip.end()

    p.drawImage(PAD, TOP, src)
    # 截图本身是白底，给它一圈细边才在白画布上有边界
    p.setPen(QPen(QColor("#e2e8f0"), 2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(PAD + 1, TOP + 1, W - 2, H - 2), 24, 24)

    y = TOP + H + 26
    for note in notes:
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#2563eb"))
        p.drawEllipse(QPointF(PAD + 9, y + 18), 7, 7)
        p.setPen(QColor("#334155")); p.setFont(f_note)
        p.drawText(QRectF(PAD + 30, y, OUT_W - PAD * 2 - 30, 38),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, note)
        y += 54

    p.end()
    canvas.save(out_path, "PNG")
    print("已生成 %s  %dx%d" % (out_path, canvas.width(), canvas.height()))


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 6:
        raise SystemExit("用法：compose_figure.py <in> <out> <标题> <副标题> <说明1> <说明2> <说明3>")
    compose(a[0], a[1], a[2], a[3], a[4:])
