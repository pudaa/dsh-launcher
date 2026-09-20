# -*- coding: utf-8 -*-
"""L0 契约层：唯一允许知道 DSH 内部结构的地方。

对外只暴露一组"能力"，内部实现遵循同一条铁律：
    每个能力都必须有【探测顺序】+【结果缓存】+【失败降级】

不允许出现"赌某个内部细节不变"的代码。DSH 是 developer preview，
官方 README 明确写着 THERE WILL BE COMPATIBILITY-BREAKING CHANGES。

能力清单
--------
    resolve_install()   定位 node + dsh 入口（多路探测）
    resolve_home()      定位 DSH_HOME
    read_version()      读取 DSH 版本
    launch()            启动 dsh web
    wait_ready()        等待就绪并拿到访问 URL
    recover_url()       附加到已在运行的服务时恢复 URL
    stop()              停止服务
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import config, compat

# Windows 进程创建标志
DETACHED = 0x00000008 | 0x00000200     # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
NO_WINDOW = 0x08000000                 # CREATE_NO_WINDOW

PKG_REL = os.path.join("node_modules", "@deepseek-ai", "dsh")
BIN_REL = os.path.join(PKG_REL, "lib", "bin.js")


class DshError(RuntimeError):
    """契约层对外统一异常，GUI 只需捕获这一种。"""


# ------------------------------------------------------------------ 数据结构

@dataclass
class DshInstall:
    node: str
    bin_js: str
    pkg_root: str
    prefix: str
    npm_cli: str | None
    version: str
    source: str
    profile: dict = field(default_factory=dict)

    def describe(self) -> str:
        return (f"DSH {self.version} · 运行环境 {os.path.basename(os.path.dirname(self.node))}"
                f" · 来源 {self.source}")


@dataclass
class ServiceHandle:
    install: DshInstall
    pid: int
    host: str
    requested_port: int
    port: int
    url: str
    token: str
    log_path: str
    started_at: float
    proc: subprocess.Popen | None = None
    _read_pos: int = 0


# ------------------------------------------------------------------ 进程工具

def _creationflags(detached: bool) -> int:
    return DETACHED if detached else NO_WINDOW


def run_capture(args: list[str], timeout: float = 60, env: dict | None = None):
    """执行命令并捕获输出，不弹控制台。返回 (returncode, stdout, stderr)。"""
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, errors="ignore",
            timeout=timeout, env=env, creationflags=NO_WINDOW,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"命令超时（{timeout}s）：{args[0]}"
    except OSError as e:
        return -1, "", str(e)


def _kill_pid(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    rc, _, _ = run_capture(["taskkill", "/F", "/PID", str(pid), "/T"], timeout=20)
    return rc == 0


def _image_of(pid: int) -> str:
    """取进程映像名。CSV 第二列是 exe 名，不受系统语言影响。"""
    rc, out, _ = run_capture(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=20)
    if rc != 0 or not out.strip():
        return ""
    first = out.strip().splitlines()[0]
    m = re.match(r'^"([^"]+)"', first)
    return (m.group(1) if m else "").lower()


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    if not port:
        return False
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pids_listening_on(port: int) -> list[int]:
    """查监听指定端口的 PID。按列位置解析 netstat，不依赖状态词的本地化文案。"""
    rc, out, _ = run_capture(["netstat", "-ano", "-p", "TCP"], timeout=25)
    if rc != 0:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local, _, pid = parts[1], parts[2], parts[3]
        if not local.endswith(f":{port}"):
            continue
        if not pid.isdigit():
            continue
        if int(pid) not in pids:
            pids.append(int(pid))
    return pids


# ------------------------------------------------------------------ 安装定位

def _install_from_dir(d: str, source: str) -> tuple[str, str, str, str] | None:
    """给一个 node bin 目录，返回 (node, bin_js, pkg_root, prefix)。"""
    node = os.path.join(d, "node.exe")
    bin_js = os.path.join(d, BIN_REL)
    if os.path.isfile(node) and os.path.isfile(bin_js):
        pkg_root = os.path.join(d, PKG_REL)
        return node, bin_js, pkg_root, d
    return None


def _parse_shim(shim_path: str) -> tuple[str, str, str, str] | None:
    """从 dsh.cmd / dsh / dsh.ps1 包装里解析出 bin.js 位置（兼容手写与 npm 生成两种格式）。"""
    try:
        with open(shim_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    shim_dir = os.path.dirname(shim_path)

    # 快速路径：npm / 手写包装都指向同目录下的 node_modules
    hit = _install_from_dir(shim_dir, "PATH 包装")
    if hit:
        return hit

    # 慢速路径：从脚本正文里抠路径
    for raw in re.findall(r'["\']([^"\']*dsh[\\/]lib[\\/]bin\.js)["\']', text):
        p = raw.replace("%~dp0", shim_dir + os.sep).replace("$basedir", shim_dir)
        p = os.path.normpath(p)
        if os.path.isfile(p):
            pkg_root = os.path.dirname(os.path.dirname(p))
            node = _find_node_near(pkg_root) or os.path.join(shim_dir, "node.exe")
            return node, p, pkg_root, prefix_of(pkg_root)
    return None


def prefix_of(pkg_root: str) -> str:
    """由包目录反推 npm 全局 prefix。

        <prefix>\\node_modules\\@deepseek-ai\\dsh    ← pkg_root
        <prefix>\\node_modules\\@deepseek-ai
        <prefix>\\node_modules
        <prefix>                                      ← 要的就是这个（往上三级）
    """
    d = pkg_root
    for _ in range(3):
        d = os.path.dirname(d)
    return d


def _find_node_near(pkg_root: str) -> str | None:
    """从 npm 全局 prefix 往上找 node.exe（nvm 布局下 node.exe 与 node_modules 同级）。"""
    d = prefix_of(pkg_root)
    for _ in range(4):
        cand = os.path.join(d, "node.exe")
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _npm_global_prefix(node: str) -> str | None:
    npm_cli = find_npm_cli(node)
    if not npm_cli:
        return None
    rc, out, _ = run_capture([node, npm_cli, "prefix", "-g"], timeout=45)
    if rc != 0:
        return None
    prefix = (out or "").strip().splitlines()[-1].strip() if out.strip() else ""
    return prefix or None


def find_npm_cli(node: str) -> str | None:
    """定位 npm 的 JS 入口。直调 node + npm-cli.js，绕开 .cmd 包装以避开控制台闪现。"""
    node_dir = os.path.dirname(node)
    cand = os.path.join(node_dir, "node_modules", "npm", "bin", "npm-cli.js")
    if os.path.isfile(cand):
        return cand
    # PATH 上的 npm 所在目录
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip('"')
        c = os.path.join(d, "node_modules", "npm", "bin", "npm-cli.js")
        if os.path.isfile(c):
            return c
    return None


def read_version(node: str, bin_js: str, timeout: float = 45) -> str | None:
    """优先问 CLI 要版本（官方公开接口），读不出来再退回 package.json。"""
    rc, out, _ = run_capture([node, bin_js, "--version"], timeout=timeout)
    if rc == 0 and out.strip():
        m = re.search(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?", out)
        if m:
            return m.group(0)
    pkg_json = os.path.join(os.path.dirname(os.path.dirname(bin_js)), "package.json")
    try:
        with open(pkg_json, "r", encoding="utf-8") as f:
            v = json.load(f).get("version")
        if v:
            return str(v)
    except (OSError, ValueError):
        pass
    return None


# winget / 官方 MSI 装完 Node 之后，当前进程的 PATH 里不会有它
# （环境变量变更不向已运行的进程传播），所以要认得这些标准落点。
_NODE_KNOWN_PATHS = (
    r"%ProgramFiles%\nodejs\node.exe",
    r"%ProgramFiles(x86)%\nodejs\node.exe",
    r"%LOCALAPPDATA%\Programs\nodejs\node.exe",
)


def _which_node() -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip('"')
        cand = os.path.join(d, "node.exe")
        if os.path.isfile(cand):
            return cand
    return None


def node_from_known_paths() -> str | None:
    """按标准安装落点找 node。用于"刚装完 Node、PATH 尚未生效"的时刻。"""
    for raw in _NODE_KNOWN_PATHS:
        p = os.path.expandvars(raw)
        if os.path.isfile(p):
            return p
    return None


def find_node() -> str | None:
    """找一个可用的 node.exe：PATH 优先，其次标准落点。"""
    return _which_node() or node_from_known_paths()


def _node_for(bin_js: str) -> str | None:
    """为给定 bin.js 配一个 node：就近优先，其次 PATH / 标准落点。

    为什么不能只认"就近"：私有 prefix 布局天然没有 node.exe（Node 装在别处），
    只认就近的话这类安装会被**静默跳过**——实测踩过：设了 DSH_BIN 指向私有
    prefix，结果解析器一声不吭地退回去用了另一个安装。
    """
    pkg_root = os.path.dirname(os.path.dirname(bin_js))
    node = _find_node_near(pkg_root) or find_node()
    return node if node and os.path.isfile(node) else None


def candidate_bin_js():
    """所有"看起来装了 DSH"的位置 → (来源, bin.js, node 覆盖值)。

    这里**不校验能否运行**——那是 read_version 的事。解耦之后，诊断层才能区分
    "根本没装"和"装了但跑不起来"（后者通常是 Node 版本过旧），给出不同的建议。
    """
    cfg = config.load()

    # 1. 显式覆盖：环境变量 / 配置文件，永远优先
    v = os.environ.get("DSH_BIN")
    if v and os.path.isfile(v):
        yield "环境变量 DSH_BIN", v, os.environ.get("DSH_NODE")
    c = cfg.get("dsh_bin")
    if c and os.path.isfile(c):
        yield "配置文件", c, cfg.get("dsh_node")

    # 2. 我们私有管理的安装。存在就一定优先——它是唯一由我们说准了位置、
    #    且能免管理员更新的那一份。（想强制用别处，在设置里指定 dsh_bin）
    priv = os.path.join(config.node_global_dir(), BIN_REL)
    if os.path.isfile(priv):
        yield "私有 prefix", priv, None

    # 3. PATH 上的 dsh 包装（等价于"用户在终端敲 dsh 会跑什么"）
    seen: set[str] = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip('"')
        if not d or d in seen:
            continue
        seen.add(d)
        for name in ("dsh.cmd", "dsh", "dsh.ps1"):
            shim = os.path.join(d, name)
            if os.path.isfile(shim):
                hit = _parse_shim(shim)
                if hit:
                    yield "PATH 上的 " + name, hit[1], None
                break

    # 4. npm 全局 prefix
    npm_node = _which_node()
    if npm_node:
        prefix = _npm_global_prefix(npm_node)
        if prefix:
            p = os.path.join(prefix, BIN_REL)
            if os.path.isfile(p):
                yield "npm 全局 prefix", p, None

    # 5. 已知回退（历史上手工装过的位置）
    for d in filter(None, [os.environ.get("DSH_FALLBACK_DIR"), r"D:\DevVmEnv\nodejs"]):
        p = os.path.join(d, BIN_REL)
        if os.path.isfile(p):
            yield "已知回退", p, None


def _candidate_dirs():
    """按可靠性排序的 (node, bin.js, 来源)。"""
    for source, bin_js, node_override in candidate_bin_js():
        node = node_override if node_override and os.path.isfile(node_override) else None
        node = node or _node_for(bin_js)
        if node:
            yield node, bin_js, source


def _cache_path() -> str:
    return config.install_cache_path()


def _load_cache() -> tuple[str, str] | None:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        node, bin_js = d.get("node"), d.get("bin_js")
        if node and bin_js and os.path.isfile(node) and os.path.isfile(bin_js):
            return node, bin_js
    except (OSError, ValueError):
        pass
    return None


def _save_cache(install: DshInstall) -> None:
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump({"node": install.node, "bin_js": install.bin_js,
                       "version": install.version, "source": install.source}, f,
                      ensure_ascii=False, indent=2)
    except OSError:
        pass


def try_resolve_install(force: bool = False) -> DshInstall | None:
    """定位 DSH 安装，**找不到就返回 None**（首次运行引导要用，不能抛异常）。

    命中缓存则跳过全量扫描（每次启动省 1-3 秒）。
    **有显式覆盖时不用缓存** —— 用户在设置里指定 dsh_bin 或环境变量 DSH_BIN，
    意图明确，不能被上一次的缓存结果顶掉。
    """
    explicit = bool(os.environ.get("DSH_BIN") or config.get("dsh_bin"))
    if not force and not explicit:
        cached = _load_cache()
        if cached:
            node, bin_js = cached
            v = read_version(node, bin_js, timeout=30)
            if v:
                pkg_root = os.path.dirname(os.path.dirname(bin_js))
                return DshInstall(node, bin_js, pkg_root, prefix_of(pkg_root),
                                  find_npm_cli(node), v, "缓存", compat.load_profile(v))

    for node, bin_js, source in _candidate_dirs():
        v = read_version(node, bin_js)
        if not v:
            continue
        pkg_root = os.path.dirname(os.path.dirname(bin_js))
        inst = DshInstall(
            node=node, bin_js=bin_js, pkg_root=pkg_root,
            prefix=prefix_of(pkg_root),
            npm_cli=find_npm_cli(node), version=v, source=source,
            profile=compat.load_profile(v),
        )
        _save_cache(inst)
        return inst
    return None


def resolve_install(force: bool = False) -> DshInstall:
    inst = try_resolve_install(force)
    if inst is None:
        tried = [src for src, _b, _n in candidate_bin_js()]
        raise DshError(
            "未能定位 DSH 安装。已尝试：" +
            ("、".join(tried) if tried else "无任何候选位置") +
            "\n如果 DSH 装在非常规位置，可在设置里指定 dsh_node / dsh_bin。"
        )
    return inst


# ------------------------------------------------------------------ 环境诊断

@dataclass
class EnvironmentReport:
    """首次运行自检结果。

    判断逻辑全部在这里，界面只负责展示 —— 界面层不允许出现
    "如果没有 node 就……" 这种判断，否则又变成耦合。
    """
    node: str | None = None
    node_version: str | None = None
    npm_cli: str | None = None
    install: DshInstall | None = None
    # 装了但跑不起来的位置（通常意味着 Node 版本过旧）
    broken: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.install is not None

    @property
    def need_node(self) -> bool:
        return self.node is None

    @property
    def need_dsh(self) -> bool:
        return self.node is not None and self.install is None

    def rows(self) -> list[dict]:
        """给界面用的三行状态。文案面向使用者，不出现 npm prefix 这类词。"""
        return [
            {
                "name": "Node.js 运行环境",
                "ok": bool(self.node),
                "text": (f"已就绪，版本 {self.node_version}"
                         if self.node and self.node_version else
                         ("已就绪" if self.node else "未安装")),
            },
            {
                "name": "npm 包管理器",
                "ok": bool(self.npm_cli),
                "text": "已就绪" if self.npm_cli else "未安装",
            },
            {
                "name": "DeepSeek Harness",
                "ok": bool(self.install),
                "text": (f"已就绪，版本 {self.install.version}" if self.install else "未安装"),
            },
        ]


def diagnose() -> EnvironmentReport:
    """首次运行自检。**不抛异常** —— 把"缺什么"如实报出来，交给界面引导。"""
    rep = EnvironmentReport()

    # 先解析安装，再决定用哪个 node —— 否则会出现"报告的版本来自 PATH 上的 node，
    # 实际用的是安装旁边的另一个 node"这种自相矛盾
    inst = try_resolve_install()
    rep.install = inst

    node = (inst.node if inst else None) or find_node()
    if node:
        rep.node = node
        rc, out, _ = run_capture([node, "--version"], timeout=30)
        if rc == 0 and out.strip():
            rep.node_version = out.strip().splitlines()[0].strip()
        rep.npm_cli = find_npm_cli(node)

    if inst is None:
        # 区分"根本没装"和"装了但跑不起来"——两者给用户的建议完全不同
        for source, bin_js, _n in candidate_bin_js():
            rep.broken.append(f"{source}（{bin_js}）")
        if rep.broken:
            rep.detail = ("在以下位置找到了 DeepSeek Harness 但无法运行，"
                          "通常是 Node.js 版本过旧或安装不完整。")
        elif rep.node and not rep.npm_cli:
            rep.detail = "Node.js 存在但未找到 npm，安装可能不完整，建议重新安装 Node.js。"
        elif rep.node:
            rep.detail = "Node.js 已就绪，但未安装 DeepSeek Harness。"
        else:
            rep.detail = "未检测到 Node.js 运行环境，需要先安装。"
    return rep


# ------------------------------------------------------------------ DSH_HOME

def resolve_home() -> str | None:
    """DSH_HOME 定位：环境变量优先，其次常见约定位置。找不到就返回 None 交给 DSH 自己决定。"""
    profile = compat.load_profile(None)
    cfg = config.load()

    if cfg.get("dsh_home"):
        return cfg["dsh_home"]

    for key in profile.get("home_env", ["DSH_HOME"]):
        v = os.environ.get(key)
        if v and os.path.isdir(v):
            return v

    for raw in profile.get("home_candidates", []):
        p = os.path.expanduser(raw)
        if os.path.isdir(p):
            return p
    return None


def clear_stale_locks(home: str | None, profile: dict) -> list[str]:
    """清理强杀/崩溃残留的锁文件。属于"可选优化"——清不掉也不影响启动。"""
    removed: list[str] = []
    if not home:
        return removed
    for name in profile.get("stale_locks", []):
        p = os.path.join(home, name)
        try:
            os.remove(p)
            removed.append(name)
        except OSError:
            pass
    return removed


# ------------------------------------------------------------------ 数据指纹

# 遍历上限。DSH_HOME 可能很大（单个工作区的会话与附件到 GB 级都不奇怪），
# 所以统计必须封顶——否则光是数一遍就要几十秒，更新流程会被拖住。
_FP_MAX_ENTRIES = 60000
_FP_MAX_SECONDS = 20.0


def human_bytes(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024:
            return ("%.0f %s" % (v, unit)) if unit == "B" else ("%.1f %s" % (v, unit))
        v /= 1024
    return "%.1f TB" % (v / 1024)


@dataclass
class HomeFingerprint:
    """DSH_HOME 的规模快照：文件数与总字节数。

    刻意**只统计、不复制**。老大的工作区曾到 2GB，快照会变成不可预估的存储负担；
    而"更新会不会动用户数据"这件事，一对数字就够了。
    """
    path: str = ""
    files: int = 0
    bytes: int = 0
    truncated: bool = False

    def describe(self) -> str:
        text = "%d 个文件 / %s" % (self.files, human_bytes(self.bytes))
        return text + "（数据量过大，统计已截断）" if self.truncated else text

    def lost_against(self, before: "HomeFingerprint") -> bool:
        """是否存在"东西变少了"的迹象。截断过的统计不做判断，避免误报。"""
        if self.truncated or before.truncated:
            return False
        return self.files < before.files or self.bytes < before.bytes


def home_fingerprint(home: str | None = None) -> HomeFingerprint:
    """统计 DSH_HOME 的文件数与总字节数。

    用于更新前后各取一次，确认"程序换了、用户数据一个没少"。
    排除 `*.lock`：那是进程运行期的临时文件，不该影响对数据完整性的判断。
    """
    home = home or resolve_home()
    fp = HomeFingerprint(path=home or "")
    if not home or not os.path.isdir(home):
        return fp

    deadline = time.time() + _FP_MAX_SECONDS
    stack = [home]
    while stack:
        if fp.files >= _FP_MAX_ENTRIES or time.time() > deadline:
            fp.truncated = True
            break
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            if entry.name.endswith(".lock"):
                                continue
                            fp.files += 1
                            fp.bytes += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return fp


# ------------------------------------------------------------------ 启动 / 就绪

def _extract(text: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        try:
            m = re.search(pat, text)
        except re.error:
            continue
        if m:
            return m.group(0)
    return None


def _absorb(handle: ServiceHandle, chunk: str) -> None:
    """从新增输出里吸收 URL / 端口 / 令牌。"""
    profile = handle.install.profile
    if not chunk:
        return
    url = _extract(chunk, profile.get("url_regexes", []))
    if url:
        handle.url = url
        m = re.search(r":(\d+)", url)
        if m:
            handle.port = int(m.group(1))
        tm = re.search(r"[?&]token=([A-Za-z0-9_\-.]+)", url)
        if tm:
            handle.token = tm.group(1)
        return
    if not handle.token:
        tok = _extract(chunk, profile.get("log_token_regexes", []))
        if tok:
            handle.token = tok.split("=", 1)[-1]


def _read_new(path: str, pos: int) -> tuple[str, int]:
    try:
        size = os.path.getsize(path)
        if size <= pos:
            return "", (pos if size <= pos else size)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(pos)
            data = f.read()
        return data, size
    except OSError:
        return "", pos


def _build_url(handle: ServiceHandle) -> str:
    if handle.url:
        return handle.url
    base = f"http://{handle.host}:{handle.port}/"
    return f"{base}?token={handle.token}" if handle.token else base


def launch(install: DshInstall, port: int, log_path: str, cwd: str | None = None) -> ServiceHandle:
    """启动 dsh web。stdout/stderr 落到我们自己的日志文件——不读 DSH 的私有日志。"""
    profile = install.profile
    home = resolve_home()
    clear_stale_locks(home, profile)

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    args = [install.node, install.bin_js]
    args += list(profile.get("cli", {}).get("web", ["web"]))
    args += list(profile.get("cli", {}).get("no_open", ["--no-open"]))
    args += list(profile.get("cli", {}).get("port", ["--port"])) + [str(port)]

    env = os.environ.copy()
    if home:
        env["DSH_HOME"] = home

    fh = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            args, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            cwd=cwd or os.path.dirname(log_path), env=env,
            creationflags=DETACHED,
        )
    finally:
        fh.close()   # 子进程已持有该句柄的独立副本

    return ServiceHandle(
        install=install, pid=proc.pid, host=profile.get("service_host", "127.0.0.1"),
        requested_port=port, port=(port or 0), url="", token="",
        log_path=log_path, started_at=time.time(), proc=proc,
        _read_pos=os.path.getsize(log_path) if os.path.exists(log_path) else 0,
    )


def wait_ready(handle: ServiceHandle, timeout: float | None = None,
               on_tick=None) -> bool:
    """等待服务就绪。

    port=0 时端口由系统分配，因此必须先从输出里拿到 URL 才知道该探哪个端口；
    固定端口时 TCP 探测与 URL 解析并行，谁先成功都算就绪。

    注意就绪后的 URL 兜底：DSH 写 `dsh web: <url>` 与开始监听几乎同一瞬间，
    而 TCP 一连通我们就该返回。若此时还没读到 URL 行，会出现"就绪但 URL 为空"
    ——首屏会直接变成 401 页。所以 TCP 判定就绪后必须再给一小段宽限期补读 URL。
    """
    profile = handle.install.profile
    timeout = timeout or float(profile.get("ready_timeout", 120))
    interval = float(profile.get("poll_interval", 0.5))
    grace = float(profile.get("url_grace", 3.0))
    deadline = time.time() + timeout

    while time.time() < deadline:
        chunk, pos = _read_new(handle.log_path, handle._read_pos)
        if chunk:
            handle._read_pos = pos
            _absorb(handle, chunk)

        if handle.port and tcp_open(handle.host, handle.port):
            _finalize_url(handle, grace)
            return True

        if handle.proc is not None and handle.proc.poll() is not None:
            tail = _tail(handle.log_path, 1200)
            raise DshError(f"dsh web 进程已退出（exit={handle.proc.returncode}）。"
                           f"日志尾部：\n{tail}")

        if on_tick:
            try:
                on_tick(handle)
            except Exception:
                pass
        time.sleep(interval)

    raise DshError(
        f"{int(timeout)} 秒内未检测到服务就绪。\n"
        f"期望端口：{'系统分配' if handle.requested_port == 0 else handle.requested_port}\n"
        f"已解析端口：{handle.port or '未解析到'}\n"
        f"日志尾部：\n{_tail(handle.log_path, 1200)}"
    )


def _finalize_url(handle: ServiceHandle, grace: float) -> None:
    """就绪后补读 URL；宽限期内拿不到就按现有信息兜底，绝不返回空 URL。"""
    if handle.url:
        return
    until = time.time() + grace
    while time.time() < until and not handle.url:
        chunk, pos = _read_new(handle.log_path, handle._read_pos)
        if chunk:
            handle._read_pos = pos
            _absorb(handle, chunk)
        if not handle.url:
            time.sleep(0.1)
    # 兜底：至少给一个能打开的地址，DSH 自己的鉴权会接手
    if not handle.url:
        handle.url = _build_url(handle)
    if not handle.url:
        handle.url = f"http://{handle.host}:{handle.port}/"


def _tail(path: str, nbytes: int) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            if size > nbytes:
                f.seek(size - nbytes)
            return f.read().strip() or "(日志为空)"
    except OSError:
        return "(无法读取日志)"


def recover_url(install: DshInstall, port: int, log_path: str) -> str:
    """附加到已在运行的服务时恢复 URL。

    三级降级：① 本次日志的 URL 行 → ② 日志里的令牌 → ③ 裸 URL（交给 DSH 自己的鉴权）。
    全部失败也返回裸 URL，绝不因为"拿不到令牌"而拒绝启动。
    """
    profile = install.profile
    host = profile.get("service_host", "127.0.0.1")
    text = _tail(log_path, 200_000)
    url = _extract(text, profile.get("url_regexes", [])) or \
        _extract(text, profile.get("log_url_regexes", []))
    if url:
        return url
    tok = _extract(text, profile.get("log_token_regexes", []))
    token = tok.split("=", 1)[-1] if tok else ""
    base = f"http://{host}:{port}/"
    return f"{base}?token={token}" if token else base


# ------------------------------------------------------------------ 停止

def probe_http(url: str, timeout: float = 4.0) -> tuple[bool, int, str]:
    """轻量 HTTP 探针：返回 (是否得到响应, 状态码, 响应体片段)。

    用途是区分"端口被自家 DSH 占着"和"端口被别的程序占着"——
    只靠 TCP 连通性分不出来，而这两种情况的处置完全不同。
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dsh-desktop"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4096).decode("utf-8", "ignore")
            return True, getattr(r, "status", 200), body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(4096).decode("utf-8", "ignore")
        except Exception:
            body = ""
        return True, e.code, body
    except (urllib.error.URLError, OSError, ValueError):
        return False, 0, ""


def looks_like_dsh(url: str, timeout: float = 4.0) -> bool:
    ok, status, body = probe_http(url, timeout)
    if not ok:
        return False
    low = body.lower()
    if "dsh" in low or "deepseek" in low:
        return True
    # 有些版本会先跳转到带 token 的地址
    return status in (301, 302, 303, 307, 308)


# ------------------------------------------------------------------ 运行时状态

def _state_path() -> str:
    return config.service_state_path()


def save_state(handle: ServiceHandle | None) -> None:
    """把我们自己 spawn 的服务句柄落盘，供下次启动时接管（不依赖任何 DSH 私有信息）。"""
    path = _state_path()
    try:
        if handle is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": handle.pid, "host": handle.host, "port": handle.port,
                "requested_port": handle.requested_port, "url": handle.url,
                "token": handle.token, "log_path": handle.log_path,
                "started_at": handle.started_at,
            }, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_state() -> dict | None:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def stop(handle: ServiceHandle | None = None, port: int | None = None) -> int:
    """停止 dsh web。

    优先级：自己 spawn 的 PID（最可靠）→ 端口反查 PID。
    端口反查不依赖 netstat 的状态词文案，只按列位置取 PID，并对映像名做一次校验。
    """
    killed = 0

    candidates: list[int] = []
    if handle and handle.pid:
        candidates.append(handle.pid)
    if port:
        for pid in pids_listening_on(port):
            if pid not in candidates:
                candidates.append(pid)

    for pid in candidates:
        img = _image_of(pid)
        if img and img != "node.exe":
            # 端口被别的程序占着——不是我们的服务，不动它
            continue
        if _kill_pid(pid):
            killed += 1
    return killed
