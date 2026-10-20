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

    # E：已装 RC 高于 alpha 时，"已是最新"必须能解释为什么 alpha 不升。
    #    2026-10-19 老大实测：已装 0.2.1-rc.2，看到尝鲜通道 0.2.1-alpha.5
    #    却"检查不到更新"，以为漏检。预览计划取最高预览版，不自动降级。
    E = {"latest": "0.2.0-rc.2", "next": "0.2.1-rc.2", "alpha": "0.2.1-alpha.5"}
    Einfo = updater.decide(SimpleNamespace(version="0.2.1-rc.2"), E, True)
    check("已装 RC 时能指出 alpha 低于当前（界面解释用）",
          Einfo.action == "none"
          and Einfo.preview_below_current() == [("尝鲜", "0.2.1-alpha.5")],
          f"action={Einfo.action} lower={Einfo.preview_below_current()}")
    check("action_for 区分升级 / 降级 / 不动",
          (updater.action_for("0.2.1-rc.2", "0.2.1-alpha.4") == "downgrade"
           and updater.action_for("0.2.1-alpha.4", "0.2.1-rc.2") == "upgrade"
           and updater.action_for("0.2.1-rc.2", "0.2.1-rc.2") == "none"))
    check("已装 alpha 本身时不被列为「低于当前」",
          updater.decide(SimpleNamespace(version="0.2.1-alpha.5"), E, True)
          .preview_below_current() == [])


def install_cache_checks():
    """安装缓存与"版本复验"快路径的纯逻辑回归（不需要装 DSH）。

    守卫的是 v2.1.6 的改动：启动路径不再为 `dsh --version` 白等 400ms，
    改为采信缓存里的版本号，复验挪到后台。代价是这个快路径**必须**满足两条：
      1. 缓存里确实存了版本号（否则快路径会拿到空版本）
      2. 早期缓存（没写 version 字段）必须退回 spawn，不能返回空版本

    做法是把 `_cache_path()` 临时指到临时文件上——**绝不能碰用户真实的
    `%LOCALAPPDATA%\\DSH-Web\\install.json`**。
    """
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="dsh-cache-check-")
    fake_cache = os.path.join(tmpdir, "install.json")
    # _load_cache 要求 node / bin_js 都真实存在，拿当前解释器与一个临时文件顶上
    fake_bin = os.path.join(tmpdir, "bin.js")
    open(fake_bin, "w", encoding="utf-8").close()
    node = sys.executable

    orig = contract._cache_path
    contract._cache_path = lambda: fake_cache          # type: ignore[assignment]
    try:
        import json
        with open(fake_cache, "w", encoding="utf-8") as f:
            json.dump({"node": node, "bin_js": fake_bin,
                       "version": "9.9.9-test", "source": "自测"}, f)
        got = contract._load_cache()
        check("缓存里的版本号被读出来", bool(got) and got[2] == "9.9.9-test",
              str(got[2] if got else None))
        check("缓存里的来源也被读出来", bool(got) and got[3] == "自测")

        # 模拟早期缓存：没有 version 字段
        with open(fake_cache, "w", encoding="utf-8") as f:
            json.dump({"node": node, "bin_js": fake_bin}, f)
        got2 = contract._load_cache()
        check("旧缓存缺 version 时返回空串（而不是报错或 None）",
              bool(got2) and got2[2] == "", repr(got2[2] if got2 else None))
        check("旧缓存仍能给出 node/bin_js（不会退化成'未安装'）",
              bool(got2) and got2[0] == node and got2[1] == fake_bin)

        # 路径不存在 → None
        contract._cache_path = lambda: os.path.join(tmpdir, "nope.json")
        check("缓存文件不存在时返回 None", contract._load_cache() is None)
    finally:
        contract._cache_path = orig                    # type: ignore[assignment]
        shutil.rmtree(tmpdir, ignore_errors=True)


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
    print("[3b] 安装缓存与版本复验快路径")
    install_cache_checks()

    print(LINE)
    print("[4] 启动 / 就绪 / URL 发现（--port 0 系统分配）")
    logf = os.path.join(TMP, "svc.log")
    handle = contract.launch(inst, 0, logf, cwd=TMP)
    print("    pid :", handle.pid)
    # 这一段会覆盖**用户正在用的** service.json（同一路径）。以前跑完就把它
    # 清成 None，于是正在运行的服务丢了状态：launcher 下次启动时要重新猜端口
    # 和令牌，甚至可以起来了却连不上（2026-10-20 现场就被这么坑过）。
    # 所以先备份，结束时原样恢复。
    state_file = config.service_state_path()
    prev_state = None
    if os.path.isfile(state_file):
        with open(state_file, "rb") as fh:
            prev_state = fh.read()
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
        if prev_state is not None:
            try:
                with open(state_file, "wb") as fh:
                    fh.write(prev_state)
                print("    已恢复原有的 service.json（不干扰正在运行的实例）")
            except OSError as e:
                print("    恢复 service.json 失败：", e)
        else:
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
    # 注意标签：v2.3.7 把来源名从"私有 prefix"改成了"私有目录"（用户看不懂 prefix），
    # 断言当时没跟着改，于是长期红着——不是产品问题，是断言过时（2026-10-20 修）。
    orig_ngd = config.node_global_dir
    try:
        config.node_global_dir = lambda: inst.prefix
        srcs = [s for s, _b, _n in contract.candidate_bin_js()]
        check("私有 prefix 命中时排在首位", bool(srcs) and srcs[0] == "私有目录",
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
    print("[L3] 自更新下载（分段并行 / 退回单连接 / 断流续传 / 校验）")
    selfupdate_download_checks()

    print(LINE)
    if failures:
        print("失败项：", "、".join(failures))
        return 1
    print("契约层自测全部通过")
    return 0


def selfupdate_download_checks():
    """自更新下载：分段并行、无 Range 退回、断流续传、体积与摘要校验。

    为什么专门造一个本地服务端：这条路径出错的表现是「更新装上了，但装进去
    的程序是坏的」——最坏的一种失败，而且用户看不到任何报错。真网络上
    碰运气覆盖不到「服务端不支持 Range」这种分支，所以把服务端行为造出来。

    背景见 dsh_host/selfupdate.py 的「为什么要分段并行」：GitHub 对单条连接
    实测只给 40 KB/s，100 MB 的包要 40 分钟；分 6 条连接实测 5.4 倍。
    """
    import hashlib
    import re as _re
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from dsh_host import selfupdate

    _saved_workers = os.environ.pop("DSH_DL_WORKERS", None)
    check("体积小于阈值时不开线程（建连本身要花钱）",
          selfupdate._worker_count(3 * 1024 * 1024) == 1)
    check("大体积才会开到多连接",
          selfupdate._worker_count(107 * 1024 * 1024) > 1,
          str(selfupdate._worker_count(107 * 1024 * 1024)))
    check("体积未知时不猜（交给单连接）", selfupdate._worker_count(0) == 1)
    spans = selfupdate._split(100, 3)
    check("分段首尾相接且恰好覆盖整个文件",
          [(s, e) for s, e in spans] == [(0, 33), (34, 67), (68, 99)], str(spans))
    check("段数多于字节数时不产生空段也不丢内容",
          all(e >= s for s, e in selfupdate._split(10, 8))
          and sum(e - s + 1 for s, e in selfupdate._split(10, 8)) == 10,
          str(selfupdate._split(10, 8)))
    if _saved_workers is not None:
        os.environ["DSH_DL_WORKERS"] = _saved_workers

    payload = b"MZ" + bytes(range(256)) * (6 * 1024 * 4)      # ≈6 MB
    total = len(payload)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"        # 一条连接一个请求，行为可预测
        ranges = True
        truncate_once = False
        seen: list = []
        truncated: set = set()

        def log_message(self, *args):        # 别往测试输出里掺访问日志
            pass

        def do_GET(self):
            rng = self.headers.get("Range") or ""
            Handler.seen.append(rng)
            if rng and Handler.ranges:
                m = _re.match(r"bytes=(\d+)-(\d+)", rng)
                start = int(m.group(1))
                end = min(int(m.group(2)), total - 1)
                body = payload[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, end, total))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                key = (start, end)
                # 只截断**整轮测试的第一个**请求：续传请求的区间是新的，
                # 如果也截断，就会一直"续传又断"，测不出补齐能力。
                if Handler.truncate_once and not Handler.truncated:
                    Handler.truncated.add(key)
                    self.wfile.write(body[:max(1, len(body) // 3)])
                    self.wfile.flush()
                    self.close_connection = True         # 模拟断流
                    return
                self.wfile.write(body)
                return
            self.send_response(200)                          # 忽略 Range，回整包
            self.send_header("Content-Length", str(total))
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/DSH-Web.exe" % srv.server_address[1]

    def make_rel(size=total, dig=digest):
        return selfupdate.HostRelease(
            version="9.9.9", tag="v9.9.9", notes="", html_url="",
            published_at="",
            asset=selfupdate.ReleaseAsset(name="DSH-Web.exe", url=url,
                                          size=size, digest=dig))

    def fetch(rel):
        path = selfupdate.download(rel)
        try:
            with open(path, "rb") as fh:
                return fh.read()
        finally:
            os.remove(path)

    def reset(ranges=True, truncate=False):
        Handler.ranges, Handler.truncate_once = ranges, truncate
        Handler.seen, Handler.truncated = [], set()

    os.environ["DSH_DL_WORKERS"] = "4"        # 6 MB 也强制分段，跑得快
    try:
        reset()
        check("分段下载拿到完整文件", fetch(make_rel()) == payload)
        check("每个请求都带了 Range（确实分了段）",
              len(Handler.seen) == 4 and all(Handler.seen), str(Handler.seen))

        # 服务端忽略 Range（老 release / 代理剥头）→ 必须安静退回单连接
        reset(ranges=False)
        check("服务端不支持 Range 时仍能下完", fetch(make_rel()) == payload)
        check("退回单连接只发一次不带 Range 的请求",
              Handler.seen.count("") == 1,
              "共 %d 次请求" % len(Handler.seen))

        # 断流：第一段只写三分之一就断，应带 Range 续传补齐
        reset(truncate=True)
        check("断流后能续传补齐", fetch(make_rel()) == payload)
        check("断流后确实重发了同一段",
              len(Handler.seen) > 4, "共 %d 次请求" % len(Handler.seen))

        reset()
        bad = "sha256:" + "0" * 64
        try:
            fetch(make_rel(dig=bad))
            check("摘要不符时必须失败", False, "下载被当成成功了")
        except selfupdate.SelfUpdateError as e:
            check("摘要不符时必须失败", "校验值" in str(e), str(e))

        # 服务端公布体积比实际大 → 体积校验拦下（不能让截断的 exe 过关）
        os.environ["DSH_DL_WORKERS"] = "1"
        reset()
        try:
            fetch(make_rel(size=total + 1000))
            check("体积不符时必须失败", False, "下载被当成成功了")
        except selfupdate.SelfUpdateError as e:
            check("体积不符时必须失败", "不完整" in str(e), str(e))
    finally:
        if _saved_workers is None:
            os.environ.pop("DSH_DL_WORKERS", None)
        else:
            os.environ["DSH_DL_WORKERS"] = _saved_workers
        srv.shutdown()
        srv.server_close()


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
    print("[L2] 从日志恢复地址（attach 时最容易拿错令牌的地方）")
    # 2026-10-20 线上事故：同一份 dsh-web.log 里堆着历次启动的地址行，
    # 端口相同、令牌不同。老实现取"第一个匹配"，于是 attach 用**上一次的旧令牌**
    # 打开首屏 → 401。这里造一份真实的日志形状，钉住"取最后一条 + 按端口过滤"。
    import tempfile
    import types
    fake_install = types.SimpleNamespace(profile={
        "service_host": "127.0.0.1",
        "url_regexes": prof.get("url_regexes") or [],
        "log_url_regexes": prof.get("log_url_regexes") or [],
        "log_token_regexes": prof.get("log_token_regexes") or [],
    })
    with tempfile.TemporaryDirectory() as tmp:
        logf = os.path.join(tmp, "dsh-web.log")
        with open(logf, "w", encoding="utf-8") as fh:
            fh.write("dsh web: http://127.0.0.1:3080/?token=OLDOLDOLD\n")
            fh.write("dsh web: http://127.0.0.1:4001/?token=OTHERPORT\n")
            fh.write("dsh web: http://127.0.0.1:3080/?token=NEWNEWNEW\n")
        got = contract.recover_url(fake_install, 3080, logf)
        check("同端口取最后一条（不用上次的旧令牌）",
              "token=NEWNEWNEW" in got, got)
        got2 = contract.recover_url(fake_install, 9999, logf)
        check("端口对不上时不硬塞旧令牌",
              "OLDOLDOLD" not in got2 and "NEWNEWNEW" not in got2, got2)

    print(LINE)
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
    print("[L0] 安装缓存（启动路径靠它跳过 400ms 的版本复验）")
    install_cache_checks()

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
    # 预发布版（`X.Y.Z-beta.N`）的排序：测试版必须低于同号正式版，
    # 序号必须按数值比 —— 用字符串比时 beta.10 会小于 beta.9，
    # 表现为"发了新 beta 却提示已是最新"。
    check("预发布版低于同号正式版",
          not is_newer("2.4.0-beta.1", "2.4.0"))
    check("预发布序号按数值比较（beta.10 > beta.9）",
          is_newer("2.4.0-beta.10", "2.4.0-beta.9"))

    # 可升级目标的判据：**不取预发布时，三条获取路径都不能返回 beta**。
    # 平时 latest 接口自己会跳过预发布，所以这条只在 API 撞限额时暴露。
    from dsh_host.selfupdate import HostRelease, accept_release
    from dsh_host.version import is_prerelease

    def _rel(version, prerelease=False):
        return HostRelease(version=version, tag="v" + version, notes="",
                           html_url="", published_at="", prerelease=prerelease)

    check("is_prerelease 与版本号判据一致",
          is_prerelease("2.4.0-beta.1") and not is_prerelease("2.4.0"))
    check("正式版可被当作更新", accept_release(_rel("2.4.0")))
    check("tag 带 beta 的 release 被拦下（未勾 prerelease 标记也一样）",
          not accept_release(_rel("2.4.0-beta.1")))
    check("勾了 prerelease 标记的 release 被拦下",
          not accept_release(_rel("2.4.0-beta.1", prerelease=True)))
    check("明确要求预发布时 beta 放行",
          accept_release(_rel("2.4.0-beta.1"), include_prerelease=True))

    print(LINE)
    print("[Host] 数据目录搬迁")
    # 2026-10-20：数据根可搬（C 盘紧张时），搬迁逻辑必须钉住——
    # 它删的是用户数据，错一次就没法交代。全程用临时 LOCALAPPDATA 隔离。
    import tempfile

    from dsh_host import storage

    _old_local = os.environ.get("LOCALAPPDATA")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCALAPPDATA"] = tmp
        config._root_cache = None
        try:
            root = config.data_root()
            os.makedirs(os.path.join(root, "logs"), exist_ok=True)
            with open(os.path.join(root, "logs", "a.log"), "w", encoding="utf-8") as f:
                f.write("x" * 200)
            f1, b1 = storage.tree_usage(root)
            check("能统计数据目录占用", f1 >= 1 and b1 >= 200,
                  "%d 个文件 / %d 字节" % (f1, b1))

            target = os.path.join(tmp, "moved")
            new_root = storage.migrate(target)
            check("迁移后指针指向新位置",
                  os.path.normcase(config.data_root())
                  == os.path.normcase(os.path.abspath(new_root)))
            f2, _b2 = storage.tree_usage(new_root)
            check("新位置文件数与原来一致", f2 == f1, "%d → %d" % (f1, f2))
            check("指针文件落在默认根",
                  os.path.isfile(os.path.join(tmp, config.APP_NAME, "location.json")))

            storage.remove_old(root)
            check("旧目录的数据已清除",
                  not os.path.isdir(os.path.join(root, "logs")))
            check("指针本身被保留",
                  os.path.isfile(os.path.join(tmp, config.APP_NAME, "location.json")))
        finally:
            if _old_local is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = _old_local
            config._root_cache = None


    # 2026-10-20 实测：chat.deepseek.com 直连 429、走代理 403。
    # DSH 只认环境变量里的代理，不读系统代理，于是"账号资料不可用"极难归因。
    from dsh_host import netenv

    _keys = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
             "http_proxy", "https_proxy", "no_proxy")
    _saved = {k: os.environ.get(k) for k in _keys}
    try:
        for k in _keys:
            os.environ.pop(k, None)
        check("没设代理时不打扰用户", netenv.proxy_hint() == "")
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"
        check("设了代理且没排除 DeepSeek 时给出提示", bool(netenv.proxy_hint()))
        os.environ["NO_PROXY"] = "chat.deepseek.com"
        check("NO_PROXY 覆盖后不再提示", netenv.proxy_hint() == "")
    finally:
        for k, v in _saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


    print(LINE)
    print("[Host] 安装完整性（半升级防护）")
    # 背景（2026-10-20）：`npm install -g` 可能只升主包，把精确版本的子依赖
    # 留在旧版。表现为"版本号正常，界面却报接口 404"——光看版本号查不出来。
    import inspect
    import json as _json
    import tempfile
    import types

    from dsh_host import updater

    check("安装默认先卸载（根除半升级）",
          inspect.signature(updater.install_version).parameters["fresh"].default is True)
    check("更新流程默认先卸载",
          inspect.signature(updater.update_to).parameters["fresh"].default is True)
    check("提供「重新安装」入口", callable(getattr(updater, "reinstall", None)))

    with tempfile.TemporaryDirectory() as tmp:
        pkg = os.path.join(tmp, "node_modules", "@deepseek-ai", "dsh")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "package.json"), "w", encoding="utf-8") as f:
            _json.dump({"version": "2.0.0", "dependencies": {
                "@deepseek-ai/a": "2.0.0",       # 精确，实际 1.0.0 → 应报
                "@deepseek-ai/b": "~1.2.3",      # 范围，跳过
                "@deepseek-ai/c": "2.0.0",       # 精确，缺失 → 应报
            }}, f)
        for name, ver in (("@deepseek-ai/a", "1.0.0"), ("@deepseek-ai/b", "1.2.9")):
            d = os.path.join(tmp, "node_modules", *name.split("/"))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "package.json"), "w", encoding="utf-8") as f:
                _json.dump({"version": ver}, f)
        fake = types.SimpleNamespace(prefix=tmp, version="2.0.0")
        drift = updater.check_dependency_consistency(fake)
        names = sorted(n for n, _w, _g in drift)
        check("能识别「主包新、子依赖旧」的半升级",
              names == ["@deepseek-ai/a", "@deepseek-ai/c"], str(names))
        check("范围型依赖不误报", "@deepseek-ai/b" not in names)


    print(LINE)
    print("[L3] 自更新下载（分段并行 / 退回单连接 / 断流续传 / 校验）")
    selfupdate_download_checks()

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
