# -*- coding: utf-8 -*-
"""诊断：QStackedWidget 里「隐藏着创建」的 QWebEngineView，何时才把画面画满。

复刻 dsh_gui_qt.MainWindow 的关键时序：

    show 窗口 → 80ms 创建 QWebEngineView（父=窗口，默认 100x30）
    → addWidget 进 QStackedWidget（不是当前页 → 隐藏）
    → 隐藏状态下 load()（Chromium 以 100x30 的视口开始排版）
    → 等一会儿再 setCurrentIndex(1)（视图一步跳到全尺寸）

页面画了一圈 6px 纯红描边。于是「红框包围盒」就是**网页实际画出来的视口**：

    · 红框 = 100x30 贴左上  → 就是看到的"左上角一小块"
    · 红框 = 空（没有红像素）→ 那一帧网页还没上屏，露的是 Qt 背景
    · 红框 = 客户区全尺寸    → 已对齐

同一套装置顺带量"缩放响应"：改窗口大小后每 16ms 截一帧，记录红框从哪一
帧起跟上了新尺寸。

用法：
    python tools/webview_flicker_diag.py [--out .diag]

产出：<out>/frame-*.png、summary.txt、summary.json
注意：会真的弹一个窗口约 6 秒（必须真 GPU 合成，离屏测不出这个问题）。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

from PySide6.QtCore import Qt, QTimer, QUrl                       # noqa: E402
from PySide6.QtGui import QImage                                  # noqa: E402
from PySide6.QtWidgets import (QApplication, QLabel, QMainWindow,  # noqa: E402
                               QProgressBar, QStackedLayout, QStackedWidget,
                               QVBoxLayout, QWidget)
from PySide6.QtWebEngineWidgets import QWebEngineView             # noqa: E402

import dsh_host.dwm as dwm                                       # noqa: E402

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;overflow:hidden;background:#16181d;
            color:#e6e8eb;font:16px/1.4 'Segoe UI','Microsoft YaHei',sans-serif}
  #grid{position:absolute;inset:0;
        background:repeating-linear-gradient(90deg,#20242b 0 48px,#1a1e24 48px 96px)}
  #frame{position:absolute;inset:0;border:6px solid #ff3b30;box-sizing:border-box}
  #hud{position:absolute;left:18px;top:18px;font-size:40px;font-weight:700;color:#4a9eff}
</style></head><body>
  <div id="grid"></div><div id="frame"></div><div id="hud">boot</div>
<script>
  function upd(){
    document.getElementById('hud').textContent = innerWidth + ' x ' + innerHeight;
    document.title = innerWidth + 'x' + innerHeight;
  }
  addEventListener('resize', upd);
  upd();
</script></body></html>
"""

#: 切页后按节拍截多少帧（每帧 ~16ms，即 60Hz 下的一屏）
REVEAL_FRAMES = 24
#: 缩放对照：每次改到哪个尺寸
RESIZE_STEPS = [(1100, 700), (900, 620), (1200, 760), (1360, 860)]
#: 每次缩放后截多少帧
RESIZE_FRAMES = 20

INTERVAL_MS = 16


def _client_box(hwnd: int, w: int, h: int):
    """客户区在整窗位图里的 (ox, oy, cw, ch)。取不到就退回整窗。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        crect = dwm._RECT()
        user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(crect))
        pt = dwm._POINT(0, 0)
        user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt))
        wrect = dwm._RECT()
        user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(wrect))
        ox = max(0, pt.x - wrect.left)
        oy = max(0, pt.y - wrect.top)
        return ox, oy, min(crect.right, w - ox), min(crect.bottom, h - oy)
    except Exception:                                          # noqa: BLE001
        return 0, 0, w, h


def _red_bbox(data: bytes, w: int, h: int, box, step: int = 2):
    """找红色描边像素的包围盒。data 是 BGRA（自上而下）。"""
    ox, oy, cw, ch = box
    stride = w * 4
    x0 = y0 = 1 << 30
    x1 = y1 = -1
    count = 0
    for y in range(oy, oy + ch, step):
        base = y * stride
        for x in range(ox, ox + cw, step):
            i = base + x * 4
            b, g, r = data[i], data[i + 1], data[i + 2]
            if r > 170 and g < 90 and b < 90:
                count += 1
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if count == 0:
        return None, 0
    return [x0 - ox, y0 - oy, x1 - ox + 1, y1 - oy + 1], count


class Diag(QMainWindow):
    def __init__(self, out_dir: str, mode: str = "stackone"):
        super().__init__()
        self.out = out_dir
        self.mode = mode
        self.setWindowTitle("DSH WebEngine 诊断（会自动关闭）")
        self.resize(1360, 860)
        self.move(60, 60)

        self.stack = QStackedWidget()
        if mode == "stackall":
            # 关键对照：StackAll 会把**所有页**都排版并显示，只把当前页 raise 到最上层
            self.stack.layout().setStackingMode(
                QStackedLayout.StackingMode.StackAll)
        self.setCentralWidget(self.stack)
        loading = QWidget()
        lay = QVBoxLayout(loading)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setFixedHeight(3)
        tip = QLabel("正在启动 DSH 服务…")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(bar)
        lay.addStretch(1)
        lay.addWidget(tip)
        lay.addStretch(1)
        loading.setStyleSheet(
            "QWidget{background:#16181d;}QLabel{color:#8b949e;}")
        self.stack.addWidget(loading)

        self.view = None
        self.page = None
        self.frames = []
        self.step = 0            # 当前阶段内已截帧数
        self.phase = ""
        self.phase_t0 = 0.0
        self.resize_idx = 0
        self.done = False

        self._timer = QTimer(self)
        self._timer.setInterval(INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)

    # ---------------------------------------------------------- 时序
    def start(self):
        self.show()
        QTimer.singleShot(80, self._ensure_webview)

    def _ensure_webview(self):
        # 与 dsh_gui_qt._ensure_webview 同构：父是窗口、默认 100x30
        self.view = QWebEngineView(self)
        self.stack.addWidget(self.view)
        if self.mode == "stackall":
            # 新加的页会被顶到最上层，这里把加载页重新 raise 回来
            self.stack.setCurrentIndex(0)
        self.page = self.view.page()
        self.page.setHtml(HTML, QUrl("about:blank"))
        self._inner_widget = None
        QTimer.singleShot(1200, self._reveal)

    def _reveal(self):
        self._capture("pre-reveal")
        self.stack.setCurrentIndex(1)
        self.phase = "reveal"
        self.phase_t0 = time.perf_counter()
        self.step = 0
        self._timer.start()

    def _on_tick(self):
        if self.done:
            return
        self._capture(self._tag())
        self.step += 1
        limit = REVEAL_FRAMES if self.phase == "reveal" else RESIZE_FRAMES
        if self.step < limit:
            return
        if self.phase == "reveal":
            self._next_resize()
        else:
            self._next_resize()

    def _tag(self):
        if self.phase == "reveal":
            return "reveal-%02d" % self.step
        return "resize%d-%dx%d-%02d" % (
            self.resize_idx + 1, self._cur[0], self._cur[1], self.step)

    def _next_resize(self):
        self._timer.stop()
        if self.resize_idx >= len(RESIZE_STEPS):
            self._finish()
            return
        self._cur = RESIZE_STEPS[self.resize_idx]
        self.resize(*self._cur)
        self.phase = "resize"
        self.phase_t0 = time.perf_counter()
        self.step = 0
        self.resize_idx += 1
        self._timer.start()

    # ---------------------------------------------------------- 截帧
    def _capture(self, tag):
        t_ms = (time.perf_counter() - self.phase_t0) * 1000.0
        hwnd = int(self.winId())
        data, w, h = dwm._grab_window_bgra(hwnd)
        if not data:
            self.frames.append({"tag": tag, "t_ms": t_ms, "error": "grab failed"})
            return
        box = _client_box(hwnd, w, h)
        bbox, count = _red_bbox(data, w, h, box)
        img = QImage(data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        name = "frame-%03d-%s.png" % (len(self.frames), tag)
        img.save(os.path.join(self.out, name))
        inner = self._find_inner()
        self.frames.append({
            "tag": tag,
            "t_ms": round(t_ms, 1),
            "view_size": [self.view.width(), self.view.height()],
            "inner_widget": inner,
            "client_size": [box[2], box[3]],
            "page_inner": self.page.title(),
            "red_bbox": bbox,
            "red_count": count,
            "png": name,
        })
        # 只在头几帧额外留一张控件自绘的图（QQuickWidget 走自己的离屏路径）
        if len(self.frames) <= 3:
            v = self.view.grab()
            v.save(os.path.join(self.out, "view-%03d-%s.png" % (len(self.frames), tag)))

    def _find_inner(self):
        from PySide6.QtWidgets import QWidget as _W
        kids = [c for c in self.view.findChildren(_W) if c is not self.view]
        if not kids:
            return None
        g = kids[0].geometry()
        return [g.width(), g.height(), type(kids[0]).__name__]

    def _finish(self):
        self.done = True
        rows = []
        for fr in self.frames:
            b = fr.get("red_bbox")
            if fr.get("error"):
                rows.append("%-28s %s" % (fr["tag"], fr["error"]))
                continue
            iw = fr.get("inner_widget")
            if b is None:
                shape = "无红框"
            elif b[2] - b[0] < 60 or b[3] - b[1] < 40:
                shape = "小框 %dx%d@(%d,%d)" % (b[2] - b[0], b[3] - b[1], b[0], b[1])
            elif abs((b[2] - b[0]) - fr["client_size"][0]) <= 4:
                shape = "满幅 %dx%d" % (b[2] - b[0], b[3] - b[1])
            else:
                shape = "旧尺寸 %dx%d" % (b[2] - b[0], b[3] - b[1])
            rows.append(
                "%-28s t=%6.1fms view=%dx%d inner=%s client=%dx%d innerHTML=%-10s %s"
                % (fr["tag"], fr["t_ms"], fr["view_size"][0], fr["view_size"][1],
                   ("%dx%d" % (iw[0], iw[1])) if iw else "?",
                   fr["client_size"][0], fr["client_size"][1],
                   fr["page_inner"], shape))
        text = "\n".join(rows)
        with open(os.path.join(self.out, "summary.txt"), "w",
                  encoding="utf-8") as f:
            f.write(text + "\n")
        with open(os.path.join(self.out, "summary.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.frames, f, ensure_ascii=False, indent=2)
        print(text)
        self.close()
        QApplication.quit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".diag")
    ap.add_argument("--mode", choices=("stackone", "stackall"),
                    default="stackone")
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    app = QApplication(sys.argv)
    win = Diag(out, args.mode)
    win.start()
    QTimer.singleShot(25000, app.quit)      # 兜底
    app.exec()
    return 0 if os.path.isfile(os.path.join(out, "summary.txt")) else 1


if __name__ == "__main__":
    sys.exit(main())
