# -*- coding: utf-8 -*-
"""契约层自测（回归工具）。

用途：每次动 dsh_host/ 之后跑一遍，确认"定位 → 启动 → 就绪 → 拿 URL → 停止"
这条链路没断。DSH 每次更新后也建议跑一次，用来发现破坏性变更落在哪一环。

    python tools/contract_selftest.py

默认用临时 DSH_HOME（.tmp-selftest），不会碰你正在用的 D:\\AppData\\dsh。
加 --keep 可保留临时目录便于查看日志。
"""
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = os.path.join(ROOT, ".tmp-selftest")
KEEP = "--keep" in sys.argv
os.environ["DSH_HOME"] = TMP
os.makedirs(TMP, exist_ok=True)

from dsh_host import config, contract, updater   # noqa: E402

LINE = "-" * 64
failures: list[str] = []


def check(name: str, ok: bool, extra: str = ""):
    print(f"    [{'OK  ' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")
    if not ok:
        failures.append(name)


def main() -> int:
    print(LINE)
    print("[1] 安装定位")
    try:
        inst = contract.resolve_install()
    except contract.DshError as e:
        print("    定位失败：", e)
        return 1
    print("    describe :", inst.describe())
    print("    node     :", inst.node)
    print("    bin_js   :", inst.bin_js)
    print("    pkg_root :", inst.pkg_root)
    print("    prefix   :", inst.prefix)
    print("    npm_cli  :", inst.npm_cli)

    # 关键回归断言：prefix 必须使得 <prefix>/node_modules/@deepseek-ai/dsh == pkg_root。
    # 层级数错一位时，--prefix 参数会把新版本装进 node_modules/node_modules/，
    # 而版本校验因为查的是原位置反而"通过"——属于会静默炸掉的类型。
    rebuilt = os.path.join(inst.prefix, contract.PKG_REL)
    check("prefix 层级正确", os.path.normcase(rebuilt) == os.path.normcase(inst.pkg_root),
          f"(反推 {rebuilt})")
    check("prefix 目录存在", os.path.isdir(inst.prefix))
    check("版本号可解析", contract.compat.parse_version(inst.version) is not None,
          inst.version)
    check("npm 入口可用", bool(inst.npm_cli))

    print(LINE)
    print("[2] DSH_HOME 与适配 profile")
    home = contract.resolve_home()
    print("    DSH_HOME :", home)
    check("DSH_HOME 命中环境变量", home == os.path.normpath(TMP) or home == TMP, home or "")
    check("profile 含必要键", all(k in inst.profile for k in
                                  ("cli", "url_regexes", "ready_timeout",
                                   "home_env", "stale_locks")))

    print(LINE)
    print("[3] 官方通道查询")
    try:
        tags = updater.fetch_dist_tags(inst)
        print("    dist-tags :", tags)
        check("latest 通道可读", bool(tags.get("latest")))
        for opt in (False, True):
            info = updater.decide(inst, tags, opt)
            print(f"    预览计划={'开' if opt else '关'} → {info.summary()}")
        check("关闭预览计划时不指向 alpha",
              updater.decide(inst, tags, False).channel == "latest")
    except Exception as e:                               # noqa: BLE001
        check("通道查询", False, str(e)[:120])

    print(LINE)
    print("[4] 启动 / 就绪 / URL 发现（--port 0 系统分配）")
    logf = os.path.join(TMP, "svc.log")
    handle = contract.launch(inst, 0, logf, cwd=TMP)
    print("    pid :", handle.pid)
    try:
        t0 = time.time()
        contract.wait_ready(handle, timeout=150)
        print("    就绪用时 : %.1fs" % (time.time() - t0))
        print("    端口     :", handle.port)
        print("    URL      :", handle.url)
        check("端口由系统分配（非固定 3080）", handle.port not in (0, 3080), str(handle.port))
        check("URL 可从 stdout 解析", handle.url.startswith("http://"), handle.url[:60])
        check("令牌已取到", bool(handle.token), handle.token[:8] + "…" if handle.token else "")
        # 回归断言：TCP 一连通就返回会导致"就绪但 URL 为空"，首屏变 401 页。
        # 就绪后的补读（url_grace）必须保证 URL 带令牌。
        check("URL 带令牌（就绪后补读生效）", "token=" in handle.url,
              handle.url[:70] if handle.url else "(URL 为空)")

        print(LINE)
        print("[5] 就绪后指纹校验")
        ok, status, body = contract.probe_http(handle.url, 5)
        print("    probe :", ok, status, "|", body[:50].replace("\n", " "))
        check("looks_like_dsh 能识别自家服务", contract.looks_like_dsh(handle.url))
        contract.save_state(handle)
        check("运行时状态可落盘", (contract.load_state() or {}).get("pid") == handle.pid)
    finally:
        print(LINE)
        print("[6] 停止服务（PID 优先，端口反查兜底）")
        n = contract.stop(handle, port=handle.port)
        time.sleep(2)
        print("    终止进程数 :", n)
        check("进程已停止", n >= 1)
        check("端口已释放", not contract.tcp_open("127.0.0.1", handle.port))
        contract.save_state(None)

    print(LINE)
    print("[7] 环境自检与探测（首次运行引导依赖）")
    rep = contract.diagnose()
    for r in rep.rows():
        print(f"    [{'OK  ' if r['ok'] else 'MISS'}] {r['name']}: {r['text']}")
    check("diagnose 返回完整三行状态", len(rep.rows()) == 3)
    check("diagnose 与 resolve 结论一致", rep.ready == (rep.install is not None))

    # 回归：node 与 bin.js 分离的布局必须能配对。
    # 私有 prefix（首次运行引导装的那种）没有 node.exe，旧实现要求"就近找到 node"，
    # 导致整条候选被**静默跳过**——表现为设了 DSH_BIN 却用了另一个安装。
    check("node 与 bin.js 分离时仍能配对", bool(contract._node_for(inst.bin_js)),
          contract._node_for(inst.bin_js) or "")

    # 回归：显式覆盖必须绕过缓存（用户指定意图不能被上次结果顶掉）
    os.environ["DSH_BIN"] = inst.bin_js
    try:
        rep2 = contract.diagnose()
        src = rep2.install.source if rep2.install else "未解析"
        check("DSH_BIN 覆盖生效且绕过缓存", rep2.ready and src != "缓存", src)
    finally:
        os.environ.pop("DSH_BIN", None)

    # 私有 prefix 必须位于候选序列并优先命中，否则引导装完自己都找不到。
    # 借用现有安装冒充私有 prefix 来验证这条路径真实可达（而不是只打印一遍候选）。
    orig_ngd = config.node_global_dir
    try:
        config.node_global_dir = lambda: inst.prefix
        srcs = [s for s, _b, _n in contract.candidate_bin_js()]
        check("私有 prefix 命中时排在首位", bool(srcs) and srcs[0] == "私有 prefix",
              "候选顺序：" + ("、".join(srcs) or "无"))
    finally:
        config.node_global_dir = orig_ngd

    # 数据指纹：更新前后各取一次，确认"程序换了、用户数据一个没少"。
    # 这里验证判断逻辑本身——自比不误报、真减少能认出来、截断时不假装判断过。
    fp = contract.home_fingerprint()
    check("数据指纹可统计", bool(fp.path) and fp.files >= 0, fp.describe())
    check("指纹自比不误报减少", not fp.lost_against(fp))
    check("指纹能识别减少", fp.lost_against(contract.HomeFingerprint(
        path=fp.path, files=fp.files + 1, bytes=fp.bytes + 1)))
    check("截断时不判断（不把'没查'伪装成'查过'）",
          not fp.lost_against(contract.HomeFingerprint(
              path=fp.path, files=0, bytes=0, truncated=True)))

    print(LINE)
    if failures:
        print("失败项：", "、".join(failures))
        return 1
    print("契约层自测全部通过")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        if not KEEP:
            shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
