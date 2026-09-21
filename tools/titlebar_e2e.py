# -*- coding: utf-8 -*-
"""端到端：用真实 GUI 窗口验证 `_apply_titlebar` 的完整调用链。

前面的 dwm_verify.py 验证的是 dwm 模块的**基本能力**；
本脚本验证的是**接线**：从 config 读 mute → desaturate → contrast_text
→ is_dark → 四个 DWM 调用 → GDI 截图确认像素落地。

关键在于它用的是真实的主窗口类，而不是裸 QMainWindow。
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------
# 关键：隔离配置目录，绝不写真实 settings.json
# ---------------------------------------------------------------
# 教训（2026-09-20）：本脚本原先直接调 config.set(adaptive_titlebar=False)，
# 而 config 的数据根是 %LOCALAPPDATA%\DSH-Web —— 那是**老大的真实配置**。
# 结果：跑一次测试就把「标题栏跟随界面配色」永久关掉了，导致老大
# 实机运行时完全没效果，白排查一场。
#
# config._local_appdata() 每次调用都读环境变量，所以在导入任何会写配置的
# 模块**之前**改写 LOCALAPPDATA，就能把全部读写重定向到沙箱目录。
# 必须放在 dsh_host 导入之前。
_SANDBOX = os.path.join(tempfile.gettempdir(), "dsh-launcher-test-cfg")
os.makedirs(_SANDBOX, exist_ok=True)
os.environ["LOCALAPPDATA"] = _SANDBOX

from PySide6.QtCore import QTimer                                    # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow              # noqa: E402

from dsh_host import config, dwm                                     # noqa: E402

# 断言隔离生效——不生效就立刻失败，而不是悄悄污染老大配置
_real_root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "DSH-Web")
assert config.data_root().startswith(_SANDBOX), (
    "配置目录隔离失败！data_root=%s 不在沙箱 %s 内，拒绝继续以免污染真实配置"
    % (config.data_root(), _SANDBOX))

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PASS: list[str] = []
FAIL: list[str] = []
#: 是否走到最后一步。用来区分"跑完"和"中途异常"——只看 PASS 非空不够。
FINISHED = False


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
    """借用真实实现的方法，避免构造完整窗口（会连服务）。

    要点：**借用的方法所依赖的其他方法也必须一起借**。
    之前漏了 _set_theme_icon —— _apply_titlebar 内部会调它，
    结果在 Qt 槽里抛 AttributeError 把 app.quit() 一起带走，
    app.exec() 永久阻塞（表现为"测试挂住不返回"，很误导）。
    """
    _apply_titlebar = g.MainWindow._apply_titlebar
    _sample_titlebar_color = g.MainWindow._sample_titlebar_color
    _set_theme_icon = g.MainWindow._set_theme_icon


win = Probe()
win.setWindowTitle("E2E titlebar")
win.resize(500, 260)
win.show()

DOM = (0x22, 0xC1, 0xA3)          # 「青云学」主题色，模拟界面主色


def step():
    mute = float(config.get("titlebar_mute") or 0.0)
    expect = dwm.desaturate(DOM, mute)
    print("\n[1] 模拟界面主色 %s，mute=%.2f" % (dwm.to_hex(DOM), mute))
    print("    预期标题栏底色 = %s（降饱和 %d%%）"
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
        check("标题栏落地为 desaturate 后的颜色", len(after) >= 8,
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
        global FINISHED
        FINISHED = True
        app.quit()

    QTimer.singleShot(900, verify)


QTimer.singleShot(500, step)
# 兜底：无论中途哪个回调抛异常，都不能让 app.exec() 永久阻塞。
# 没有它的时候，一次 AttributeError 就让测试挂死（排查起来很误导）。
#
# 判据用 FINISHED，不是"PASS 非空"——后者在"前几步成功、之后异常"
# 的情况下会把失败误判成通过。
QTimer.singleShot(20000, app.quit)
app.exec()
if FAIL or not FINISHED:
    if not FINISHED:
        print("\n⚠️ 测试未跑完（中途异常），视为失败")
    sys.exit(1)
sys.exit(0)
