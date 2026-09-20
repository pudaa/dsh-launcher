# DSH Launcher

给 [DeepSeek Harness](https://www.npmjs.com/package/@deepseek-ai/dsh) 套的 Windows 桌面壳。

一个 exe 搞定：双击打开就是原生窗口里的 DSH Web UI，不用敲命令、不用管端口和令牌。

## 它解决什么问题

DSH 官方只发 CLI。要用 Web UI 得自己开终端跑 `dsh web`，从输出里复制带 token 的长 URL 粘进浏览器；关掉终端服务就没了，重开又要来一遍。这个壳把这些事收进一个窗口：

- **不用记命令** —— 双击启动，服务自动拉起
- **不用复制 token** —— URL 自动从服务输出解析，直接加载
- **关窗不杀服务** —— 后台任务继续跑，下次打开直接接管
- **没装 DSH 也能用** —— 内置引导，用官方渠道装好 Node 和 DSH
- **更新有保障** —— 更新前比对数据指纹，不会碰你的会话和登录态

## 功能

| 功能 | 说明 |
|---|---|
| 一键启动 | 自动找 DSH、规划端口、拉起服务、加载 UI |
| 服务接管 | 端口上已有自家服务时直接接管，不重复启动 |
| 端口冲突处理 | 3080 被占且响应不像 DSH 时，可自动改用系统分配端口 |
| 环境自检 | 启动前诊断 Node / npm / DSH 三行状态 |
| 首次运行引导 | 缺 Node 走 winget 官方渠道安装；DSH 装到私有 prefix，不动全局环境 |
| DSH 更新 | 走官方 npm 通道，支持 latest / alpha 双通道与一键回归 |
| 桌面壳更新 | 从 GitHub Release 拉取新版本，自动替换自身并重启 |
| 数据安全 | 更新前后统计 DSH_HOME 指纹，记录异常减少 |
| 应用内反馈 | 手动操作的结果都落在窗口里，不依赖系统通知 |

### 托盘菜单

```
显示主窗口
准备运行环境…
────────────────
检查 DSH 更新…
加入预览计划
回归稳定版
回滚 DSH 到上一版本
────────────────
检查桌面壳更新…
关于 DSH Launcher
────────────────
打开日志目录
退出
```

两组更新刻意分开：**DSH 更新**换的是 DSH 程序（走 npm），
**桌面壳更新**换的是正在运行的 exe（走 GitHub Release）。
两者可以独立进行，互不影响。

「退出」会连带停止后台服务 —— 想留服务的话，把窗口关掉（最小化到托盘）而不是退出。

## 快速开始

### 用现成的 exe

到 [Releases](https://github.com/pudaa/dsh-launcher/releases) 下载 `DSH-Web.exe`，双击。

如果机器上还没装 Node 和 DSH，程序会弹出引导窗口，一步步装好。首次安装 DSH 需要下载 500 多个包，大约十几分钟，窗口里有进度。

### 从源码跑

```bash
pip install -r requirements.txt
python dsh_gui_qt.py
```

### 打包

```bash
python -m nuitka --onefile \
  --windows-console-mode=disable \
  --windows-icon-from-ico=assets/icon.ico \
  --enable-plugin=pyside6 \
  --include-package=dsh_host \
  --onefile-tempdir-spec={CACHE_DIR}/DSH-Web-runtime \
  --output-dir=build --output-filename=DSH-Web.exe \
  --lto=yes dsh_gui_qt.py
```

> 打包解释器建议用 python.org 的独立环境。conda 系环境会让 Nuitka 报 `flavor 'Anaconda Python'`，其 PySide6 插件枚举 conda 元数据时会 `KeyError: 'files'` 直接崩。

### 发布

版本号只在 `dsh_host/version.py` 里定义一次。改完提交，打 tag 即可：

```bash
# 1. 改 dsh_host/version.py 里的 HOST_VERSION
# 2. 提交并打 tag
git commit -am "release: v2.0.1"
git tag v2.0.1
git push origin main v2.0.1
```

GitHub Actions 会自动跑自测、构建 exe、创建 Release。
工作流会校验 tag 与源码版本号一致，不一致直接失败。

## 架构

分三层，越往下越了解 DSH 的内部细节。上层不许越过下层直接和 DSH 打交道。

```
┌─────────────────────────────────────────────┐
│  dsh_gui_qt.py        界面层                 │
│  Qt 窗口 / 托盘 / 引导 / 更新对话框           │
│  不出现任何 DSH 私有路径、文件名、日志格式     │
└──────────────────┬──────────────────────────┘
                   │ 只调 dsh_host 的公开函数
┌──────────────────▼──────────────────────────┐
│  L2  dsh_host/updater.py     DSH 更新事务    │
│      dsh_host/selfupdate.py  桌面壳自更新    │
│      dsh_host/provision.py   环境供给        │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  L1  dsh_host/compat.py      版本适配表      │
│  区间覆盖 + 外部 compat.json 覆盖             │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  L0  dsh_host/contract.py    契约层  ★唯一    │
│      启动 / 就绪探测 / URL 解析 / 停止        │
│      环境自检 / 数据指纹                      │
│  dsh_host/config.py          路径定义  ★唯一  │
│  dsh_host/version.py         版本定义  ★唯一  │
└─────────────────────────────────────────────┘
```

**为什么要这样分。** DSH 是 developer preview，官方 README 明示会有破坏性更新。任何"赌某个内部细节不变"的写法都会在下次更新时炸掉。所以对 DSH 的所有假设都收敛在 L0 一个文件里，并且带探测顺序和降级路径。

### 模块职责

| 文件 | 职责 |
|---|---|
| `dsh_host/config.py` | **所有路径的唯一权威定义处**。其余模块 import，禁止自行拼 `LOCALAPPDATA` |
| `dsh_host/version.py` | **桌面壳版本号的唯一定义处**。发布脚本与界面都从这里读 |
| `dsh_host/contract.py` | L0 契约层。DSH 安装探测、服务启动、就绪等待、URL 解析、环境自检、数据指纹 |
| `dsh_host/compat.py` | L1 版本适配表。按 `major.minor.patch` 代数比较（DSH 所有 release 都是预发布，严格 semver 序会匹配不到） |
| `dsh_host/updater.py` | L2 DSH 更新事务。停服 → 取指纹 → 安装 → 校验 → 比对指纹 → 重启，失败自动回滚 |
| `dsh_host/selfupdate.py` | L2 桌面壳自更新。从 GitHub Release 下载、校验、生成替换脚本 |
| `dsh_host/provision.py` | L2 首次运行环境供给。winget 装 Node、私有 prefix 装 DSH |
| `dsh_gui_qt.py` | 界面层。PySide6 + QtWebEngine |

## 目录规划

运行时数据分两处，互不干扰：

```
%LOCALAPPDATA%\DSH-Web\              桌面壳数据根（配置与登录态）
    settings.json / compat.json / install.json / service.json
    logs\{dsh-web.log, host.log}
    profile\                         QtWebEngine 登录态
    node-global\                     私有 npm prefix

%LOCALAPPDATA%\DSH-Web-runtime\      Nuitka onefile 解包目录（纯缓存）
```

拆开的原因：以前这两者是同一个目录，清缓存（排障标准动作）会连带丢掉登录态和配置。现在删 `DSH-Web-runtime\` 永远安全，删 `DSH-Web\` 才会丢配置。

**桌面壳自有数据永不写进 `DSH_HOME`。** `DSH_HOME` 是 DSH 的地盘，我们只读不写（唯一例外是清理陈旧的 `.credentials.yaml.lock`）。

## 开发

### 自测

DSH 每次更新后跑契约层自测，能定位破坏性变更是落在哪一环：

```bash
python tools/contract_selftest.py
```

无头 GUI 自测（不需要显示器）：

```bash
python tools/gui_selftest.py
```

### 日志

`%LOCALAPPDATA%\DSH-Web\logs\`：

- `host.log` —— 桌面壳自身的操作记录（启动、检查更新、安装、回滚）
- `dsh-web.log` —— DSH 服务进程的原始输出

## 已知限制

- **只支持 Windows。** 服务生命周期管理用了 `DETACHED_PROCESS`、winget、注册表读 PATH。
- **应用被强杀时服务会一起终止。** `DETACHED_PROCESS` 只脱离控制台，不脱离 Job Object。正常关窗退出则服务存活。
- **回滚不保证数据格式可逆。** 自动回滚保护的是"能启动"，不是"会话可读"。旧版本可能打不开新版本写出的会话记录 —— 记录不会被删除，只是旧版本读不了。

## 许可

待定。
