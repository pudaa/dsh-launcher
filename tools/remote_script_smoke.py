# -*- coding: utf-8 -*-
"""远程脚本的冒烟测试：用**本地 sh** 跑一遍 probe / start / stop。

为什么需要它
------------
`dsh_host/remote.py` 里的三段脚本是发给远端执行的，语法错误、`fi` 漏写、
变量名打错这类问题在本地"看代码"看不出来，而真机验证需要一台可达的 SSH
主机——每次改动都要连过去试，既慢又容易把别人的机器搞脏。

这里用本地 sh 扮演远端：临时 HOME（脚本只碰 `$HOME/.dsh-remote`）+ 临时
DSH_HOME（不碰用户的 DSH），把"启动 → 拿到带令牌地址 → 探测到复用 → 停止"
整条链路跑一遍。`tools/contract_selftest.py` 与发版三件套都会调它。

    python tools/remote_script_smoke.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from dsh_host import contract, remote  # noqa: E402


def run_script(sh: str, script: str, home: str, env: dict,
               timeout: float = 30) -> tuple[int, str, str]:
    proc = subprocess.run([sh, "-s"], input=script.encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, env=env, cwd=home)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def main() -> int:
    sh = shutil.which("sh")
    if not sh:
        print("找不到 sh，跳过（Windows 上需要 Git Bash）")
        return 0

    inst = contract.resolve_install(verify=False)
    tmp = tempfile.mkdtemp(prefix="dsh-remote-smoke-")
    home = os.path.join(tmp, "home")
    dsh_home = os.path.join(tmp, "dshhome")
    os.makedirs(home, exist_ok=True)
    os.makedirs(dsh_home, exist_ok=True)

    env = os.environ.copy()
    # 脚本靠 $HOME 定位状态目录；DSH 靠 DSH_HOME 定位自己的数据。
    # 两个都指到临时位置，保证这个测试可反复跑、不残留、不碰用户数据。
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["DSH_HOME"] = dsh_home
    env.pop("DSH_PROFILE_DIR", None)

    # Windows 上没有 `dsh` 这个可执行名（是 .cmd），所以用 node + bin.js。
    # 参数里带空格，正好顺带验证命令的引号处理。
    cmd = '"%s" "%s" web' % (inst.node, inst.bin_js)

    failures: list[str] = []
    skipped: list[str] = []
    passed = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal passed
        print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                               ("  " + extra) if extra else ""))
        if ok:
            passed += 1
        else:
            failures.append(name)

    def skip(name: str, why: str) -> None:
        print("  [SKIP] %s  %s" % (name, why))
        skipped.append(name)

    # 目标远端是 Linux / macOS，那里 `kill -0` + `/proc/<pid>/stat` 或
    # `ps -o lstart` 语义可靠。本机若在 MSYS/Cygwin 的 sh 里跑，PID 是它自己
    # 的一套编号（和 Windows 的 WINPID 不同）、/proc 还会被快速复用——
    # 这时"这个 pid 还是不是当初那个进程"根本无从判断，相关断言只能跳过。
    # 跳过不等于通过：Linux 上（CI/真机）这些断言必须真的跑。
    try:
        probe_uname = subprocess.run([sh, "-c", "uname -o 2>/dev/null || uname -s"],
                                     capture_output=True, text=True, timeout=10)
        shell_flavour = (probe_uname.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        shell_flavour = ""
    identity_ok = not any(k in shell_flavour.lower() for k in ("msys", "cygwin"))

    print("=" * 70)
    print("远程脚本冒烟（本地 sh 扮演远端）")
    print("sh   :", sh)
    print("HOME :", home)
    print("=" * 70)

    # --- 1. 全新环境：应该没有实例 ---
    rc, out, err = run_script(sh, remote.probe_script(), home, env)
    info = remote.parse_tagged(out)
    check("首次探测：拿到远端 HOME", bool(info.get("home")), info.get("home", ""))
    check("首次探测：没有已运行的实例", "alive" not in info,
          "rc=%s %s" % (rc, err.strip()[:60]))

    # --- 2. 启动 ---
    t0 = time.time()
    rc, out, err = run_script(sh, remote.start_script(cmd, 120), home, env,
                              timeout=180)
    cost = time.time() - t0
    info = remote.parse_tagged(out)
    port, token = remote._url_of(info.get("url") or "")
    check("启动脚本拿到带令牌的地址", bool(port and token),
          "端口 %s / 令牌 %s 位" % (port, len(token)))
    check("启动脚本报告了 pid", bool(info.get("started")), info.get("started", ""))
    check("启动耗时在合理区间", 0 < cost < 180, "%.1f s" % cost)

    # 状态目录里的四个文件
    state = os.path.join(home, ".dsh-remote")
    for name in ("web.pid", "web.log", "web.cmd", "web.st"):
        check("状态文件已落盘：%s" % name,
              os.path.exists(os.path.join(state, name)))
    try:
        with open(os.path.join(state, "web.cmd"), encoding="utf-8") as f:
            saved = f.read()
        check("web.cmd 记录的是裸命令（不含 shell 引号）",
              saved == cmd, saved[:50])
    except OSError as e:
        check("web.cmd 记录的是裸命令（不含 shell 引号）", False, str(e))

    # --- 3. 再探测：应识别为可复用 ---
    rc, out, err = run_script(sh, remote.probe_script(), home, env)
    info = remote.parse_tagged(out)
    if identity_ok:
        check("二次探测：识别出存活实例", bool(info.get("alive")), info.get("alive", ""))
        check("二次探测：启动命令一致", (info.get("cmd") or "").strip() == cmd)
        p2, t2 = remote._url_of(info.get("url") or "")
        check("二次探测：地址与启动时一致", p2 == port and t2 == token,
              "%s/%d" % (p2, len(t2)))
    else:
        why = "本机 sh 是 %s，PID 语义与目标远端不同" % (shell_flavour or "非 POSIX")
        skip("二次探测：识别出存活实例", why)
        skip("二次探测：启动命令一致", why)
        skip("二次探测：地址与启动时一致", why)

    # --- 4. 停止 ---
    rc, out, err = run_script(sh, remote.stop_script(), home, env, timeout=40)
    info = remote.parse_tagged(out)
    if "stopped" in info:
        check("停止脚本报告已停止", True)
    elif "notours" in info and not identity_ok:
        # 认不出那个 pid 属于我们，于是拒绝动手——这正是脚本该有的保守行为：
        # 宁可不停，也不在别人的机器上杀别人的进程。
        skip("停止脚本报告已停止", "认不出 pid 属于我们，脚本按设计不动手")
    else:
        check("停止脚本报告已停止", False, err.strip()[:60] or str(info))

    time.sleep(0.6)
    rc, out, err = run_script(sh, remote.probe_script(), home, env)
    info = remote.parse_tagged(out)
    if identity_ok:
        check("停止后不再有存活实例", "alive" not in info)
    else:
        skip("停止后不再有存活实例", "同上：本机 sh 认不出进程身份")

    # --- 5. 清场：万一没停掉，别把 node 留在后台 ---
    # 用 sh 的 kill（MSYS 的 PID 只有它自己认），而不是 Windows 的 taskkill。
    try:
        with open(os.path.join(state, "web.pid"), encoding="utf-8") as f:
            pid = (f.read() or "").strip()
        if pid:
            subprocess.run([sh, "-c", "kill -9 %s 2>/dev/null" % int(pid)],
                           capture_output=True, text=True)
    except (OSError, ValueError):
        pass

    shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 70)
    if failures:
        print("失败 %d 项：%s" % (len(failures), "、".join(failures)))
        return 1
    if skipped:
        print("通过 %d 项，跳过 %d 项（本机 sh 不支持进程身份判定；"
              "这些断言在 Linux 远端上必须真的跑）" % (passed, len(skipped)))
    else:
        print("全部 %d 项通过" % passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
