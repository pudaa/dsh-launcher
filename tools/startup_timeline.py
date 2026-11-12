# -*- coding: utf-8 -*-
"""DSH 服务启动的分阶段时间线探针。

存在的理由
----------
`startup_profile.py` 只给出"launch → 就绪"的总时长，无法回答"这 11 秒
里 DSH 在干什么"。要判断可优化的空间，必须拿到**每一行 stdout 的到达时刻**
——DSH 的启动日志是顺序输出的，行间空档就是该阶段的耗时。

    python tools/startup_timeline.py                 # 空临时 DSH_HOME（纯净基线）
    python tools/startup_timeline.py --profile real  # 借用真实 profile 的插件集
    python tools/startup_timeline.py --keep          # 保留临时 DSH_HOME 便于复查

安全边界
--------
* `--profile real` **不写用户的 DSH_HOME**：临时目录里只放
  `profiles/web/{package.json,cordis.yml,cordis.patch.yml,pnpm-*.yaml}`，
  `node_modules` 用 junction 指向真实目录（插件是只读加载的）。
* 启动参数固定 `--port 0`，不占用用户正在使用的端口。
* 只读 stdout，不读 DSH 私有日志。
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

from dsh_host import contract  # noqa: E402

REAL_PROFILE = "--profile" in sys.argv and "real" in sys.argv
KEEP = "--keep" in sys.argv
# `--compile-cache` 打开 V8 的模块编译缓存（node 22+ 的 NODE_COMPILE_CACHE），
# 用来验证"冷启动的模块编译能否被磁盘缓存复用"。
COMPILE_CACHE = "--compile-cache" in sys.argv
# `--drop <name,name>` 从 profile 的 bundles 里摘掉指定插件后启动，
# 用来量化"每个非必需插件值多少秒"。
DROP = ""
for i, a in enumerate(sys.argv):
    if a == "--drop" and i + 1 < len(sys.argv):
        DROP = sys.argv[i + 1]
# `--rounds N` 复用同一个临时 HOME 连跑 N 次，取中位数。
# 单次测量会被后台任务、杀软首次扫描等偶然因素带偏，对照实验必须取稳定值。
ROUNDS = 1
for i, a in enumerate(sys.argv):
    if a == "--rounds" and i + 1 < len(sys.argv):
        ROUNDS = max(1, int(sys.argv[i + 1]))
# `--no-rebuild` 复用已存在的临时 HOME（有 junction 时重复 mklink 会失败）
NO_REBUILD = "--no-rebuild" in sys.argv
HOME = os.path.join(ROOT, ".tmp-boot-timeline")
REAL_HOME = os.environ.get("REAL_DSH_HOME") or r"D:\AppData\dsh"


def build_home() -> None:
    """准备探针用的 DSH_HOME。"""
    if os.path.isdir(HOME):
        shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME, exist_ok=True)
    if not REAL_PROFILE:
        return

    src = os.path.join(REAL_HOME, "profiles", "web")
    if not os.path.isdir(src):
        raise SystemExit("找不到真实 profile：%s" % src)
    dst = os.path.join(HOME, "profiles", "web")
    os.makedirs(dst, exist_ok=True)
    for name in ("package.json", "cordis.yml", "cordis.patch.yml",
                 "pnpm-lock.yaml", "pnpm-workspace.yaml"):
        s = os.path.join(src, name)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, name))
    if DROP:
        _drop_bundles(os.path.join(dst, "package.json"), DROP)
    # 插件目录只读借用：junction 不复制 283 MB，也不改用户文件
    link = os.path.join(dst, "node_modules")
    subprocess.run(["cmd", "/c", "mklink", "/J", link,
                    os.path.join(src, "node_modules")],
                   capture_output=True, text=True)


def _drop_bundles(pkg_path: str, names: str) -> None:
    """从副本的 package.json 里摘掉指定 bundle（只改临时副本）。"""
    import json
    drop = {n.strip() for n in names.split(",") if n.strip()}
    with open(pkg_path, "r", encoding="utf-8") as f:
        pkg = json.load(f)
    bundles = pkg.get("dsh", {}).get("profile", {}).get("bundles", [])
    kept = [b for b in bundles if b not in drop]
    pkg["dsh"]["profile"]["bundles"] = kept
    with open(pkg_path, "w", encoding="utf-8") as f:
        json.dump(pkg, f, ensure_ascii=False, indent=2)
    print("已摘掉 bundle：%s" % ", ".join(sorted(drop & set(bundles)) or ["（无匹配）"]))


def main() -> int:
    inst = contract.resolve_install(verify=False)
    print("=" * 74)
    print("DSH 启动时间线探针")
    print("DSH  :", inst.describe())
    print("HOME :", HOME, "（真实插件集）" if REAL_PROFILE else "（纯净基线）")
    print("=" * 74)

    if NO_REBUILD and os.path.isdir(HOME):
        print("复用已存在的临时 HOME：", HOME)
    else:
        build_home()

    times: list[float] = []
    for r in range(1, ROUNDS + 1):
        ready_at = _one_round(inst, verbose=(ROUNDS == 1), tag="第 %d" % r if ROUNDS > 1 else "")
        if ready_at is None:
            return 1
        times.append(ready_at)

    if ROUNDS > 1:
        ordered = sorted(times)
        mid = ordered[len(ordered) // 2] if len(ordered) % 2 else \
            (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
        print("-" * 74)
        print("各轮：%s" % " / ".join("%.2fs" % t for t in times))
        print("中位数：%.2f s   （首轮 %.2f s）" % (mid, times[0]))
    return 0


def _one_round(inst, verbose: bool, tag: str) -> float | None:
    """跑一轮：spawn 到拿到带令牌地址。返回耗时。"""
    env = os.environ.copy()
    env["DSH_HOME"] = HOME
    env["NO_COLOR"] = "1"
    if COMPILE_CACHE:
        cache = os.path.join(ROOT, ".tmp-node-compile-cache")
        os.makedirs(cache, exist_ok=True)
        env["NODE_COMPILE_CACHE"] = cache
        print("V8 编译缓存：", cache)
    args = [inst.node, inst.bin_js, "web", "--no-open", "--port", "0"]

    t0 = time.perf_counter()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, cwd=HOME,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)

    ready_at = None
    lines: list[tuple[float, str]] = []
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            dt = time.perf_counter() - t0
            line = raw.rstrip()
            lines.append((dt, line))
            if "dsh web: http" in line:
                ready_at = dt
                break
    finally:
        subprocess.run(["taskkill", "/F", "/PID", str(proc.pid), "/T"],
                       capture_output=True, text=True)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass

    if ready_at is None:
        print("%s未在观测窗口内拿到带令牌的地址（退出码 %s）" % (tag, proc.returncode))
        return None

    if not verbose:
        print("%s: %.2f s" % (tag or "单轮", ready_at))
        return ready_at

    # 只打印有信息量的行：空行丢弃
    prev = 0.0
    printed = 0
    for dt, line in lines:
        if not line.strip():
            continue
        gap = dt - prev
        prev = dt
        if printed > 400:
            break
        flag = "  <<< %.2fs 空档" % gap if gap >= 1.0 else ""
        print("%7.2fs (+%5.2fs)  %s%s" % (dt, gap, line[:150], flag))
        printed += 1

    print("-" * 74)
    print("总耗时：%.2f s" % ready_at)
    gaps = [(lines[i][0] - lines[i - 1][0], lines[i - 1][1][:60])
            for i in range(1, len(lines))]
    gaps.sort(reverse=True)
    print("最长的 6 段空档：")
    for g, after in gaps[:6]:
        if g < 0.3:
            break
        print("  %5.2fs  在「%s」之后" % (g, after))
    return ready_at


def _cleanup() -> None:
    """删临时 HOME。**必须先单独摘掉 junction**。

    这一步不容出错：`shutil.rmtree` 会跟随 junction 进入真实的
    `profiles/web/node_modules`，把用户的 156 个插件全删掉。所以先只用
    `rmdir`（不带 /s，对重解析点只删链接本身）摘链，摘不掉就**放弃清理**
    并提示路径——宁可留一个临时目录，也不能碰到真实目录。
    """
    link = os.path.join(HOME, "profiles", "web", "node_modules")
    if os.path.exists(link) or os.path.islink(link):
        subprocess.run(["cmd", "/c", "rmdir", link], capture_output=True, text=True)
        if os.path.exists(link):
            print("！临时目录里的 junction 摘不掉，已停止清理，请手动删除：%s" % HOME)
            return
    shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = main()
    finally:
        if not KEEP and os.path.isdir(HOME):
            _cleanup()
    sys.exit(code)
