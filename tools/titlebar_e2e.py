# -*- coding: utf-8 -*-
"""端到端：用真实 GUI 窗口验证 `_apply_titlebar` 的完整调用链。

前面的 dwm_verify.py 验证的是 dwm 模块的**基本能力**；
本脚本验证的是**接线**：从 config 读 mute → blend → contrast_text
→ is_dark → 四个 DWM 调用 → GDI 截图确认像素落地。

关键在于它用的是真实的主窗口类，而不是裸 QMainWindow。
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTimer                                    # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow              # noqa: E402

from dsh_host import config, dwm                                     # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PASS: list[str] = []
FAIL: list[str] = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -> " + detail) if detail else ""))


class _RECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                ("r", ctypes.c_long), ("b", ctypes.c_long)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def gdi_capture(hwnd):
    rect = _RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    w, h = rect.r - rect.l, rect.b - rect.t
    hdc = user32.GetWindowDC(wintypes.HWND(hwnd))
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    user32.PrintWindow(wintypes.HWND(hwnd), memdc, 2)
    bi = _BMIH()
    bi.biSize = ctypes.sizeof(_BMIH)
    bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, -h, 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(wintypes.HWND(hwnd), hdc)
    return buf.raw, w, h


def rows_near(data, w, h, target, tol=10, scan=60):
    out, x = [], w // 2
    for y in range(min(scan, h)):
        i = (y * w + x) * 4
        if i + 2 < len(data) and dwm.color_near(
                (data[i + 2], data[i + 1], data[i]), target, tol):
            out.append(y)
    return out


# 用真实的窗口类，只 stub 掉不需要的初始化
import dsh_gui_qt as g                                            # noqa: E402

print("=" * 64)
print("端到端：_apply_titlebar 接线验证")
print("=" * 64)
print("config: adaptive_titlebar=%s titlebar_mute=%s"
      % (config.get("adaptive_titlebar"), config.get("titlebar_mute")))

app = QApplication.instance() or QApplication(sys.argv)


class Probe(QMainWindow):
    """借用真实实现的方法，避免构造完整窗口（会连服务）。"""
    _apply_titlebar = g.MainWindow._apply_titlebar
    _sample_titlebar_color = g.MainWindow._sample_titlebar_color


win = Probe()
win.setWindowTitle("E2E titlebar")
win.resize(500, 260)
win.show()

DOM = (0x22, 0xC1, 0xA3)          # 「青云学」主题色，模拟界面主色


def step():
    mute = float(config.get("titlebar_mute") or 0.0)
    expect = dwm.blend(DOM, (18, 20, 24), mute)
    print("\n[1] 模拟界面主色 %s，mute=%.2f" % (dwm.to_hex(DOM), mute))
    print("    预期标题栏底色 = %s（往深灰拉 %d%%）"
          % (dwm.to_hex(expect), mute * 100))

    d0, w0, h0 = gdi_capture(int(win.winId()))
    before = rows_near(d0, w0, h0, expect)
    check("上色前不应命中预期色", not before, str(before))

    print("\n[2] 调用真实 _apply_titlebar")
    win._apply_titlebar(DOM)

    def verify():
        d1, w1, h1 = gdi_capture(int(win.winId()))
        after = rows_near(d1, w1, h1, expect)
        print("    命中预期色的行数 =", len(after))
        check("标题栏落地为 blend 后的颜色", len(after) >= 8,
              "命中 %d 行" % len(after))
        # 文字色必须与底色对比
        fg = dwm.contrast_text(expect)
        check("文字色与底色对比 >= 100",
              abs(dwm.luminance(fg) - dwm.luminance(expect)) >= 100,
              "底色 %s 文字 %s" % (dwm.to_hex(expect), dwm.to_hex(fg)))

        print("\n[3] 关闭开关后不应再上色")
        config.set(adaptive_titlebar=False)
        check("config 开关已关闭", config.get("adaptive_titlebar") is False)
        check("_sample_titlebar_color 提前返回（不报错）",
              win._sample_titlebar_color() is None)

        print("\n" + "=" * 64)
        print("结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
        for f in FAIL:
            print("  FAILED:", f)
        print("=" * 64)
        app.quit()

    QTimer.singleShot(900, verify)


QTimer.singleShot(500, step)
app.exec()
sys.exit(1 if FAIL else 0)
