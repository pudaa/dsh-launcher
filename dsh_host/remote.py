# -*- coding: utf-8 -*-
"""远程 DSH：复用系统的 OpenSSH，把远端的 loopback 端口借到本机。

为什么是这个方案
----------------
DSH 把"只听 127.0.0.1"当成安全边界（官方明说拒绝绑 0.0.0.0 的理由是 RCE），
所以我们绝不去改它的绑定。要做远程，只能在**传输层**下手：远端仍然只听 loopback，
用 SSH 把那个端口搬到本机的 loopback 上。

链路：

    浏览器/QtWebEngine → 127.0.0.1:<本地端口> → ssh -L → 127.0.0.1:<远端端口> → dsh web

**端到端 Host 是自洽的**（浏览器发 `127.0.0.1:<本地端口>`，DSH 收到的还是它），
所以既不需要 `--trusted-host`，也不需要改写 Host 的代理——2026-11-12 实测：
`/api` 的栅栏只要求 Host 是 loopback 字面量，不比对端口；401 来自 cookie 绑定
签发时的 authority，链路自洽就不会触发。详见 `docs/remote-ssh.md`。

为什么不自带 SSH 实现
--------------------
Windows 10+ 自带 OpenSSH，用户机器上通常已经有 `~/.ssh/config`、密钥和 known_hosts。
复用它就等于复用了用户已经配好的一切（含 ProxyJump、agent、证书），
和我们"复用用户已有 DSH"是同一套哲学，还省掉几 MB 的打包体积和一堆密码存储问题。
代价是只支持密钥/agent 认证——这是有意为之，不把密码落盘。

这一层不碰 Qt：所有函数都是同步的、可被工作线程调用，进度靠回调传出去。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time

from . import config

#: 远端状态目录（相对远端 `$HOME`）。用 `umask 077` + `chmod 700` 建，
#: 里面只有一个 pid、一份日志、一个启动命令和一次启动时间。
REMOTE_STATE = "$HOME/.dsh-remote"

#: 远端脚本的结构化输出前缀。SSH 登录会打 banner、`Last login:`、`~/.profile`
#: 的杂音，不加前缀就只能靠猜——这是别人踩过的坑，直接照抄结论。
TAG = "DSHREMOTE"

#: 远端启动超时（秒）。DSH 冷启动实测 10~30 s，留足余量。
DEFAULT_START_TIMEOUT = 90

#: 本地端口的尝试次数。`ExitOnForwardFailure` 会告诉我们绑定失败，换一个再试。
PORT_ATTEMPTS = 5


class RemoteError(Exception):
    """远程链路的失败，带可读分类。

    `kind` 决定界面给什么建议，不要把它降级成一个字符串——"连不上 SSH"和
    "连上了但远端没有 dsh"对用户来说是完全不同的两件事。
    """

    def __init__(self, kind: str, message: str, detail: str = "",
                 retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail
        self.retryable = retryable

    def __str__(self) -> str:
        return self.message + (("\n" + self.detail) if self.detail else "")


#: 失败分类 → 界面上的建议（每一条都要能指导用户做点什么）
KIND_HINTS = {
    "no_ssh": "这台电脑找不到 ssh 命令。Windows 10 以后自带 OpenSSH 客户端，"
              "可在「设置 → 应用 → 可选功能」里添加。",
    "auth": "SSH 认证没有通过。请确认这台电脑的公钥已加入对方的 authorized_keys，"
            "或先在终端里手动 ssh 一次确认能登录。",
    "host_key": "对方的主机密钥与上次记录的不一致。这可能是换了机器或重装系统，"
                "也可能是中间人。确认无误后，删掉 known_hosts 里对应的那一行再连。",
    "unreachable": "连不上对方的 SSH。请检查地址、端口和网络。",
    "no_shell": "登录成功，但对方没有可用的 sh。Windows 作为被控端目前不支持。",
    "no_dsh": "对方机器上没有找到 dsh。请先在那边执行：npm install -g @deepseek-ai/dsh",
    "start_timeout": "远端 dsh web 启动超时。可能是机器太慢或插件初始化卡住，"
                     "详情见下方日志。",
    "exited": "远端 dsh web 启动后立刻退出了，日志见下方。",
    "shared": "远端已有一个我们上次启动的实例在跑，但当前配置与它不一致。"
              "请选择「重启远端服务」再试。",
    "tunnel": "SSH 连上了，但端口转发没建起来。",
    "config": "主机配置不完整。",
}


def _err(kind: str, message: str = "", detail: str = "",
         retryable: bool = False) -> RemoteError:
    e = RemoteError(kind, message or KIND_HINTS.get(kind, kind), detail, retryable)
    return e


def _q(value: str) -> str:
    """POSIX sh 单引号转义。"""
    return "'" + str(value).replace("'", "'\\''") + "'"


def workdir() -> str:
    """本地侧的工作目录（放 SSH 的 known_hosts 快照、临时脚本，不放凭据）。"""
    d = os.path.join(config.data_root(), "remote")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------------ SSH 定位

def find_ssh() -> str:
    """找到可用的 ssh 客户端。

    优先系统目录里的那个：PATH 上可能先命中 Git Bash 的 ssh，它读的是另一个
    HOME 下的配置，用户会看到"我明明配了却说没配"。系统自带的那个才和
    `~/.ssh/config`、Windows 的 OpenSSH Agent 一致。
    """
    candidates = [
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "OpenSSH", "ssh.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "OpenSSH", "ssh.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    found = shutil.which("ssh")
    if found:
        return found
    raise _err("no_ssh")


def ssh_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh", "config")


def ssh_config_aliases() -> list[str]:
    """列出 `~/.ssh/config` 里可选的 Host 别名。

    只取具体的别名：带通配符的（`*`、`?`）是匹配规则，不是能直接连的目标；
    `github.com` 之类指向 git 服务的条目也没意义，但它们无法从语法上区分，
    所以全部返回、由界面合并去重。
    """
    path = ssh_config_path()
    names: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                m = re.match(r"(?i)^host\s+(.+)$", raw)
                if not m:
                    continue
                for tok in m.group(1).split():
                    if "*" in tok or "?" in tok or tok.startswith("!"):
                        continue
                    if tok not in names:
                        names.append(tok)
    except OSError:
        return []
    return names


# ------------------------------------------------------------------ 主机配置

#: 断开后怎么处理远端服务。三种取值，语义见 docs/remote-ssh.md §7。
KEEP_KEEP, KEEP_STOP, KEEP_ASK = "keep", "stop", "ask"

#: 启动命令必须接受这三个参数，否则我们没法让它只听 loopback。
REQUIRED_FLAGS = ("--host", "--port", "--no-open")


class SshHost:
    """一台可连的远端机器。**不含任何凭据。**

    凭据全部交给系统 SSH（密钥 / agent / `~/.ssh/config`），我们只记
    "连哪儿、怎么起服务、断开后留不留"。
    """

    def __init__(self, name: str = "", host: str = "", user: str = "",
                 port: int = 22, launch_command: str = "dsh web",
                 keep_service: str = KEEP_ASK, id: str = ""):
        self.id = id or self.new_id(host)
        self.name = name or host
        self.host = host
        self.user = user
        self.port = int(port or 22)
        self.launch_command = (launch_command or "dsh web").strip()
        self.keep_service = keep_service if keep_service in (
            KEEP_KEEP, KEEP_STOP, KEEP_ASK) else KEEP_ASK

    @staticmethod
    def new_id(host: str) -> str:
        return "ssh-" + re.sub(r"[^a-z0-9]+", "-", (host or "host").lower()).strip("-")

    @property
    def target(self) -> str:
        """传给 ssh 的参数。有 user 就拼进去。

        如果 `host` 已经是 `~/.ssh/config` 里的别名，**不要**覆盖它的 User/Port——
        别名存在的意义就是把那些细节收在 config 里。所以我们只在用户显式填了
        user/port 时才拼。
        """
        t = self.host
        if self.user:
            t = "%s@%s" % (self.user, t)
        return t

    @property
    def display_address(self) -> str:
        """界面上的地址显示：`user@host:port`，默认端口不写出来。"""
        s = self.host if not self.user else "%s@%s" % (self.user, self.host)
        if self.port and self.port != 22:
            s += ":%d" % self.port
        return s

    def base_args(self, ssh: str) -> list[str]:
        args = [ssh, "-p", str(self.port)] if self.port and self.port != 22 else [ssh]
        return args + ["-o", "BatchMode=yes",
                       "-o", "ConnectTimeout=10",
                       "-o", "StrictHostKeyChecking=accept-new",
                       "-o", "UserKnownHostsFile=" + os.path.join(
                           os.path.expanduser("~"), ".ssh", "known_hosts")]

    def validate(self) -> None:
        if not self.host.strip():
            raise _err("config", "请填写主机地址或 ~/.ssh/config 里的别名。")
        missing = [f for f in REQUIRED_FLAGS if f not in self.launch_command]
        if missing and self.launch_command != "dsh web":
            # 自定义命令必须接受这三个参数，否则我们无法让它只听 loopback / 只选端口。
            raise _err(
                "config",
                "启动命令需要原样接受 --host / --port / --no-open 三个参数，"
                "我们会自动追加。当前命令缺少：%s" % "、".join(missing))

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "host": self.host,
                "user": self.user, "port": self.port,
                "launch_command": self.launch_command,
                "keep_service": self.keep_service}

    @classmethod
    def from_json(cls, raw: object) -> "SshHost | None":
        if not isinstance(raw, dict):
            return None
        host = str(raw.get("host") or "").strip()
        if not host:
            return None
        try:
            port = int(raw.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        return cls(id=str(raw.get("id") or ""),
                   name=str(raw.get("name") or host),
                   host=host, user=str(raw.get("user") or ""), port=port,
                   launch_command=str(raw.get("launch_command") or "dsh web"),
                   keep_service=str(raw.get("keep_service") or KEEP_ASK))


def load_hosts() -> list[SshHost]:
    path = config.remote_hosts_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    items = raw.get("hosts") if isinstance(raw, dict) else raw
    out = []
    for item in items or []:
        h = SshHost.from_json(item)
        if h is not None:
            out.append(h)
    return out


def save_hosts(hosts: list[SshHost]) -> None:
    path = config.remote_hosts_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"hosts": [h.to_json() for h in hosts]}, f,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------------ 远端脚本
#
# 全部是纯 POSIX sh：对方的登录 shell 可能是 bash / zsh / fish / busybox，
# 我们统一用 `sh -l -s` 把脚本从 stdin 喂进去，不依赖对方默认 shell 的语法。
#
# 三个约定：
#   · 每行结果都带 `DSHREMOTE ` 前缀——SSH 登录会打 banner、Last login、
#     ~/.profile 的杂音，不加前缀就没法可靠解析；
#   · 服务用 nohup 起在后台，`</dev/null` + `>>log` 三重定向缺一不可，
#     少一个 SSH 会一直挂着等子进程释放管道；
#   · 身份用「pid + 进程启动时间」，因为 PID 会被系统回收——
#     只判 `kill -0` 可能认错进程、甚至杀掉别人的。

_PRELUDE = r'''
_st() {
  if [ -r "/proc/$1/stat" ]; then
    S=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    S=${S##*) }
    set -- $S
    [ "$#" -ge 20 ] || return 1
    shift 19
    printf '%s\n' "$1"
  else
    ps -p "$1" -o lstart= 2>/dev/null | sed 's/^ *//'
  fi
}
ours() {
  kill -0 "$1" 2>/dev/null || return 1
  if [ -f "$D/web.st" ]; then
    A=$(_st "$1") || A=""
    B=$(cat "$D/web.st" 2>/dev/null) || B=""
    if [ -n "$A" ] && [ -n "$B" ]; then
      [ "$A" = "$B" ]
      return $?
    fi
  fi
  # 拿不到启动时间（macOS 的 ps 参数不同、部分精简系统的 ps 也缺 lstart）时，
  # 退到命令行特征：只认我们自己追加的那串参数，别的进程不会长这样。
  A=$(ps -p "$1" -o args= 2>/dev/null)
  case "$A" in
    *"--host 127.0.0.1 --port 0 --no-open"*) return 0;;
  esac
  return 1
}
_latest_url() {
  grep -E 'https?://[^ ]*[?&]token=' "$D/web.log" 2>/dev/null | tail -n 1
}
'''

#: `dsh` 在非交互 shell 里常常不在 PATH 上（`~/.bashrc` 的追加不生效），
#: 所以按常见安装位置再找一遍。找不到就明确报 nodsh。
_DSH_SEARCH = r'''
if ! command -v dsh >/dev/null 2>&1; then
  F=""
  for c in "$HOME/.local/bin" "$HOME/.npm-global/bin" "$HOME/.npm/bin" \
           "$HOME/.volta/bin" "$HOME/.bun/bin" "$HOME/.local/share/pnpm" \
           "$HOME/Library/pnpm" /usr/local/bin /opt/homebrew/bin \
           "$HOME"/.nvm/versions/node/*/bin "$HOME"/.local/share/fnm/node-versions/*/installation/bin; do
    if [ -x "$c/dsh" ]; then F="$c"; fi
  done
  if [ -n "$F" ]; then PATH="$F:$PATH"; export PATH; fi
fi
'''


def probe_script() -> str:
    """看看远端有没有我们上次启动的实例，以及它现在是什么状态。

    纯读，**只在确认进程已经不存在时才删状态文件**。
    这点很重要：如果"认不出是不是我们的"（比如系统的 ps 不支持某个参数），
    就删掉 pid 文件，那个进程会变成一个谁也停不掉的孤儿——下次连接既不能
    复用、也杀不掉。所以身份对不上时只标记 `foreign`，把决定权交给上层。
    """
    return (
        "umask 077\n"
        'D="%s"\n'
        'printf \'%s home %%s\\n\' "$HOME"\n'
        "%s"
        'if [ -f "$D/web.pid" ]; then\n'
        '  P=$(cat "$D/web.pid" 2>/dev/null)\n'
        '  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then\n'
        '    if ours "$P"; then\n'
        '      printf \'%s alive %%s\\n\' "$P"\n'
        '      [ -f "$D/web.st" ] && printf \'%s start %%s\\n\' "$(cat "$D/web.st")"\n'
        '      printf \'%s cmd %%s\\n\' "$(cat "$D/web.cmd" 2>/dev/null)"\n'
        '      printf \'%s url %%s\\n\' "$(_latest_url)"\n'
        "    else\n"
        '      printf \'%s foreign %%s\\n\' "$P"\n'
        "    fi\n"
        '  else\n'
        '    rm -f "$D/web.pid" "$D/web.st"\n'
        "  fi\n"
        "fi\n"
        "true\n"
    ) % (REMOTE_STATE, TAG, _PRELUDE, TAG, TAG, TAG, TAG, TAG)


def start_script(command: str, timeout: int = DEFAULT_START_TIMEOUT) -> str:
    """在远端启动 dsh web，等它打印带令牌的地址，然后退出（服务留在后台）。

    `command` 由用户配置（默认 `dsh web`），我们只在后面追加三个参数：
    `--host 127.0.0.1`（只听本机，这是我们的安全底线）、
    `--port 0`（端口由系统分配，避免和其它实例抢）、`--no-open`（远端没有浏览器）。
    """
    seconds = max(5, min(int(timeout), 600))
    return (
        "umask 077\n"
        'D="%s"\n'
        'printf \'%s home %%s\\n\' "$HOME"\n'
        'mkdir -p "$D" || { printf \'%s nodir\\n\'; exit 70; }\n'
        'chmod 700 "$D" 2>/dev/null\n'
        "%s"
        "%s"
        'if [ -f "$D/web.pid" ]; then\n'
        '  P=$(cat "$D/web.pid" 2>/dev/null)\n'
        '  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then\n'
        '    if ours "$P"; then printf \'%s shared\\n\'; exit 73; fi\n'
        '    printf \'%s foreign %%s\\n\' "$P"; exit 74\n'
        "  fi\n"
        "fi\n"
        'printf \'%%s\' %s > "$D/web.cmd"\n'
        ': > "$D/web.log"\n'
        "NO_COLOR=1; export NO_COLOR\n"
        'N=""; command -v nohup >/dev/null 2>&1 && N=nohup\n'
        '$N sh -c %s </dev/null >>"$D/web.log" 2>&1 &\n'
        "P=$!\n"
        '_st "$P" > "$D/web.st" 2>/dev/null\n'
        'printf \'%%s\\n\' "$P" > "$D/web.pid"\n'
        'printf \'%s started %%s\\n\' "$P"\n'
        "i=0\n"
        "while [ $i -lt %d ]; do\n"
        '  L=$(_latest_url)\n'
        '  if [ -n "$L" ]; then printf \'%s url %%s\\n\' "$L"; exit 0; fi\n'
        '  if ! kill -0 "$P" 2>/dev/null; then\n'
        '    printf \'%s exited\\n\'\n'
        '    tail -n 20 "$D/web.log" 2>/dev/null | sed \'s/^/%s log /\'\n'
        "    exit 1\n"
        "  fi\n"
        "  sleep 1; i=$((i+1))\n"
        "done\n"
        'printf \'%s timeout\\n\'\n'
        'tail -n 20 "$D/web.log" 2>/dev/null | sed \'s/^/%s log /\'\n'
        "exit 1\n"
    ) % (REMOTE_STATE, TAG, TAG, _PRELUDE, _DSH_SEARCH, TAG, TAG,
         _q(command), _q(command + " --host 127.0.0.1 --port 0 --no-open"),
         TAG, seconds, TAG, TAG, TAG, TAG, TAG)


def stop_script() -> str:
    """停掉**我们自己启动的**那个实例。

    先核启动时间再动手：`web.pid` 里的号可能已经被系统分配给了别的进程，
    无脑 `kill` 等于在别人的机器上杀别人的进程。
    认不出身份时**连状态文件都不动**——留着它，下次还能查到"有这么个东西"；
    删掉就真成谁也找不到的孤儿了。
    """
    return (
        "umask 077\n"
        'D="%s"\n'
        "%s"
        'if [ -f "$D/web.pid" ]; then\n'
        '  P=$(cat "$D/web.pid" 2>/dev/null)\n'
        '  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then\n'
        '    if ours "$P"; then\n'
        '      if command -v pkill >/dev/null 2>&1; then pkill -TERM -P "$P" 2>/dev/null; fi\n'
        '      kill "$P" 2>/dev/null\n'
        '      i=0\n'
        '      while kill -0 "$P" 2>/dev/null && [ $i -lt 10 ]; do sleep 1; i=$((i+1)); done\n'
        '      ours "$P" && kill -9 "$P" 2>/dev/null\n'
        '      printf \'%s stopped %%s\\n\' "$P"\n'
        '      rm -f "$D/web.pid" "$D/web.st"\n'
        "    else\n"
        '      printf \'%s notours %%s\\n\' "$P"\n'
        "    fi\n"
        "  else\n"
        '    printf \'%s nothing\\n\'\n'
        '    rm -f "$D/web.pid" "$D/web.st"\n'
        "  fi\n"
        "else\n"
        '  printf \'%s nothing\\n\'\n'
        "fi\n"
        "true\n"
    ) % (REMOTE_STATE, _PRELUDE, TAG, TAG, TAG, TAG)


# ------------------------------------------------------------------ 结果解析

def parse_tagged(text: str) -> dict:
    """把 `DSHREMOTE key value` 的行收成一个字典。

    同一 key 出现多次时保留**最后一条**：启动脚本会先打 home，再打 started/url，
    日志尾部还可能追加若干条，取最后一条才是当前状态。
    """
    out: dict = {}
    logs: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith(TAG + " "):
            continue
        rest = line[len(TAG) + 1:]
        key, _, value = rest.partition(" ")
        key, value = key.strip(), value.strip()
        if key == "log":
            logs.append(value)
            continue
        out[key] = value
    if logs:
        out["log"] = "\n".join(logs)
    return out


class RemoteEndpoint:
    """远端那个 dsh web 的坐标。令牌只在它的生命周期内有效。"""

    def __init__(self, port: int, token: str, pid: int = 0, reused: bool = False,
                 home: str = "", process_start: str = ""):
        self.port = port
        self.token = token
        self.pid = pid
        self.reused = reused
        self.home = home
        self.process_start = process_start

    def describe(self) -> str:
        return "127.0.0.1:%d（%s）" % (
            self.port, "复用已运行的实例" if self.reused else "本次启动")


def _url_of(line: str) -> tuple[int, str]:
    """从 `dsh web: http://127.0.0.1:<port>/?token=<token>` 里取端口和令牌。"""
    m = re.search(r"http://[^/\s]+:(\d+)/[^\s]*?[?&]token=([A-Za-z0-9_\-.]+)",
                  line or "")
    if not m:
        return 0, ""
    return int(m.group(1)), m.group(2)


# ------------------------------------------------------------------ 执行

_NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW


class RemoteShell:
    """在远端执行一段 sh 脚本，把带前缀的输出收成字典。

    脚本走 stdin（`sh -l -s`）而不是命令行参数，原因有两个：脚本里有换行和
    大量引号，拼进命令行会在 Windows 上撞长度上限、还要处理两层转义；
    `-l` 让它是登录 shell，`~/.profile` 里追加的 PATH 才生效。
    """

    def __init__(self, host: SshHost, ssh: str | None = None):
        self.host = host
        self.ssh = ssh or find_ssh()

    def run(self, script: str, timeout: float = 30) -> dict:
        args = (self.host.base_args(self.ssh)
                + [self.host.target, "sh -l -s"])
        try:
            proc = subprocess.run(
                args, input=script.encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise _err("unreachable",
                       "SSH 命令超过 %d 秒没有返回。" % timeout, retryable=True)
        except OSError as e:
            raise _err("no_ssh", "无法执行 ssh：%s" % e)
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace").strip()
        if TAG not in out:
            raise self._classify(proc.returncode, err)
        return parse_tagged(out)

    @staticmethod
    def _classify(code: int, err: str) -> RemoteError:
        low = (err or "").lower()
        if "permission denied" in low or "publickey" in low \
                or "no supported authentication" in low:
            return _err("auth", detail=err)
        if "host key verification failed" in low \
                or "remote host identification has changed" in low:
            return _err("host_key", detail=err)
        if "sh: not found" in low or "command not found" in low:
            return _err("no_shell", detail=err)
        if "could not resolve" in low or "connection refused" in low \
                or "connection timed out" in low or "no route to host" in low:
            return _err("unreachable", detail=err, retryable=True)
        return _err("unreachable", "SSH 执行失败（退出码 %s）。" % code,
                    err, retryable=True)


class RemoteDsh:
    """远端 dsh web 的探测 / 启动 / 停止。

    只碰我们自己建的状态目录（`~/.dsh-remote`），不读 DSH 自己的私有日志，
    也不去动我们没启动的进程。
    """

    def __init__(self, host: SshHost, shell: RemoteShell | None = None):
        self.host = host
        self.shell = shell or RemoteShell(host)

    def probe(self) -> dict:
        return self.shell.run(probe_script(), timeout=20)

    def ensure_running(self, force_restart: bool = False,
                       timeout: int = DEFAULT_START_TIMEOUT,
                       on_detail=None) -> RemoteEndpoint:
        """有可复用的实例就复用，否则启动一个。

        "可复用"要同时满足：进程活着（且启动时间对得上）、启动命令一致、
        日志里还有带令牌的地址。任何一条不满足都不能盲目复用——复用错了
        等于把用户接到一个配错的实例上。
        """
        if not force_restart:
            info = self.probe()
            if "foreign" in info:
                # 有人占着 web.pid，但我们认不出是不是自己起的（系统 ps 能力不足、
                # 或那个号被系统分配给了别的进程）。**不动手**：既不删状态，也不 kill。
                raise _err(
                    "shared",
                    "远端有一个进程占着我们上次记录的 pid %s，但身份对不上。"
                    "为避免误杀别人的进程，这次不做任何操作。" % info.get("foreign", ""),
                    detail="请登录那台机器确认后，手动删除 ~/.dsh-remote/web.pid。")
            pid = int(info.get("alive") or 0)
            if pid:
                same = (info.get("cmd") or "").strip() == self.host.launch_command
                port, token = _url_of(info.get("url") or "")
                if same and port and token:
                    if on_detail:
                        on_detail("复用远端已运行的实例（端口 %d）" % port)
                    return RemoteEndpoint(port=port, token=token, pid=pid,
                                          reused=True,
                                          home=info.get("home") or "",
                                          process_start=info.get("start") or "")
                raise _err(
                    "shared",
                    "远端有一个我们上次启动的实例，但它和当前配置不一致"
                    "（启动命令或端口变了）。重启远端服务才能继续。",
                    detail="远端记录的命令：%s\n当前配置：%s"
                           % (info.get("cmd") or "（空）", self.host.launch_command))

        if on_detail:
            on_detail("正在远端启动 dsh web…")
        info = self.shell.run(start_script(self.host.launch_command, timeout),
                              timeout=timeout + 30)
        if "shared" in info:
            raise _err("shared", "远端已有一个实例在运行（本次是按重启发起的，"
                                 "但它没停下来）。请先在面板里停止远端服务。")
        if "foreign" in info:
            raise _err("shared",
                       "远端有个进程占着我们上次记录的 pid %s，但身份对不上，"
                       "已停止操作以免误杀。" % info.get("foreign", ""),
                       detail="请登录那台机器确认后，手动删除 ~/.dsh-remote/web.pid。")
        port, token = _url_of(info.get("url") or "")
        if port and token:
            return RemoteEndpoint(port=port, token=token,
                                  pid=int(info.get("started") or 0),
                                  reused=False,
                                  home=info.get("home") or "",
                                  process_start=info.get("start") or "")
        if "nodsh" in info:
            raise _err("no_dsh")
        if "nodir" in info:
            raise _err("config", "远端 $HOME 下无法创建 .dsh-remote 目录。")
        if "timeout" in info:
            raise _err("start_timeout", detail=info.get("log") or "")
        if "exited" in info:
            raise _err("exited", detail=info.get("log") or "")
        raise _err("start_timeout",
                   "远端 dsh web 没有打印出可用地址。", detail=info.get("log") or "")

    def stop(self) -> str:
        """停掉我们启动的实例。返回 done（停掉了）/ notours / nothing。"""
        info = self.shell.run(stop_script(), timeout=30)
        for key in ("stopped", "notours", "nothing"):
            if key in info:
                return "done" if key == "stopped" else key
        return "nothing"


# ------------------------------------------------------------------ 隧道

def free_port() -> int:
    """向系统要一个当前空闲的 loopback 端口。

    拿到后立刻关掉，所以存在极小的竞争窗口——这正是下一步用
    `ExitOnForwardFailure=yes` 的原因：真被抢了，ssh 会立刻退出而不是
    假装建好了隧道，我们再换一个端口重试。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _dsh_answering(port: int, timeout: float = 3.0) -> bool:
    """本地这个端口上是不是已经有一个 DSH 在应答。

    光"能连上 TCP"不够：`ssh -L` 是懒连接，本地监听成功不代表远端通。
    发一个最小的 HTTP 请求，看到 401/200/30x 才算真的打通了。
    Host 用本地端口——链路自洽，这也正是浏览器会发的那个值。
    """
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return False
    try:
        s.sendall(("GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "Connection: close\r\n\r\n" % port).encode())
        head = s.recv(256)
        return head.startswith((b"HTTP/1.1 401", b"HTTP/1.1 200", b"HTTP/1.1 30"))
    except OSError:
        return False
    finally:
        s.close()


class Tunnel:
    """`ssh -N -L` 子进程。生命周期由我们管，退出时一定收掉。"""

    def __init__(self, host: SshHost, local_port: int, remote_port: int,
                 ssh: str | None = None):
        self.host = host
        self.local_port = local_port
        self.remote_port = remote_port
        self.ssh = ssh or find_ssh()
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 25) -> None:
        args = self.host.base_args(self.ssh) + [
            "-N",
            "-L", "127.0.0.1:%d:127.0.0.1:%d" % (self.local_port, self.remote_port),
            # 绑定失败立刻退出，而不是静默地"监听成功但不转发"
            "-o", "ExitOnForwardFailure=yes",
            # 保活：15 秒一次，连续 2 次没回应才认为断了（避免单次抖动）
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2",
            self.host.target,
        ]
        try:
            self.proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW)
        except OSError as e:
            raise _err("no_ssh", "无法启动 ssh：%s" % e)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = ""
                try:
                    err = (self.proc.stderr.read() or b"").decode("utf-8", "replace")
                except OSError:
                    pass
                raise RemoteShell._classify(self.proc.returncode or -1, err.strip())
            if _dsh_answering(self.local_port, timeout=0.6):
                return
            time.sleep(0.3)
        self.close()
        raise _err("tunnel",
                   "隧道建起来了，但本地端口 %d 上等不到远端 DSH 应答。"
                   % self.local_port,
                   "通常是远端进程刚好退出，或端口转发被服务端策略限制。",
                   retryable=True)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass


# ------------------------------------------------------------------ 一次连接

class RemoteSession:
    """一次远程连接：探测/启动远端服务 → 建隧道 → 给出可直接加载的地址。

    对界面层暴露的只有 `url` 和 `close()`——加载流程和本地完全一致，
    `_finalize_url()`、令牌断言那些既有防线全都继续生效。
    """

    def __init__(self, host: SshHost, ssh: str | None = None):
        self.host = host
        self.ssh = ssh or find_ssh()
        self.shell = RemoteShell(host, self.ssh)
        self.dsh = RemoteDsh(host, self.shell)
        self.tunnel: Tunnel | None = None
        self.endpoint: RemoteEndpoint | None = None
        self.url = ""

    def connect(self, force_restart: bool = False, on_stage=None) -> str:
        def stage(text: str) -> None:
            if on_stage:
                on_stage(text)

        self.host.validate()
        stage("正在连接 SSH…")
        endpoint = self.dsh.ensure_running(
            force_restart=force_restart,
            on_detail=lambda d: stage(d))
        self.endpoint = endpoint

        stage("正在建立隧道…")
        last: RemoteError | None = None
        for _ in range(PORT_ATTEMPTS):
            local = free_port()
            tunnel = Tunnel(self.host, local, endpoint.port, self.ssh)
            try:
                tunnel.start()
            except RemoteError as e:
                last = e
                # 只有"端口被抢"值得换一个再试；认证/网络问题换端口没用
                continue
            self.tunnel = tunnel
            self.url = "http://127.0.0.1:%d/?token=%s" % (local, endpoint.token)
            stage("已连接 · 端口 %d" % local)
            return self.url
        self.tunnel = None
        raise last or _err("tunnel", "反复分配本地端口都失败了。", retryable=True)

    def close(self, stop_remote: bool | None = None) -> None:
        """收尾。

        `stop_remote` 为 None 时按主机的 `keep_service` 决定；`ask` 当作"保留"
        ——真正的询问必须发生在界面上，模型层不替用户做决定。
        """
        self.tunnel_close()
        want_stop = (self.host.keep_service == KEEP_STOP
                     if stop_remote is None else stop_remote)
        if want_stop:
            try:
                self.dsh.stop()
            except RemoteError:
                pass
            # 停远端时把本地状态也清掉：下次连是全新的，避免误认旧 pid
            self.endpoint = None
            self.url = ""

    def tunnel_close(self) -> None:
        if self.tunnel is not None:
            self.tunnel.close()
            self.tunnel = None


# ------------------------------------------------------------------ 自测钩子

def selftest_checks() -> list[str]:
    """纯逻辑断言（不联网、不执行 ssh），给 `tools/contract_selftest.py` 用。"""
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                               ("  " + extra) if extra else ""))
        if not ok:
            failures.append(name)

    # 脚本生成：先确认没有转义过头（`%%` 是 Python 的转义产物，出现在最终脚本里
    # 说明某一层写错了），再交给本地 sh 做一次**语法**检查——`-n` 只解析不执行，
    # 能把"少写一个 fi / done"这类错误挡在联网之前。
    p = probe_script()
    s = start_script("dsh web", 30)
    t = stop_script()
    sh_bin = shutil.which("sh")
    for name, text in (("probe", p), ("start", s), ("stop", t)):
        check("%s 脚本没有转义残留" % name, "%%" not in text)
        check("%s 脚本带结构化前缀" % name, TAG in text)
        if sh_bin:
            try:
                proc = subprocess.run([sh_bin, "-n"], input=text.encode("utf-8"),
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=15)
                ok = proc.returncode == 0
                extra = proc.stderr.decode("utf-8", "replace").strip()[:80]
            except (OSError, subprocess.SubprocessError) as e:
                ok, extra = False, str(e)[:80]
            check("%s 脚本 sh 语法通过" % name, ok, extra)
        else:
            check("%s 脚本 sh 语法通过" % name, False, "本机找不到 sh")

    check("启动脚本追加三个必要参数",
          "--host 127.0.0.1 --port 0 --no-open" in s)
    check("启动脚本用 nohup 三重定向",
          "</dev/null" in s and '>>"$D/web.log" 2>&1 &' in s)
    check("启动脚本会截断旧日志", ': > "$D/web.log"' in s)
    check("停止脚本核对启动时间后才 kill",
          "_st" in t and "ours" in t)
    check("脚本用 pid+启动时间做身份（防 PID 回收）",
          "ours()" in p and 'web.st' in p)
    check("自定义命令的单引号被转义",
          "'\\''" in start_script("dsh web --patch 'a b.yml'", 30))

    # 解析
    sample = ("Welcome to Ubuntu\n"
              "Last login: Thu Nov 12\n"
              "%s home /home/cx\n"
              "%s alive 4242\n"
              "%s url dsh web: http://127.0.0.1:3080/?token=abc-DEF_123.4\n"
              % (TAG, TAG, TAG))
    got = parse_tagged(sample)
    check("只认带前缀的行（banner 被忽略）",
          got.get("home") == "/home/cx" and got.get("alive") == "4242",
          str(got))
    port, token = _url_of(got.get("url", ""))
    check("从日志行解出端口与令牌", port == 3080 and token == "abc-DEF_123.4",
          "%s / %s" % (port, token))
    check("没有令牌行时返回 0", _url_of("dsh web: http://127.0.0.1:3080/") == (0, ""))

    multi = "%s log 第一行\n%s log 第二行\n" % (TAG, TAG)
    check("多行日志按顺序合并",
          parse_tagged(multi).get("log") == "第一行\n第二行")

    # 主机配置
    h = SshHost(name="家的服务器", host="frp-six", user="cx", port=2222)
    check("别名/端口进 ssh 参数", "-p" in h.base_args("ssh") and "2222" in h.base_args("ssh"))
    check("BatchMode 打开（不交互挂住）",
          any("BatchMode=yes" == a for a in h.base_args("ssh")))
    check("不关闭主机密钥校验",
          any(a.startswith("StrictHostKeyChecking=") and "no" != a.split("=")[1]
              for a in h.base_args("ssh")))
    check("带 user 时拼成 user@host", h.target == "cx@frp-six", h.target)
    check("默认端口 22 不覆盖 config 别名", "-p" not in SshHost(host="x").base_args("ssh"))

    bad = SshHost(host="x", launch_command="dsh web")
    try:
        bad.validate()
        check("默认命令可以通过校验", True)
    except RemoteError as e:
        check("默认命令可以通过校验", False, str(e))

    bad = SshHost(host="x", launch_command="mycmd --port 1")
    try:
        bad.validate()
        check("缺参数的启动命令被拒绝", False)
    except RemoteError as e:
        check("缺参数的启动命令被拒绝", e.kind == "config", e.message[:40])

    import json as _json
    round_trip = SshHost.from_json(_json.loads(_json.dumps(h.to_json())))
    check("主机配置能往返序列化",
          round_trip is not None and round_trip.to_json() == h.to_json())

    check("没有凭据字段落盘",
          not any(k in h.to_json() for k in ("password", "passphrase", "token")))

    check("keep_service 非法值回落到 ask",
          SshHost(host="x", keep_service="whatever").keep_service == KEEP_ASK)
    return failures



