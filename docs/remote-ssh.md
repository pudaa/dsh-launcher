# 远程 DSH：用 SSH 把远端的 loopback 借到本机

> 调研于 2026-11-12，对象是社区里两个已经实现了远程的项目。
> 结论先行：**路子对，而且比它们俩都简单——我们不需要网关。**

## 1. 调了两个项目

| 项目 | 形态 | 远程做法 | 许可证 |
|---|---|---|---|
| [`ccch1mneyyy/dsh-go`](https://github.com/ccch1mneyyy/dsh-go) | Android 客户端（Dart/Flutter） | 自建 SSH 库 + 自建 HTTP 客户端 | AGPL-3.0 |
| [`dsh-tauri/deepseek-harness-desktop`](https://github.com/dsh-tauri/deepseek-harness-desktop) | Tauri 桌面端（TS/Rust） | `ssh2` 库 + 本机反向代理网关 | MIT + 非商业附加条款 |

两边都做到了我们 9 月设计里想做的事：**远端 DSH 只听 127.0.0.1，靠 SSH 把它的 loopback 端口搬到本机**。
没有一家去改 DSH 的绑定，也没有一家让 DSH 监听外网。我们的安全底线和它们一致。

它们的实现里有 8 个细节值得直接吸收（见 §4），但也有一个关键差异让我们的方案可以更简单（见 §3）。

## 2. 两边各自的流程

**dsh-go（手机）**
`SSH 连接 → 校验主机密钥 → 远端启动或复用 dsh web → 探一次隧道 → 本机 loopback 监听 → HTTP 登录 → 15s 保活`

**dsh-tauri（桌面）**
`SSH 连接（支持 ProxyJump 多跳）→ 校验主机密钥 → 远端 bootstrap → 本机网关监听 → 网关改写 Host/cookie → WebView`

dsh-tauri 之所以要那一层网关，是因为它还要支持**反方向**：把本机 DSH 分享到局域网或公网隧道（它内置了 cloudflared）。
一旦要对外服务，就必须统一处理 Host/Origin/跨站/cookie 铸造。**这个方向我们明确不做**，所以那层网关对我们没有价值。

## 3. 关键实测：DSH 到底要求什么样的 Host

我原本担心一件事：通过 `ssh -L` 访问时，浏览器发出的 `Host` 是 `127.0.0.1:<本地端口>`，
而远端 DSH 监听的端口可能不同。如果 DSH 的栅栏比对 host:port，那我们的方案就得让两个端口相等，
或者加一层改写 Host 的代理——这会把复杂度抬上去一个量级。

实测（`build/probe_host_fence.py`、`build/probe_authority_binding.py`，在临时 DSH_HOME 里起真服务）：

| 请求 | Host 头 | 结果 |
|---|---|---|
| 根路径 + 令牌 | `127.0.0.1:<真实端口>` | 303 |
| 根路径 + 令牌 | `127.0.0.1:<别的端口>` | **303** |
| 根路径 + 令牌 | `evil.example.com:<真实端口>` | **303** |
| `/api` + 该端口签发的 cookie | `127.0.0.1:<其他端口>` | **404（通过）** |
| `/api` + cookie 换成另一个 Host | `127.0.0.1:<真实端口>` | 401 |
| `/api` + cookie | `evil.example.com` | 403 |

读数：

1. **根路径只认令牌，不认 Host。**
2. **`/api` 的栅栏只要求 Host 是 loopback 字面量**（`127.0.0.1` / `localhost` 都行，域名一律 403）。
   它**不比对端口**。
3. 401 不是栅栏拒绝，而是 **cookie 绑定签发时的 authority**：cookie 在 `127.0.0.1:A` 下签发，
   拿它去请求 `127.0.0.1:B` 就是 401。
4. 只要**整条链路的 Host 自洽**（浏览器看到什么，DSH 就收到什么），一切正常。

**结论：`ssh -L` 的 TCP 转发天然自洽——它不改 HTTP，浏览器发 `127.0.0.1:<本地端口>`，
DSH 收到的还是它，cookie 也在这个 authority 下签发。端口不必相等，不需要 trusted-host，
不需要改写 Host 的代理。**

这一条修正了 9 月文档里"天然通过信任栅栏"的说法：那句话成立，但成立的原因是"端到端自洽"，
而不是"DSH 不校验 Host"。差别在于——**自洽一旦被破坏（比如中间加了改写 Host 的代理），
就必须把 cookie 也一起搬**，dsh-tauri 的 `mintedCookies` 就是在做这件事。

## 4. 值得吸收的 8 个细节

按"不做会出问题"的程度排序。

### 4.1 远端状态目录，而不是自己猜 PID

dsh-go 在远端建 `~/.dsh-app-remote/`（mode 700），四个文件各司其职：

| 文件 | 作用 |
|---|---|
| `web.pid` | 它启动的 `dsh web` 的 pid |
| `web.log` | stdout/stderr，**每次启动截断**，所以最后一条 `token=` 行必然属于当前进程 |
| `web.cmd` | 启动命令；命令变了要求显式重启（不静默替换） |
| `web.st` | 进程启动时间 |

`web.st` 是关键：**PID 会被回收**，重启后的另一个进程可能正好拿到这个号。
只 `kill -0` 判断"还活着"会误判、甚至误杀别人的进程。它把「pid + 启动时间」当复合身份。

### 4.2 服务活过 SSH 会话

启动时 `nohup ... </dev/null >>web.log 2>&1 &`，三个重定向缺一不可：
`</dev/null` 断开 stdin，`>>log` 接管 stdout/stderr。少一个，SSH 会一直挂着等子进程释放管道
（dsh-go 为此专门把"启动脚本"和"普通命令"分成两套执行契约）。

### 4.3 从日志里拿 URL，而不是从 stdout 解析

因为服务是 nohup 在后台跑的，URL 只能从日志里捞：`grep -E 'https?://[^ ]*[?&]token=' | tail -1`。
配合"每次启动前截断日志"，`tail -1` 就一定是当前进程的地址。

### 4.4 结构化输出加前缀

SSH 登录会打印 banner、`~/.profile` 的杂音、`Last login:` 之类。直接解析 stdout 会误判。
dsh-go 给每行结果加 `DSHAPP` 前缀，只认带前缀的行。**这个必须学**，否则线上一定会遇到
"某台机器的 shell 打了个提示，我们就解析错了"。

### 4.5 非交互 shell 的 PATH

`ssh host 'cmd'` 拿到的是**非交互非登录** shell，`~/.bashrc` 里的 PATH 追加往往不生效，
于是"明明装了 dsh 却说 command not found"。dsh-go 的做法：先 `command -v dsh`，
找不到就依次探 nvm / volta / bun / pnpm / Homebrew / `~/.local/bin` 等常见位置，
找到就临时塞进 PATH；实在找不到才报错。

它还用 `sh -lc`（登录 shell）执行，让 `~/.profile` 生效。顺带处理了 fish 在单引号里
解释反斜杠的坑——引号要留在引号外。

### 4.6 两种模式：托管 vs 只连

- **managed**：我们负责在远端启动/复用 `dsh web`，读它的日志、管它的 pid。
- **attachOnly**：远端已经有别人在跑（用户自己开的，或另一台机器共享给你），
  我们**不执行任何命令**，只用固定端口 + 用户提供的 token 建隧道。

后者很实用：有些机器没有 SSH 权限、或者用户不想让我们碰它的进程，只连就够。

### 4.7 主机密钥 TOFU

首次连接把 `SHA256:...` 指纹摆给用户看，接受就钉住；之后每次比对，**变了就硬失败并提示**
（典型的中间人信号），不允许"忽略一次"。换地址/换端口时自动清空钉住的密钥（它属于旧机器）。

`ssh.exe` 自带 `known_hosts` 和这套流程，我们不用自己实现，但**要保证不把
`StrictHostKeyChecking` 关掉**——那是这类工具最常见的自毁式"便利"。

### 4.8 保活与重连

dsh-go：15s 一次 ping，10s 超时，**连续 2 次失败**才判定掉线（避免单次抖动），
之后进入退避重连，UI 显示"第几次重试 / 下次重试时间"。
`ssh.exe` 侧等价物是 `ServerAliveInterval=15` + `ServerAliveCountMax=2`。

重连时**保持本地监听端口不变**——dsh-go 的 `LocalForwarder` 让 listener 活过单条 SSH 连接，
这样客户端的 base URL 不用变。（我们用 `ssh -L` 时，重连要重新绑同一个本地端口。）

## 5. 明确不做的

- **不抄代码**。dsh-go 是 AGPL-3.0，抄进我们的 MIT 项目会传染整个仓库；
  dsh-tauri 是 MIT + 非商业附加条款，同样不能直接搬。上面这些是**设计思路**，实现全部自己写。
- **不做公网分享**（dsh-tauri 的 inbound 方向 + cloudflared）。这与我们的硬原则冲突：
  绝不让 DSH 监听外网。远程只做"我去连别人"，不做"别人来连我"。
- **不加改写 Host 的网关**。§3 已证明不需要；加它只会引入 cookie 铸造、跨站检查、
  压缩透传这一整套本来不存在的问题。
- **不做多跳 ProxyJump**。`ssh.exe` 用用户的 `~/.ssh/config` 天然支持 `ProxyJump`，
  我们只要把 `Host` 别名透传给它就行，不重复实现。

## 6. 我们的方案

### 6.1 技术栈：复用系统 OpenSSH

Windows 10+ 自带 `C:\Windows\System32\OpenSSH\ssh.exe`，本机 `~/.ssh/` 里已经有 `config`
和 `id_ed25519`（现有两条 frp 主机配置）。**复用它，而不是打包 paramiko**：

- 与"复用用户已有 DSH"同一套产品哲学：复用户已有的密钥、agent、known_hosts、config 别名
- 不用在 exe 里塞 SSH 实现，也不用自己存密码
- 主机密钥校验、多跳、agent 全白送

代价：**Phase 1 只支持密钥/agent 认证**，密码认证要交互，不自动化。
失败时给明确指引（"把这台机器的公钥加进 authorized_keys"），而不是转去存明文密码。

### 6.2 启动与连接

```
1. ssh -o BatchMode=yes -o ConnectTimeout=10  <host> 'sh -lc <探测脚本>'
      → 远端有没有 sh / dsh / 有没有我们上次启动的实例
2. 需要启动时：ssh <host> 'sh -lc <启动脚本>'
      脚本内部 nohup 起 <cmd> --host 127.0.0.1 --port 0 --no-open，轮询日志直到出 URL，然后退出
      → 拿回 {port, token, pid, 启动时间, home}
3. 本地取一个空闲端口（bind(0) 拿号后释放）
4. ssh -N -L 127.0.0.1:<本地端口>:127.0.0.1:<远端端口> \
        -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=2 <host>
5. 把 http://127.0.0.1:<本地端口>/?token=<远端令牌> 交给现有的加载流程
```

第 5 步是关键收益：**远程和本地走完全相同的"拿到带令牌的 loopback URL → 加载"路径**，
WebView 侧一行代码都不用改，`_finalize_url()` / 令牌断言那些既有防线全部继续生效。

### 6.3 分阶段

**Phase 1（骨架，先能用）**
- 主机配置：别名或 host/user/port；直接框选 `~/.ssh/config` 里的现成条目
- **启动命令可配置**（默认 `dsh web`）；添加主机时校验它接受 `--host / --port / --no-open`
- 一条 SSH 隧道 + 远端启动 + 加载
- 状态：连接中 / 隧道已建 / 已加载 / 掉线重试中
- 断开时按该主机的配置决定远端服务留不留（见 §7），能复用就复用、认不出就不动

**Phase 2（可靠性）**
- 保活与退避重连（复用本地端口）
- 主机密钥变更的硬失败提示
- 错误分类：无 sh / 无 dsh / 启动超时 / 端口不通 / 令牌被拒
- 多 target 各一窗口

**Phase 3（可选）**
- attachOnly 模式（只连，不启动）
- 密码认证（如确有需要，再评估 paramiko 的取舍）

### 6.4 面板落点

「运行方式」页现有的"远程 DSH（规划中）"卡片换成真实内容：主机列表 + 连接状态 + 添加主机。
文案底线不变——**我们不会让 DSH 监听外网**。

### 6.5 实现状态（2026-11-12，Phase 1 首刀）

已完成：

| 部分 | 位置 |
|---|---|
| 主机配置（无凭据）/ JSON 读写 | `dsh_host/remote.py`：`SshHost`、`load_hosts`、`save_hosts` |
| 远端探测 / 启动 / 停止脚本 | 同上：`probe_script` / `start_script` / `stop_script` |
| 结果解析（前缀过滤） | 同上：`parse_tagged`、`_url_of` |
| SSH 执行 | 同上：`RemoteShell`（`sh -l -s` 走 stdin） |
| 隧道 | 同上：`Tunnel`（`ssh -N -L` + 端口随机 + 401 探测） |
| 一次连接 | 同上：`RemoteSession.connect() → url` |
| 面板 UI | `dsh_panel.py`：`RemoteHostRow`、`RemoteHostDialog`、`RunModePage._build_remote_card` |
| 主窗口接线 | `dsh_gui_qt.py`：`RemoteConnectWorker`、`_panel_remote_connect/_disconnect` |
| 远程态分流 | `ServiceHandle.remote`：停服务、菜单、版本复验按它分叉 |

验证：

- `contract_selftest.py [L4]` 29 条纯逻辑断言（**挂在 CI 会跑的 LITE 分支**）：
  三段脚本的 `sh -n` 语法、前缀解析、URL 提取、ssh 参数、配置往返、无凭据落盘。
- `remote_script_smoke.py`：本地 sh 扮演远端，跑真启动 → 状态文件 → 停止，10 项通过。
- 面板自测新增 7 条：主机逐台列出、名址列对齐、连接中按钮禁用、断开在连接中不可点、
  连上后状态同步。

**尚未验证**：复用判据（识别"上次启动的实例还在跑"）没能在真实 POSIX 语义下跑通——
本机 MSYS 的 PID 与 Windows 不同、`/proc` 会被快速复用，WSL 服务异常，没有容器运行时。
首次真机连接时日志会打印"复用已运行的实例"或"本次启动"，以此确认。

## 7. 已定：断开后远端服务留不留

**交给用户选，并且能设默认值。**（2026-11-12 老大定）

两种场景对这件事的要求正好相反：

- 连的是**服务器**，上面可能有一个跑了几十分钟的复杂任务 → 断开就杀等于毁掉工作，
  必须保留，下次连上去接着看。
- 连的是**临时机器**（远程协助、借台机器查个 bug）→ 留着就是白占那台机器的内存，
  而且我们很可能再也不连第二次了。

所以：

| 层级 | 行为 |
|---|---|
| 全局默认（设置页） | 保留 / 停止 / 每次询问，三选一 |
| 单台主机（主机列表） | 可覆盖全局默认——服务器常留、临时机器常停 |
| 断开时 | 按该主机的生效值执行；选"每次询问"则弹一次轻提示 |

**但"保留"有个前提：下次必须真的能复用。** 只做到"不杀"是不够的——
留一个我们认不出来的孤儿 `dsh web`，下次连接又起一个新的，那才是真的留垃圾。
所以保留状态必须落在远端的**状态目录**里（见 §4.1）：

```
~/.dsh-remote/web.pid    pid
~/.dsh-remote/web.st     进程启动时间（PID 会回收，必须配它做复合身份）
~/.dsh-remote/web.log    stdout/stderr，每次启动截断，末条 token= 即当前地址
~/.dsh-remote/web.cmd    启动命令，变了要求显式重启而不是静默顶掉
```

下次连接先 probe：pid 活着 + 启动时间对得上 + 命令一致 + 日志里有 token → 直接复用，不平白等一次冷启动。
任何一项对不上就明确告诉用户"远端有个认不出的实例"，让他决定杀掉还是忽略，不擅自 `kill`。

## 8. 远端的形态：web 还是 tui

DSH 有两种可跑形态，差别很大：

| | `dsh web` | `dsh-TUI`（社区插件，MIT） |
|---|---|---|
| 远端占用 | 常驻 HTTP 服务 + 前端资源 | 纯终端进程，无 Web 服务、无前端 |
| 客户端要求 | 需要能跑浏览器的东西（我们的 WebEngine，500 MB 起） | 任意终端 |
| 认证 | 令牌 + cookie，要处理 Host 栅栏 | SSH 本身就够，**不需要端口转发** |
| 交互 | 完整 Web UI，可视化强 | 纯键盘，Claude Code 风格 |

**它的真实价值在远端**：小 VPS（1–2 GB 内存）上，`dsh web` 的 node 进程要 600 MB 上下，
而 TUI 形态没有 Web 服务和前端资源，压力小得多。老大的判断是对的。

但对我们这个壳来说有个硬限制：**壳的显示栈是 QtWebEngine，它渲染不了 ANSI 全屏 TUI。**
要在壳里显示交互式 TUI，需要一整套终端能力：

- Windows 上的 ConPTY（伪终端）
- 完整 ANSI 模拟（TUI 用 alt-screen + 大量光标定位，不是"给日志加颜色"）
- 键盘输入转发、窗口 resize、复制粘贴、搜索

现有的 `dsh_console.py` 帮不上忙——它是**只读的 stdout 采集**（fd 重定向 + 环形缓冲 + 按行着色），
没有 PTY、没有输入通道、也不做光标定位。等于要重写一个终端控件。
（用第二个 WebEngine 实例跑 xterm.js 也不成立：每实例 +100~300 MB，正好和"低占用"相反。）

所以分两步走：

1. **远端启动命令做成可配置**（照 dsh-go 的 `launchCommand` 设计，默认 `dsh web`）。
   架构上先不把"web"写死，将来接别的形态不用改流程。managed 模式启动时统一追加
   `--host 127.0.0.1 --port 0 --no-open`——自定义命令必须接受这三个参数，这一点在添加主机时校验。
2. **壳只渲染 web**。TUI 作为独立议题评估：需要终端控件，成本约等于一个新模块，
   建议等远程 Web 上线、看真实需求再定。想用小机器跑 TUI 的用户，现阶段可以直接
   `ssh` 上去用——它本来就不需要端口转发，也不需要我们。

## 9. 已定：远程窗口用谁的工作区

用**远端那台机器自己的**工作区与会话。远程窗口看到的是那台机器上的 DSH 的世界，
不共享本地的会话列表。

不做会话同步——跨机器同步对话历史的工作量远大于收益（两边 schema 要对齐、
冲突要处理、附件要搬），而用户对"我连的是那台机器"这件事本来就有直觉预期。

