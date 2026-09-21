# -*- coding: utf-8 -*-
"""验证三层取色链路（不需要 WebEngine 也能测 L2/L3 的像素逻辑）。

为什么单独测
------------
沙箱里 Chromium 渲染进程会被杀（renderProcessTerminated /
KilledTerminationStatus），所以**没法在沙箱里测真实网页取色**。
但 L2（GDI PrintWindow）与 L3（控件截图）的像素逻辑可以独立验证：
造一个顶部有明确颜色的普通窗口，看取色是否命中。

本脚本验证：
  1. 顶部一条是纯色工具栏时，**不该**被判成"空白"（不能误杀）
  2. 整幅纯色（模拟未渲染）时，**必须**判为空白并返回 None
  3. 顶部有颜色时，取到的就是这个颜色
  4. 客户区偏移算对了（不会取到系统标题栏的颜色）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SANDBOX = os.path.join(tempfile.gettempdir(), "dsh-launcher-test-cfg")
os.makedirs(_SANDBOX, exist_ok=True)
os.environ["LOCALAPPDATA"] = _SANDBOX

from PySide6.QtCore import QTimer                                   # noqa: E402
from PySide6.QtGui import QColor, QPainter                          # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget                  # noqa: E402

from dsh_host import dwm                                            # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -> " + detail) if detail else ""))


TOP = (0x22, 0xC1, 0xA3)      # 顶部工具栏：青绿
BODY = (0x1E, 0x20, 0x24)     # 主体：深灰


class Probe(QWidget):
    """顶部 8% 画成 TOP 色，其余 BODY 色，中间加些纹理避免整体过平。"""

    def __init__(self, uniform=False):
        super().__init__()
        self.uniform = uniform
        self.setWindowTitle("取色探针")
        self.resize(900, 600)

    def paintEvent(self, _ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        if self.uniform:
            p.fillRect(0, 0, w, h, QColor(243, 243, 243))
            return
        band = max(1, int(h * 0.08))
        p.fillRect(0, 0, w, band, QColor(*TOP))
        p.fillRect(0, band, w, h - band, QColor(*BODY))
        # 画几条线制造"有内容"的迹象，避免整幅被判纯色
        p.setPen(QColor(90, 95, 105))
        for i in range(6):
            y = band + 40 + i * 60
            p.drawLine(40, y, w - 40, y)


print("=" * 68)
print("三层取色链路验证（像素层）")
print("=" * 68)
print("build=%s  dwm.available=%s" % (sys.getwindowsversion().build,
                                      dwm.available()))

app = QApplication.instance() or QApplication(sys.argv)

# ---- 场景一：顶部纯色工具栏 + 有内容的主体 ----
w1 = Probe()
w1.show()
# ---- 场景二：整幅纯色（模拟没渲染出来）----
w2 = Probe(uniform=True)
w2.show()


def run():
    h1 = int(w1.winId())
    print(f"\n[1] 场景一：顶部工具栏 {dwm.to_hex(TOP)}，主体 {dwm.to_hex(BODY)}")
    got = dwm.capture_window_top(h1)
    print("    capture_window_top ->", dwm.to_hex(got) if got else None)
    check("顶部纯色工具栏不被误判为空白（能取到色）", got is not None, str(got))
    if got:
        check("取到的是工具栏颜色（不是系统标题栏色）",
              dwm.color_near(got, TOP, 30),
              f"{dwm.to_hex(got)} vs 期望 {dwm.to_hex(TOP)}")

    # 直接验证"整幅空白判定"与"顶部取色"是两件事
    data, w, h = dwm._grab_window_bgra(h1)
    if w > 0:
        print(f"\n[2] 整窗 {w}x{h}，直接看 flat_ratio 对比")
        def rgba_rows(y0, n, cw=None):
            # 用一个近似：取整个窗口做 flat 判定
            seg = bytearray()
            for y in range(y0, min(y0 + n, h)):
                row = bytearray(data[y * w * 4:(y + 1) * w * 4])
                row[0::4], row[2::4] = row[2::4], row[0::4]
                seg += row
            return bytes(seg)
        full = rgba_rows(0, h)
        fr = dwm.flat_ratio(full, w, h)
        print(f"    整窗 flat_ratio = {fr:.3f}（<1 说明有内容）")
        check("整窗有内容（flat_ratio < 1）", fr < 1.0, f"{fr:.3f}")

    print(f"\n[3] 场景二：整幅纯色（模拟未渲染）")
    h2 = int(w2.winId())
    got2 = dwm.capture_window_top(h2)
    print("    capture_window_top ->", dwm.to_hex(got2) if got2 else None)
    check("整幅纯色被识别为空白并返回 None", got2 is None, str(got2))

    # L3 的等价检查：整幅纯色时 flat_ratio 应为 1
    data2, w2_, h2_ = dwm._grab_window_bgra(h2)
    if w2_ > 0:
        seg = bytearray()
        for y in range(0, h2_):
            row = bytearray(data2[y * w2_ * 4:(y + 1) * w2_ * 4])
            row[0::4], row[2::4] = row[2::4], row[0::4]
            seg += row
        fr2 = dwm.flat_ratio(bytes(seg), w2_, h2_)
        print(f"    纯色窗 flat_ratio = {fr2:.3f}（应≈1）")
        check("纯色窗 flat_ratio 接近 1", fr2 >= dwm.FLAT_RATIO, f"{fr2:.3f}")

    print("\n" + "=" * 68)
    print("结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 68)
    app.quit()


QTimer.singleShot(800, run)
app.exec()
sys.exit(1 if FAIL else 0)
