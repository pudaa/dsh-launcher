# -*- coding: utf-8 -*-
"""诊断：加载真实 DSH 页面，逐项报告各取色来源的表现。

⚠️ **必须在沙箱外运行**（老大直接双击 / 自己开终端跑）。
   在 WorkBuddy 沙箱里跑不出结果：Chromium 渲染进程会被杀，
   实测拿到 `renderProcessTerminated / KilledTerminationStatus code=1`，
   网页根本画不出来，所以任何截图都是空白，测了也没意义。

用途
----
`_sample_titlebar_color` 是三层降级取色（JS -> 窗口像素 -> 控件截图）。
本脚本把每一层的结果都打出来，用来定位"实机没上色"卡在哪一层。

判断标准
--------
  · loadFinished 必须 True，否则后面的结论都不成立
  · renderProcessTerminated 不该出现
  · 各层的"颜色数"要远大于 1（一片纯色说明没截到内容）

关于 v1 的教训（保留在此以免重蹈）
----------------------------------
v1 拿到 stdout 的 URL 就立即 load，得到 `loadFinished ok=False`，
于是四张图全是纯色 —— 差点据此得出"grab() 截不到 WebEngine"的
**错误结论**。实际是页面压根没加载。

两处差异已在本脚本修正：
  1. 用**具名 profile** + 显式 page（与应用一致）
  2. load 前加 **HTTP 就绪门**（等 303）—— 契约文档早就记载过
     "只看 TCP 会在读到 URL 前就返回就绪、首屏 401"这个坑
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTimer, QUrl                             # noqa: E402
from PySide6.QtGui import QImage                                    # noqa: E402
from PySide6.QtWidgets import (QApplication, QLabel, QMainWindow,     # noqa: E402
                               QVBoxLayout, QWidget)
from PySide6.QtWebEngineCore import (QWebEngineProfile,              # noqa: E402
                                     QWebEnginePage)
from PySide6.QtWebEngineWidgets import QWebEngineView              # noqa: E402

from dsh_host import config, dwm                                    # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

LIFETIME = int(sys.argv[1]) if len(sys.argv) > 1 else 75
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_capture_out")
os.makedirs(OUT, exist_ok=True)

DSH_BIN = r"D:\DevVmEnv\nodejs\node_modules\@deepseek-ai\dsh\lib\bin.js"
NODE = r"D:\DevVmEnv\nodejs\node.exe"
DSH_HOME = os.environ.get("DSH_HOME") or r"D:\AppData\dsh"


# ------------------------------------------------------------------ 工具

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


def printwindow_bgra(hwnd: int):
    rect = _RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    w, h = rect.r - rect.l, rect.b - rect.t
    if w <= 0 or h <= 0:
        return b"", 0, 0
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


def _rgba(qi: QImage):
    img = qi.convertToFormat(QImage.Format.Format_RGBA8888)
    mv = img.constBits()
    if getattr(mv, "itemsize", 1) != 1:
        mv = mv.cast("B")
    return img, bytes(mv), img.bytesPerLine()


def dominant(qi: QImage, frac=0.06):
    img, raw, stride = _rgba(qi)
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return None, 0
    rows = max(1, int(h * frac))
    pad = max(0, int(w * 0.02))
    x0, span = pad, max(1, w - pad * 2)
    data = b"".join(raw[y * stride + x0 * 4: y * stride + (x0 + span) * 4]
                    for y in range(rows))
    return dwm.dominant_color(data, span, rows), rows


def color_count(qi: QImage):
    """近似统计出现的不同颜色数 + 最多颜色的占比。判定是否一片空白。"""
    img, raw, stride = _rgba(qi)
    w, h = img.width(), img.height()
    seen: dict = {}
    for y in range(0, h, max(1, h // 60)):
        for x in range(0, w, max(1, w // 120)):
            i = y * stride + x * 4
            if i + 2 >= len(raw):
                continue
            k = (raw[i], raw[i + 1], raw[i + 2])
            seen[k] = seen.get(k, 0) + 1
    if not seen:
        return 0, "-"
    top, cnt = max(seen.items(), key=lambda kv: kv[1])
    return len(seen), "%s %d%%" % (dwm.to_hex(top), cnt * 100 // sum(seen.values()))


def http_status(url: str, timeout=4):
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:                                              # noqa: BLE001
        return 0


# ------------------------------------------------------------ 1. 启服务

print("=" * 72)
print("DSH 真实页面取色探针 v2")
print("=" * 72)
print(f"\n[1] 启动 dsh web（DSH_HOME={DSH_HOME}）")
proc = subprocess.Popen(
    [NODE, DSH_BIN, "web", "--port", "0", "--no-open"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    cwd=os.path.dirname(DSH_BIN), text=True, encoding="utf-8",
    errors="replace", env={**os.environ, "DSH_HOME": DSH_HOME},
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

url, t0 = None, time.time()
while time.time() - t0 < 60:
    line = proc.stdout.readline()
    if not line:
        if proc.poll() is not None:
            break
        continue
    if "http://127.0.0.1" in line:
        m = re.search(r"(http://127\.0\.0\.1:\d+/\?token=\S+)", line)
        if m:
            url = m.group(1)
            print("    stdout 里拿到:", url[:64] + "…")
            break
    if line.strip():
        print("    |", line[:100])

if not url:
    print("    ❌ 没拿到 URL")
    proc.terminate()
    sys.exit(1)

threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()

# 就绪门：等 HTTP 真的能服务（契约文档记载首屏 401 的坑）
print("\n[2] 等 HTTP 就绪（期望 303）")
ready = False
for i in range(40):
    st = http_status(url)
    if i < 3 or st == 303:
        print(f"    第 {i+1} 次探测 -> {st}")
    if st == 303:
        ready = True
        break
    time.sleep(0.5)
print("    就绪" if ready else "    ⚠️ 一直没到 303，仍继续尝试加载")


# ------------------------------------------------------------ 3. 开窗

app = QApplication.instance() or QApplication(sys.argv)
win = QMainWindow()
win.setWindowTitle("DSH 取色探针 v2（真实页面）")
central = QWidget()
lay = QVBoxLayout(central)
lay.setContentsMargins(0, 0, 0, 0)
lay.setSpacing(0)
bar = QLabel("加载中…（窗口可见，请观察网页是否真的渲染出来）")
bar.setStyleSheet("background:#333;color:#eee;padding:6px;font:12px sans-serif")
lay.addWidget(bar)

# **完全复刻应用的做法**：具名 profile + 显式 page
# （持久化目录用独立的临时目录 —— 应用可能正在运行并占用它自己的 profile，
#   共用会撞锁；本探针只是验证"取色能力"，不需要共享登录态）
import tempfile                                                   # noqa: E402
profile = QWebEngineProfile("dsh-probe", win)
prof_dir = os.path.join(tempfile.gettempdir(), "dsh-probe-profile")
try:
    os.makedirs(prof_dir, exist_ok=True)
    profile.setPersistentStoragePath(prof_dir)
    print(f"\n[3] profile 持久化目录 = {prof_dir}")
except Exception as e:                                             # noqa: BLE001
    print("\n[3] profile 目录设置失败:", e)

view = QWebEngineView(win)
page = QWebEnginePage(profile, view)
view.setPage(page)
lay.addWidget(view, 1)
win.setCentralWidget(central)
win.resize(1100, 720)
win.show()

STATE = {"loaded": None, "crashed": False, "captured": False}


def on_load_finished(ok):
    STATE["loaded"] = bool(ok)
    print(f"\n[4] loadFinished ok={ok}")
    print(f"    page.url = {page.url().toString()[:80]}")
    bar.setText(f"loadFinished ok={ok}，等 6 秒…")
    QTimer.singleShot(6000, do_capture)


def on_render_terminated(status, code):
    STATE["crashed"] = True
    print(f"\n[!] renderProcessTerminated status={status} code={code}")
    print("    渲染进程死了 —— 这个进程里网页根本画不出来，"
          "任何像素截图都必然是空白")


view.loadFinished.connect(on_load_finished)
page.renderProcessTerminated.connect(on_render_terminated)
view.loadProgress.connect(lambda p: None)
view.load(QUrl(url))


def do_capture():
    if STATE["captured"]:
        return
    STATE["captured"] = True
    print("\n[5] 截图对比")
    hwnd = int(win.winId())

    qa = view.grab().toImage()
    nA, topA = color_count(qa)
    dA, _ = dominant(qa)
    pa = os.path.join(OUT, "v2_A_view_grab.png")
    qa.save(pa)
    print(f"  A view.grab()    {qa.width()}x{qa.height()}  颜色数 {nA} "
          f"最多色 {topA}  主色调 {dwm.to_hex(dA) if dA else None}")

    data, w, h = printwindow_bgra(hwnd)
    if w > 0:
        from PySide6.QtGui import QImage as _QI
        qb = _QI(data, w, h, w * 4, _QI.Format.Format_ARGB32).copy()
        nB, topB = color_count(qb)
        dB, _ = dominant(qb)
        pb = os.path.join(OUT, "v2_B_printwindow.png")
        qb.save(pb)
        print(f"  B PrintWindow    {w}x{h}  颜色数 {nB} "
              f"最多色 {topB}  主色调 {dwm.to_hex(dB) if dB else None}")

    qd = win.grab().toImage()
    nD, topD = color_count(qd)
    dD, _ = dominant(qd)
    pd = os.path.join(OUT, "v2_D_window_grab.png")
    qd.save(pd)
    print(f"  D win.grab()     {qd.width()}x{qd.height()}  颜色数 {nD} "
          f"最多色 {topD}  主色调 {dwm.to_hex(dD) if dD else None}")

    print(f"\n[6] 判定")
    print(f"    loadFinished = {STATE['loaded']}")
    print(f"    渲染进程崩溃 = {STATE['crashed']}")
    print(f"    A 颜色数={nA}  B 颜色数={nB}  D 颜色数={nD}")
    if nA <= 1 and nB <= 10:
        print("    → 截图里全是纯色，**页面没渲染**，"
              "不能用来说明 grab() 的能力")
    else:
        print("    → 截图里有丰富颜色，可用于判断取色方案是否可行")

    print(f"\n    产出：{OUT}")
    print(f"    窗口保留 {LIFETIME}s 供肉眼观察")
    bar.setText("截图已完成 —— 请对照这个窗口看网页是否真的渲染出来了")


QTimer.singleShot(LIFETIME * 1000, app.quit)
app.exec()

print("\n[7] 收尾")
proc.terminate()
try:
    proc.wait(timeout=8)
except Exception:                                                  # noqa: BLE001
    proc.kill()
print("    已停止 dsh 服务")
sys.stdout.flush()
os._exit(0)          # 绕开 WebEngine 退出时的段错误噪音
