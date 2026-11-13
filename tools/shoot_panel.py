# -*- coding: utf-8 -*-
"""把控制面板概览页渲染成 PNG，用于人眼核对版式。

`gui_selftest.py` 只断言"是不是对的"，看不出"好不好看"——留白、对齐、
数字与标签的视觉重量这几件事只能看图（2026-11-12 加内存栏时用的）。

    python tools/shoot_panel.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# 2 倍渲染。以前是 1 倍，grab 出来 880px 宽，放进 README 再被缩到 820，
# 12px 的正文就糊成一团（2026-11-12 老大指出"超低分辨率，无法使用"）。
# 必须在 QApplication 创建之前设，之后改无效。
os.environ["QT_SCALE_FACTOR"] = "2"

from PySide6.QtCore import Qt                                    # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter               # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

from dsh_ui import panel as dsh_panel                                                 # noqa: E402
from dsh_host import contract                                    # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def shoot(theme: str, out_path: str, root_pid: int | None = None,
          page: int = 0, demo_remote: bool = False) -> None:
    rep = contract.diagnose(verify=False)
    kw = {}
    if demo_remote:
        from dsh_host import remote
        fake = [remote.SshHost(name="家里的服务器", host="frp-six", user="cx",
                               keep_service=remote.KEEP_KEEP),
                remote.SshHost(name="临时机器", host="10.0.0.5", port=2222)]
        # 用"未连接"而不是"连接中"：后者会让读者以为远程卡住了。
        # 首发状态本来就是没连，展示这个更诚实。
        state = {}
        kw.update(remote_hosts=lambda: list(fake),
                  remote_state=lambda: dict(state))
    host = dsh_panel.PanelHost(install=rep.install, handle=None,
                               theme=lambda: theme, **kw)
    panel = dsh_panel.ControlPanel(host)
    panel.show()
    pump(3.2)                     # 等一轮内存采样回来（间隔 2 秒）
    if page:
        panel.goto(page)
        pump(0.8)

    # `--pid` 时用真实的整棵进程树渲染一遍：本脚本自己没有子进程，
    # 只有指定真实壳的 pid 才看得到"网页 / 后台服务"都有数的样子。
    if root_pid:
        from dsh_host import procmem
        op = panel.pages.get(0)
        if op is not None:
            op._apply_mem(procmem.snapshot(root_pid))
            pump(0.2)

    shot = panel.grab().toImage()
    # 圆角外的区域是透明的，垫一层纯色才看得出边界。
    # **画布必须按物理像素建、并继承 devicePixelRatio**：`QPixmap.size()` 是逻辑
    # 尺寸，直接拿它建画布会把 2 倍图缩回 1 倍——README 里那张糊图就是这么来的。
    canvas = QImage(shot.size(), QImage.Format.Format_RGB32)
    canvas.setDevicePixelRatio(shot.devicePixelRatio())
    canvas.fill(QColor("#8a8d92"))
    p = QPainter(canvas)
    p.drawImage(0, 0, shot)
    p.end()
    canvas.save(out_path)
    print("已保存", out_path)

    panel.shutdown(4000)
    panel.close()


if __name__ == "__main__":
    app = QApplication([])                                       # noqa: F841
    pid, page, demo = 0, 0, "--demo-remote" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--pid" and i + 1 < len(sys.argv):
            pid = int(sys.argv[i + 1])
        if a == "--page" and i + 1 < len(sys.argv):
            page = int(sys.argv[i + 1])
    tag = "page%d" % page if page else "mem"
    shoot("light", os.path.join(OUT, "panel-%s-light.png" % tag), pid or None,
          page=page, demo_remote=demo)
    shoot("dark", os.path.join(OUT, "panel-%s-dark.png" % tag), pid or None,
          page=page, demo_remote=demo)
