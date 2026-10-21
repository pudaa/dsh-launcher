# -*- coding: utf-8 -*-
"""数据目录后台任务的真实冒烟测试（清缓存 / 迁移 / 进度信号）。

为什么要有它（2026-10-21）
--------------------------
`tools/gui_selftest.py` 的断言是**源码字符串检查**——它能拦住"有人把
`copytree` 改回界面线程"，但拦不住"后台任务跑起来其实是死的"。这里补的是
真跑一遍：真起 QThread、真删/真复制文件、真收进度信号。

两条红线
--------
1. **绝不碰用户真实数据根**：`config.data_root / default_root / pointer_path`
   全部 monkeypatch 到临时目录，只在当前进程内生效。
2. **不建主窗口**：只建 QApplication + 一个空 QWidget 当父窗口，
   避免真去连 DSH 服务。

用法：
    QT_QPA_PLATFORM=offscreen python tools/datatask_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget    # noqa: E402

from dsh_host import config, storage                   # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                           ("  " + extra) if extra else ""))
    if not ok:
        FAILURES.append(name)


def make_tree(root: str, plan: dict) -> int:
    """按 {分组名: 文件数} 造数据；返回文件总数。"""
    total = 0
    for grp, n in plan.items():
        d = os.path.join(root, grp)
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            with open(os.path.join(d, "%s%04d.bin" % (grp, i)), "wb") as f:
                f.write(b"z" * 256)
            total += 1
    return total


def run_worker(app: QApplication, worker, timeout_s: float = 120.0) -> dict:
    """跑一个 DataTaskWorker 并收齐它的信号（模拟模态窗里的事件循环）。"""
    marks = {"lines": 0, "progress": 0, "done": None}

    def on_line(_msg):
        marks["lines"] += 1

    def on_progress(_d, _t, _name=""):
        marks["progress"] += 1

    def on_done(ok, detail, payload):
        marks["done"] = {"ok": ok, "detail": detail, "payload": payload}

    worker.line.connect(on_line)
    worker.progress.connect(on_progress)
    worker.done.connect(on_done)
    t0 = time.time()
    worker.start()
    while worker.isRunning() and time.time() - t0 < timeout_s:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    worker.wait(3000)
    marks["elapsed"] = time.time() - t0
    return marks


def count_files(root: str) -> int:
    return sum(len(fs) for _r, _d, fs in os.walk(root))


def main() -> int:
    base = tempfile.mkdtemp(prefix="dsh-datatask-")
    app = QApplication(sys.argv)
    parent = QWidget()                                 # noqa: F841

    import dsh_gui_qt as G

    # ---------------- ① 清缓存：走后台 + 有进度 + 真删干净 ----------------
    root = os.path.join(base, "clear-root")
    made = make_tree(root, {"profile": 300, "logs": 60, "cache": 140})
    config.default_root = lambda: os.path.join(base, "ptr")
    config.pointer_path = lambda: os.path.join(base, "ptr", "location.json")
    config.data_root = lambda: root
    config._root_cache = None

    w = G.DataTaskWorker("clear_cache", None)
    m = run_worker(app, w)
    left = count_files(root)
    payload = (m["done"] or {}).get("payload") or {}
    check("清缓存任务成功", bool((m["done"] or {}).get("ok")),
          str((m["done"] or {}).get("detail"))[:120])
    check("清缓存删除了全部文件", left == 0, "剩余 %d / 原有 %d" % (left, made))
    check("清缓存回报的数量正确", payload.get("files") == made,
          "%s vs %s" % (payload.get("files"), made))
    check("清缓存有进度回调", m["progress"] >= 1, "%d 次" % m["progress"])
    check("进度回调有节流（远少于文件数）",
          m["progress"] < made, "%d 次 / %d 文件" % (m["progress"], made))

    # ---------------- ② 迁移：复制 + 核对 + 清旧目录 + 切指针 ----------------
    src = os.path.join(base, "old-root")
    made2 = make_tree(src, {"profile": 200, "logs": 40, "cache": 60})
    config.data_root = lambda: src
    config._root_cache = None
    target = os.path.join(base, "new-root")

    w2 = G.DataTaskWorker("migrate", None, target=target, old=src)
    m2 = run_worker(app, w2)
    p2 = (m2["done"] or {}).get("payload") or {}
    check("迁移任务成功", bool((m2["done"] or {}).get("ok")),
          str((m2["done"] or {}).get("detail"))[:120])
    check("新位置文件齐全", count_files(target) == made2,
          "%d / %d" % (count_files(target), made2))
    check("旧目录已清空", count_files(src) == 0, "%d 个残留" % count_files(src))
    check("指针已切到新位置", config.pointer_path()
          and os.path.isfile(config.pointer_path())
          and target.replace("\\", "/") in open(
              config.pointer_path(), encoding="utf-8").read().replace("\\\\", "/"),
          config.pointer_path())
    check("迁移有进度回调", m2["progress"] >= 1, "%d 次" % m2["progress"])

    # ---------------- ③ 失败路径：目标非空必须报错而不是硬搬 ----------------
    dirty = os.path.join(base, "dirty")
    os.makedirs(dirty, exist_ok=True)
    open(os.path.join(dirty, "x.txt"), "w").write("x")
    config.data_root = lambda: target
    config._root_cache = None
    w3 = G.DataTaskWorker("migrate", None, target=dirty, old=target)
    m3 = run_worker(app, w3)
    err = str((m3["done"] or {}).get("detail") or "")
    check("目标非空时拒绝迁移", not (m3["done"] or {}).get("ok"), err[:60])
    check("拒绝时给出可读原因", "不是空的" in err, err[:60])
    check("拒绝时源数据仍在", count_files(target) == made2)

    print()
    if FAILURES:
        print("数据任务冒烟：%d 项失败 —— %s" % (len(FAILURES), "、".join(FAILURES)))
        return 1
    print("数据任务冒烟：全部通过（清缓存 / 迁移 / 失败路径）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
