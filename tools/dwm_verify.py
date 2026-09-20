# -*- coding: utf-8 -*-
"""DWM 标题栏上色 —— 实机验证。

结论已定（2026-09-20，均有硬证据）
----------------------------------
1. `DWMWA_CAPTION_COLOR(35)` / `TEXT_COLOR(36)` / `BORDER_COLOR(34)` 是
   **只写**属性。对它们调 `DwmGetWindowAttribute` 恒返回
   `E_INVALIDARG (0x80070057)`，**不代表设置失败**。曾经据此误判
   "DWM 不可用"，实际功能一直正常。
2. Qt 的 `QScreen.grabWindow(winId)` 在本机返回整片 `#f3f3f3`
   （拿到陈旧/空白合成表面），**不能用于验证**。必须走 Win32 GDI
   `PrintWindow(PW_RENDERFULLCONTENT)`。
3. 正确验收 = GDI 截屏后逐行采样，看设定色是否出现。

用法：
    python tools/dwm_verify.py     # 会闪现一个窗口，属正常
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

from dsh_host import dwm                                             # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
PW_RENDERFULLCONTENT = 2

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -> " + detail) if detail else ""))


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def gdi_capture(hwnd: int) -> tuple[bytes, int, int]:
    """截取窗口位图（含 DWM 扩展边框）。返回 (BGRA, w, h)。"""
    rect = _RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return b"", 0, 0
    hdc = user32.GetWindowDC(wintypes.HWND(hwnd))
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    user32.PrintWindow(wintypes.HWND(hwnd), memdc, PW_RENDERFULLCONTENT)
    bi = _BMIH()
    bi.biSize = ctypes.sizeof(_BMIH)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(wintypes.HWND(hwnd), hdc)
    return buf.raw, w, h


def hit_rows(data: bytes, w: int, h: int, target: tuple[int, int, int],
             tol: int = 10, scan: int = 60) -> list[int]:
    """返回中心列上命中目标色的行号。"""
    out = []
    x = w // 2
    for y in range(min(scan, h)):
        i = (y * w + x) * 4
        if i + 2 >= len(data):
            break
        rgb = (data[i + 2], data[i + 1], data[i])
        if dwm.color_near(rgb, target, tol):
            out.append(y)
    return out


TARGET = (36, 42, 56)          # #242a38
TEXT_C = dwm.contrast_text(TARGET)

print("=" * 64)
print("DWM 标题栏上色实机验证")
print("build=%s  available=%s" % (sys.getwindowsversion().build, dwm.available()))
print("=" * 64)

app = QApplication.instance() or QApplication(sys.argv)
win = QMainWindow()
win.setWindowTitle("DWM verify")
win.resize(520, 300)
win.show()


def step_baseline():
    print("\n[1] 基线：上色前 GDI 截取")
    data, w, h = gdi_capture(int(win.winId()))
    check("GDI 截图可用", w > 0 and h > 0, "%dx%d" % (w, h))
    before = hit_rows(data, w, h, TARGET)
    print("    命中目标色的行:", before or "无")
    check("上色前不应命中目标色", not before, str(before))
    QTimer.singleShot(300, step_apply)


def step_apply():
    print("\n[2] 上色：caption=%s text=%s dark=%s"
          % (dwm.to_hex(TARGET), dwm.to_hex(TEXT_C), dwm.is_dark(TARGET)))
    check("set_dark_mode", dwm.set_dark_mode(win, dwm.is_dark(TARGET)))
    check("set_caption_color", dwm.set_caption_color(win, TARGET))
    check("set_border_color", dwm.set_border_color(win, TARGET))
    check("set_text_color", dwm.set_text_color(win, TEXT_C))
    QTimer.singleShot(900, step_verify)


def step_verify():
    print("\n[3] 验证：GDI 截取比对（DWM 有淡入，等 900ms）")
    data, w, h = gdi_capture(int(win.winId()))
    after = hit_rows(data, w, h, TARGET)
    print("    命中目标色的行:", after[:5], "...共 %d 行" % len(after)
          if after else "    无")
    check("标题栏渲染为设定色（像素级）", len(after) >= 8,
          "命中 %d 行" % len(after))
    check("上色前后确有变化", bool(after), "before=0 after=%d" % len(after))
    QTimer.singleShot(200, step_algo)


def step_algo():
    print("\n[4] 自适应算法链路（纯逻辑）")
    check("is_dark(中灰) == False", dwm.is_dark((128, 128, 128)) is False)
    check("is_dark(深蓝) == True", dwm.is_dark((36, 42, 56)) is True)
    ref = 36 | (42 << 8) | (56 << 16)
    check("COLORREF 字节序 0x00BBGGRR", ref == 0x00382A24, hex(ref))
    check("contrast_text(深底) 给浅字",
          dwm.contrast_text((20, 20, 20)) == (240, 242, 245))
    check("contrast_text(浅底) 给深字",
          dwm.contrast_text((240, 240, 240)) == (24, 26, 31))
    b = dwm.blend((0, 0, 0), (255, 255, 255), 0.5)
    check("blend 中点", b in ((128, 128, 128), (127, 127, 127)), str(b))

    print("\n[5] 主色调统计（关键回归）")

    def build(spec, w, h):
        buf = bytearray()
        for _y in range(h):
            for x in range(w):
                buf += bytes(spec(x) + (0xFF,))
        return bytes(buf), w, h

    # 5a 大面积深灰 + 小面积青绿主题色 → 必须认出青绿
    buf, w, h = build(lambda x: (0x22, 0xC1, 0xA3) if 20 <= x < 32
                      else (0x1E, 0x1E, 0x1E), 64, 8)
    dom = dwm.dominant_color(buf, w, h)
    print("    5a 灰底+青绿条 ->", dom)
    check("5a 主色调识别为青绿（不被灰底淹没）",
          dom is not None and dom[1] > dom[0] and dom[1] > dom[2], str(dom))

    # 5b 纯灰界面 → 应退回灰色，而不是返回 None
    buf, w, h = build(lambda x: (0x30, 0x30, 0x30), 64, 8)
    dom = dwm.dominant_color(buf, w, h)
    print("    5b 纯灰界面 ->", dom)
    check("5b 纯灰界面退回灰色方案", dom is not None
          and abs(dom[0] - dom[1]) < 8 and abs(dom[1] - dom[2]) < 8, str(dom))

    # 5c 空输入
    check("5c 空输入返回 None", dwm.dominant_color(b"", 0, 0) is None)

    print("\n" + "=" * 64)
    print("结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 64)
    app.quit()


QTimer.singleShot(500, step_baseline)
app.exec()
sys.exit(1 if FAIL else 0)

