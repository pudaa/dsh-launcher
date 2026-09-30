# -*- coding: utf-8 -*-
"""启动性能基线测量。

存在的理由
----------
"快速启动"和"低占用"如果没有基线，就无法判断一次改动是优化还是退化，
也无法回答"还有多少可省"。这个脚本把启动链路拆成可归因的几段，
每段单独计时，并标出**哪一段是我们的、哪一段是 DSH 自己的**。

    python tools/startup_profile.py            # 默认跑 3 轮，取第 2、3 轮为稳态值
    python tools/startup_profile.py --rounds 5
    python tools/startup_profile.py --keep     # 保留临时 DSH_HOME 便于看日志

为什么用临时 DSH_HOME
---------------------
不碰用户正在用的 `D:\\AppData\\dsh`（那里可能有正在运行的实例与会话）。
代价是首轮要初始化 profile，因此**首轮不计入稳态**，只作为"全新安装"参考值。

为什么要点名 `dsh --version` 单独计时
------------------------------------
`resolve_install()` 即使命中缓存也会再 spawn 一次 `dsh --version` 复验——
它落在**关键路径**上（界面要等它才能决定去哪找 DSH），这是我们可控的开销里
最大的一笔。单独计时才能确定"砍掉它值不值"。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROUNDS = 3
for i, a in enumerate(sys.argv):
    if a == "--rounds" and i + 1 < len(sys.argv):
        ROUNDS = max(1, int(sys.argv[i + 1]))
KEEP = "--keep" in sys.argv

TMP = os.path.join(ROOT, ".tmp-startup-profile")
os.environ["DSH_HOME"] = TMP
os.makedirs(TMP, exist_ok=True)

from dsh_host import contract  # noqa: E402

LINE = "-" * 66


def ms(t: float) -> str:
    return f"{t * 1000:.0f} ms"


def rss_of(pid: int) -> int:
    """该进程的 RSS（KB）。取不到返回 0。

    不用 psutil（不想为一次测量引入依赖），直接问 tasklist。
    """
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    # "node.exe","1234","Console","1","53,816 K"
    for line in out.splitlines():
        cells = [c.strip().strip('"') for c in line.split('","')]
        if len(cells) >= 5 and "K" in cells[-1]:
            try:
                return int(cells[-1].replace("K", "").replace(",", "").strip())
            except ValueError:
                return 0
    return 0


def main() -> int:
    print(LINE)
    print("启动性能基线（临时 DSH_HOME，不触碰正在使用的实例）")
    print("DSH_HOME :", TMP)
    print(LINE)

    # ---- 1. 安装定位：关键路径上第一笔开销 ----
    print("[1] 安装定位")
    t0 = time.perf_counter()
    inst = contract.resolve_install(force=True)
    t_cold = time.perf_counter() - t0
    print(f"    首次（含探测与读版本）: {ms(t_cold)}  -> {inst.describe()}")

    t0 = time.perf_counter()
    inst = contract.resolve_install()          # 缓存命中 + 复验
    t_warm = time.perf_counter() - t0
    print(f"    缓存命中（含版本复验）: {ms(t_warm)}")

    t0 = time.perf_counter()
    inst = contract.resolve_install(verify=False)   # 缓存命中 + 不复验
    t_fast = time.perf_counter() - t0
    print(f"    缓存命中（verify=False）: {ms(t_fast)}   <- 启动路径实际走的这一条")

    # 单独测那一次复验的成本
    t0 = time.perf_counter()
    contract.run_capture([inst.node, inst.bin_js, "--version"], timeout=45)
    t_ver = time.perf_counter() - t0
    print(f"    其中 `dsh --version` 一次: {ms(t_ver)}")
    print(f"    -> 复验已挪到启动后的后台线程，关键路径省下约 {ms(t_warm - t_fast)}")

    # ---- 2. 服务启动到就绪 ----
    print(LINE)
    print("[2] 服务启动 → 就绪（launch 到 wait_ready 返回）")
    boots: list[float] = []
    peaks: list[int] = []
    for r in range(1, ROUNDS + 1):
        logf = os.path.join(TMP, f"boot{r}.log")
        t0 = time.perf_counter()
        handle = contract.launch(inst, 0, logf, cwd=TMP)
        try:
            contract.wait_ready(handle, timeout=180)
            dt = time.perf_counter() - t0
            pss = rss_of(handle.pid)
            boots.append(dt)
            peaks.append(pss)
            tag = "（首轮，含 profile 初始化，不计稳态）" if r == 1 else ""
            print(f"    第 {r} 轮: {dt:6.2f}s   端口 {handle.port}   "
                  f"node RSS {pss / 1024:.0f} MB {tag}")
        finally:
            contract.stop(handle)

    steady = boots[1:] if len(boots) > 1 else boots
    avg_boot = sum(steady) / len(steady)
    avg_rss = sum(peaks[1:] or peaks) / len(peaks[1:] or peaks)

    # ---- 3. 归因汇总 ----
    print(LINE)
    print("[3] 归因汇总（稳态）")
    # 首屏可感知路径 ≈ 定位 + 启动就绪；WebEngine 创建另计（见文档 §10.1）
    ours_loc = t_fast
    dsh_boot = avg_boot
    total = ours_loc + dsh_boot
    print(f"    安装定位（我方可控，快路径）: {ours_loc:6.2f}s  "
          f"({ours_loc / total * 100:4.1f}%)")
    print(f"    DSH 服务就绪（上游主导）  : {dsh_boot:6.2f}s  "
          f"({dsh_boot / total * 100:4.1f}%)")
    print(f"    ------------------------------")
    print(f"    合计（不含 WebEngine 创建）: {total:6.2f}s")
    print(f"    服务进程占用参考          : {avg_rss / 1024:.0f} MB（node RSS，仅服务本身）")
    print(f"    本次已从关键路径移走      : {ms(t_warm - t_fast)}（版本复验，改到后台线程）")
    print(LINE)
    print("说明：")
    print("  · WebEngine 实例未计入——它在服务就绪后才串行创建，是独立的一笔")
    print("    固定成本（含 QtWebEngineProcess 子进程）。要做端到端结论需实测 GUI。")
    print("  · 我方可控部分已经压到接近 0（只读缓存文件）。再想快就只能动架构，")
    print("    而真正的 7 秒在 DSH 自己的插件加载上——所以重点应放在**感知优化**：")
    print("    让窗口先出来并给进度，而不是让用户白屏等。")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        if not KEEP:
            shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
