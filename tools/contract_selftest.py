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
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 输出强制 UTF-8。默认 stdout 编码跟随系统区域设置——在 cp1252 的机器上
# （CI 的 windows runner 就是这样）打印中文会直接 UnicodeEncodeError 崩掉，
# 而且报错位置看起来像是脚本本身有问题，实际只是控制台编码。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

TMP = os.path.join(ROOT, ".tmp-selftest")
KEEP = "--keep" in sys.argv
#: CI 里没有装 DSH，跑不了真实链路。设此变量则只跑不依赖 DSH 的部分。
#: 显式开关而不是"检测不到就跳过"——后者会让本机漏跑变成静默通过。
LITE = os.environ.get("DSH_SELFTEST_LITE") == "1"
os.environ["DSH_HOME"] = TMP
os.makedirs(TMP, exist_ok=True)

from dsh_host import config, contract, updater   # noqa: E402

LINE = "-" * 64
failures: list[str] = []


def check(name: str, ok: bool, extra: str = ""):
    print(f"    [{'OK  ' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")
    if not ok:
        failures.append(name)


def channel_logic_checks():
    """通道判定的**纯逻辑**回归（不需要装 DSH，所以精简模式也跑）。

    守卫的是 0.1.7-rc.1/rc.2 漏检事故：官方同时维护 latest / next / alpha
    三条线，而旧实现只读 latest + alpha，把 `next` 当"暂不暴露"，
    导致整条 RC 线在界面上不可见——已装 0.1.7-alpha.2 的用户被告知"已是最新"。

    **全部用固定 tag 组合，不依赖当下官方发了什么**，否则上游一发新版
    这些断言就跟着漂移，等于没守。
    """
    # A：next（RC）比 alpha 新 —— 旧实现会漏掉 RC
    print("    离线回归 A：next（RC）比 alpha 新——旧实现会漏掉 RC")
    A = {"latest": "0.1.5-rc.3", "next": "0.1.7-rc.2", "alpha": "0.1.7-alpha.2"}
    old = SimpleNamespace(version="0.1.5-rc.3")
    off, on = updater.decide(old, A, False), updater.decide(old, A, True)
    check("预览计划关闭时只跟稳定版", off.target == "0.1.5-rc.3", off.target)
    check("预览计划开启时取到最高版本", on.target == "0.1.7-rc.2", on.target)
    check("命中 RC 时通道标为 next", on.channel == "next", on.channel)
    check("三条通道版本都记进 UpdateInfo",
          (on.stable, on.next, on.alpha) == ("0.1.5-rc.3", "0.1.7-rc.2", "0.1.7-alpha.2"))

    # B：预览线反而落后时，不能把"开启预览"变成"被降级"
    print("    离线回归 B：预览线落后于稳定线时不得降级")
    B = {"latest": "0.2.0-rc.1", "next": "0.1.7-rc.2", "alpha": "0.1.7-alpha.2"}
    check("预览线落后时仍跟随稳定版",
          updater.decide(old, B, True).target == "0.2.0-rc.1")

    # C：tag 缺失 / 不可解析不炸，也不把垃圾值当目标
    print("    离线回归 C：tag 缺失 / 不可解析不炸")
    for name, C in (("只有 latest", {"latest": "0.1.5-rc.3"}),
                    ("latest 是垃圾值", {"latest": "garbage", "next": "0.1.7-rc.2"}),
                    ("next 是垃圾值", {"latest": "0.1.5-rc.3", "next": "junk",
                                       "alpha": "0.1.7-alpha.2"}),
                    ("dist-tags 为空", {})):
        try:
            got = updater.decide(old, C, True).target
            check(f"容错：{name}", bool(got) and got not in ("garbage", "junk"), got)
        except Exception as e:                           # noqa: BLE001
            check(f"容错：{name}", False, f"{type(e).__name__}: {e}")

    # D：跟"稳定版"的路径都必须报出预览通道有没有更高版本
    print("    离线回归 D：跟稳定版时仍须报出预览通道有更新的")
    T = {"latest": "0.1.5-rc.3", "next": "0.1.7-rc.2", "alpha": "0.1.7-alpha.2"}
    D = updater.decide(old, T, False)
    check("已是最新时能指出预览通道有 0.1.7-rc.2",
          D.action == "none" and D.newer_preview() == "0.1.7-rc.2",
          f"action={D.action} newer={D.newer_preview()}")
    # 真实情形：已装 0.1.7-alpha.2 > latest，关闭预览计划时动作是"回归稳定版"。
    # 这种提示同样不能丢——否则用户只看到"降级"，不知道还有更新的 RC。
    D3 = updater.decide(SimpleNamespace(version="0.1.7-alpha.2"), T, False)
    check("提供回归稳定版时也报出预览通道有 0.1.7-rc.2",
          D3.action == "downgrade" and D3.newer_preview() == "0.1.7-rc.2",
          f"action={D3.action} newer={D3.newer_preview()}")
    check("目标是预览版时 newer_preview 仍返回它（界面靠 channel 判重）",
          updater.decide(SimpleNamespace(version="0.1.7-alpha.2"), T, True).newer_preview()
          == "0.1.7-rc.2")
    check("已装最新 RC 时不再误报预览通道有更新",
          updater.decide(SimpleNamespace(version="0.1.7-rc.2"), T, False).newer_preview() is None)


def main() -> int:
    if LITE:
        return main_lite()
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
        check("关闭预览计划时不指向预览 tag",
              updater.decide(inst, tags, False).channel == "latest")
    except Exception as e:                               # noqa: BLE001
        check("通道查询", False, str(e)[:120])

    # 通道判定的纯逻辑回归——抽成函数是为了让 CI 的精简模式也跑到它
    channel_logic_checks()

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


def main_lite() -> int:
    """不依赖 DSH 安装的部分（CI 用）。

    检查的是"纯逻辑"：版本代数比较、路径反推、指纹判断、URL 解析规则。
    这些是契约层里最容易写错、且错了最不容易发现的部分，
    而且不需要真的装一个 DSH 就能验证。
    """
    from dsh_host import compat
    from dsh_host.version import HOST_VERSION, is_newer, version_tuple

    print(LINE)
    print("[L1] 版本代数比较（DSH 全是预发布，不能用严格 semver 序）")
    cases = [
        ("0.1.6-alpha.2", ">=0.1.6", True, "预发布也应满足不带上限的区间"),
        ("0.1.5-rc.2", ">=0.1.6", False, "低代数不满足"),
        ("0.1.5-rc.2", ">=0.1.5", True, "同代数满足"),
        ("0.2.0", ">=0.1.6", True, "高代数满足"),
        ("0.1.6-alpha.2", ">=0.1.6-alpha.1", True, "右侧带预发布则退化到严格序"),
        ("0.1.6-alpha.1", ">=0.1.6-alpha.2", False, "严格序下更低"),
    ]
    for ver, spec, expect, why in cases:
        check(f"satisfies({ver}, {spec}) == {expect}", 
              compat.satisfies(ver, spec) == expect, why)

    print(LINE)
    print("[L1] 版本解析与预发布识别")
    check("parse_version 三段", compat.parse_version("0.1.6-alpha.2")[:3] == (0, 1, 6))
    check("is_prerelease 认 alpha", compat.is_prerelease("0.1.6-alpha.2"))
    check("is_prerelease 不认正式版", not compat.is_prerelease("0.1.6"))

    print(LINE)
    print("[L1] npm prefix 反推（算错会把包装进 node_modules/node_modules/）")
    # npm 全局布局深度是固定的：<prefix>\node_modules\<scope>\<pkg>
    # 所以 prefix_of 就是往上三级。给个非 D 盘的路径确认它没写死盘符。
    pf = contract.prefix_of(r"C:\nodejs\node_modules\@deepseek-ai\dsh")
    check("三级反推正确", os.path.normcase(pf) == os.path.normcase(r"C:\nodejs"), pf)
    pf2 = contract.prefix_of(r"D:\a\b\node_modules\@scope\pkg")
    check("不同盘符与深度也正确",
          os.path.normcase(pf2) == os.path.normcase(r"D:\a\b"), pf2)

    print(LINE)
    print("[L0] 适配 profile 四层合并")
    prof = compat.load_profile("0.1.6-alpha.2")
    need = ["cli", "url_regexes", "ready_probe", "ready_timeout",
            "poll_interval", "url_grace", "service_host"]
    missing = [k for k in need if k not in prof]
    check("profile 含必要键", not missing, "缺：" + str(missing) if missing else "")
    check("url_grace 足够补读 URL（时序陷阱的兜底）",
          float(prof.get("url_grace") or 0) >= 1.0, str(prof.get("url_grace")))

    print(LINE)
    print("[L2] URL 解析规则（拿不到令牌会首屏 401）")
    import re
    sample = "dsh web: http://127.0.0.1:9716/?token=AbC123xyz"
    url = None
    for pat in prof.get("url_regexes") or []:
        m = re.search(pat, sample)
        if m:
            url = m.group(0)
            break
    check("能从官方输出行解析出 URL", bool(url and "token=" in url), str(url))

    print(LINE)
    print("[L2] 数据指纹判断逻辑")
    fp = contract.HomeFingerprint(path=r"C:\fake", files=10, bytes=1000)
    check("自比不误报减少", not fp.lost_against(fp))
    check("能识别减少", fp.lost_against(
        contract.HomeFingerprint(path=fp.path, files=11, bytes=1001)))
    check("截断时不做判断（不把'没查'伪装成'查过'）",
          not fp.lost_against(contract.HomeFingerprint(
              path=fp.path, files=0, bytes=0, truncated=True)))

    print(LINE)
    print("[L2] 通道判定（三条 dist-tag 线；只读 latest+alpha 会漏掉整条 RC 线）")
    channel_logic_checks()

    print(LINE)
    print("[L0] 路径规划（数据根与解包目录必须分家）")
    check("解包目录名与数据根不同",
          config.APP_NAME != config.RUNTIME_DIR_NAME,
          f"{config.APP_NAME} / {config.RUNTIME_DIR_NAME}")
    check("解包目录可随时删（是纯缓存）",
          "runtime" in config.runtime_dir().lower(), config.runtime_dir())
    check("私有 npm prefix 在数据根下",
          os.path.normcase(config.node_global_dir()).startswith(
              os.path.normcase(config.data_root())), config.node_global_dir())

    print(LINE)
    print("[Host] 桌面壳版本号")
    check("版本号可解析为三段", len(version_tuple(HOST_VERSION)) >= 3, HOST_VERSION)
    check("同版本不比自身新", not is_newer(HOST_VERSION, HOST_VERSION))

    print(LINE)
    if failures:
        print("失败项：", "、".join(failures))
        return 1
    print("契约层自测（精简模式）全部通过")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        if not KEEP:
            shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
