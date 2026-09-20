# DSH 桌面壳设计说明

面向"DSH 处于 developer preview、每代都可能破坏兼容"这一前提做的适配层改造。
最后更新：2026-09-18

---

## 1. 分层职责

```
L2  dsh_host/updater.py    更新器      只走官方 npm 通道
    dsh_host/provision.py  环境供给    首次运行补齐 Node 与 DSH
L1  dsh_host/compat.py     适配表      版本区间 → 探测顺序与降级策略（数据）
L0  dsh_host/contract.py   契约层      唯一允许知道 DSH 内部结构的地方
    dsh_gui_qt.py          界面        只做界面 + 流程编排
```

**铁律：L0 以上的代码不得出现 DSH 的私有路径、私有文件名、私有日志格式。**
新增功能时如果发现需要知道 DSH 内部细节，那说明该加一个契约能力，而不是在界面里写 `if`。

---

## 2. 六处硬编码耦合点的迁移对照

| # | 改造前 | 风险 | 改造后 |
|---|---|---|---|
| 1 | `DSH_HOME = r"D:\AppData\dsh"` 写死 | 忽略环境变量，换机即失效 | `resolve_home()`：配置文件 → 环境变量 → `~/.dsh`，全不命中则交给 DSH 自己决定 |
| 2 | 扫 PATH 找 `node_modules/@deepseek-ai/dsh/lib/bin.js` | 只认 npm 全局扁平布局 | `resolve_install()` 四路探测：环境变量/配置 → PATH 上的 `dsh` 包装 → `npm prefix -g` → 已知回退；命中结果缓存到 `install.json` |
| 3 | `PORT = 3080` 写死 | 端口冲突整类故障 | 3080 优先；被占用时先做 HTTP 指纹判断是不是自家服务，是则接管，否则降级 `--port 0` |
| 4 | 从 DSH 日志正则抓 `token=` | 0.1.6 恰好改了启动诊断输出 | 读**我们自己 spawn 的子进程 stdout**，解析官方输出行 `dsh web: <url>` |
| 5 | `netstat -ano` 文本解析停服 | 状态词受系统语言影响 | `stop()`：自己记录的 PID 优先，端口反查兜底；反查按列位置取 PID，不匹配状态词，并对映像名做校验 |
| 6 | 写死 `.credentials.yaml.lock` | 私有文件名耦合 | 收进适配表 `stale_locks`，降级为"可选优化"，清不掉也不影响启动 |

---

## 3. 关键发现：官方稳定契约

实测（0.1.5-rc.2）确认了四个可以放心依赖的官方接口：

| 接口 | 实测输出 | 用途 |
|---|---|---|
| `dsh --version` | `0.1.5-rc.2` | 版本探测，比读 `package.json` 稳 |
| `dsh web --port 0` | 由系统分配空闲端口 | 消除端口冲突 |
| `dsh web` stdout | `dsh web: http://127.0.0.1:1744/?token=xxxx` | **端口 + 令牌一次拿全** |
| 裸 `GET /` | `401` + `dsh web authentication required` | 识别端口上是不是自家服务 |

第 3 条是本轮最有价值的发现：它把"读 DSH 私有日志 + 正则抓令牌 + 硬编码端口"三件事一起消掉了。
这一行是官方对用户可见的输出（401 提示语本身就说 "reopen the URL printed by dsh web"），
比日志内部格式稳定得多。

### 3.1 时序陷阱（已修，勿回退）

**症状**：服务就绪但 `url` 为空字符串，首屏直接是 401 页。

**原因**：DSH 写 `dsh web: <url>` 与开始监听几乎同一瞬间。若就绪判定只看 TCP，
就会在读到 URL 之前返回"就绪"，拿到空 URL。

**处置**：`wait_ready()` 在 TCP 判定就绪后，还必须走一遍 `_finalize_url()`——
在 `url_grace`（默认 3s）内补读 URL 行；仍拿不到则兜底为裸 URL，**绝不返回空串**。

这个缺陷是概率性的（取决于轮询相位），静态检查和单次运行都看不出来，
是在真机跑打包产物时暴露的。`tools/contract_selftest.py` 已加断言：
**URL 必须带 `token=`**。

### 3.2 令牌可用性对照（实测）

| 请求 | 状态码 |
|---|---|
| 带正确令牌 | `303`（跳转，随后带 cookie） |
| 裸 `/` | `401` |
| 错误令牌 | `401` |

顺带说明：`looks_like_dsh()` 的判据是响应体含 `dsh`，因此 401 与 303 两种响应都能识别为自家服务。


---

## 4. 更新机制

### 4.1 为什么只走 npm

- DSH CLI **没有** update/upgrade 子命令（`lib/bin.js` 中不存在）
- 官方 README 指定的分发渠道就是 npm：`npx @deepseek-ai/dsh web`
- 因此：**更新 = `npm install -g @deepseek-ai/dsh@<版本>`，回滚 = 装回旧版本号**

不做"拉源码替换"或"复制目录备份"——手工替换会破坏 npm 依赖树与 bin 包装的自洽性，
下一次更新必然出问题。回滚走同一条官方通道，只是版本号不同。

### 4.2 通道策略

| 通道 | 当前值 | 可见条件 |
|---|---|---|
| `latest` | 0.1.5-rc.2 | 默认跟随 |
| `alpha` | 0.1.6-alpha.2 | 仅在托盘勾选「加入预览计划」后 |
| `next` | 0.1.5-rc.2 | 暂不暴露 |

「加入预览计划」是显式 opt-in（对应托盘的可勾选菜单项）。
开启后跟 alpha；关闭后若当前版本高于稳定版，托盘出现「回归稳定版 \<版本\>」，
一键切回——**没有方便的回滚，预览计划就不成立**。

### 4.3 更新事务

```
停服 → npm install -g @deepseek-ai/dsh@<目标> → 校验 dsh --version
     → 重启服务 → 成功
                 ↘ 任一步失败 → npm 装回原版本 → 重启 → 报错
```

停服是必需的：Windows 上服务运行期间 `node_modules` 可能被占用，且即使装上，
运行中的进程仍持有旧模块。

### 4.4 服务生命周期（实测行为）

服务用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 启动，语义是**脱离控制台**：

| 场景 | 服务是否存活 |
|---|---|
| 应用正常退出（关窗 → 托盘 → 退出） | ✅ 存活 |
| 应用被强杀（`taskkill /T`、任务管理器结束进程树） | ❌ 一并终止 |
| 应用崩溃 | ❌ 一并终止 |

原因是 `DETACHED_PROCESS` 只脱离控制台，**不脱离父进程所属的 Job Object**，
强杀进程树时子进程照样被带走。这是可接受的：外骨骼被强杀后留下一个没有 UI 的
孤儿 node 进程反而更糟——下次启动会把陈旧服务"接管"成当前服务。
清缓存与排障时按"应用被强杀 ⇒ 服务也没了"来预期即可。


---

### 4.5 反馈原则（踩过坑才写下来的）

**手动操作的结果必须落在应用内，且无条件。**

出过一次真实的观感故障：用户点「检查更新」后"一点反应没有"。查下来是三件事叠加：

| 原因 | 说明 |
|---|---|
| 唯一的反馈是托盘气泡 | `QSystemTrayIcon.showMessage` 的气泡在屏幕角落、几秒即消；用户视线在鼠标位置，容易错过 |
| 气泡可能被系统拦掉 | Windows「通知」设置 / 专注助手可以直接不显示 |
| 失败时完全静默 | 早期实现里 `CheckWorker` 是 `except Exception: pass`——理由是"网络问题不该变成弹窗"，但那是**自动检查**的理由，套到手动检查上就变成了无反馈 |
| `_checking` 竞态 | 启动后 3 秒会自动检查一次；若用户在它跑完前点击手动检查，会被守卫直接吞掉，同样毫无反馈 |

**现在的规则**：

```
手动检查 → 有更新  → 打开更新窗口（应用内）
         → 无更新  → 应用内提示，并列出当前版本 / 检查通道 / 预览通道最新值
         → 失败    → 应用内告警 + 原因 + 指向 host.log
         → 正在检查 → 应用内提示"请稍候"（不吞掉点击）

自动检查 → 有更新  → 托盘气泡（不打扰为主）
         → 其余    → 静默（不弹窗）
```

配套：`host_log()` 记录"用户看得见的现象"背后的每一步——加载、检查、更新、失败。
在此之前 `config.host_log()` 定义了却从没写过东西，导致"点了没反应"事后无从查起。

回归断言见 `tools/gui_selftest.py` 的「手动检查更新的反馈路径」一节，
五条用例覆盖手动/自动 × 各种结果。

---

### 4.6 更新会不会丢聊天记录

**用户数据全在 `%DSH_HOME%` 下**，这是官方 `dsh --profile web --dump-config` 导出的真实配置：

```yaml
- id: session-persistence-jsonl
  config:
    root: !!js dshHomePath('sessions')     # 聊天记录，一个会话一个 JSONL
- id: storage-json
  config:
    root: !!js dshHomePath('storages')     # 工作区注册表
```

| 内容 | 位置 |
|---|---|
| 聊天记录 | `%DSH_HOME%\sessions\`（懒创建） |
| 工作区注册表 | `%DSH_HOME%\storages\workspace.json`（带 `"version": 2` 单元版本号） |
| 登录凭据 | `%DSH_HOME%\.credentials.yaml` |
| DSH 自己的依赖 | `%DSH_HOME%\profiles\`（**可重建**，实测 200MB / 3.1 万文件） |
| 桌面壳登录态 | `%LOCALAPPDATA%\DSH-Web\profile\` |

更新是 `npm install -g @deepseek-ai/dsh@<版本>`，只动 `<prefix>\node_modules\` 与 npm 的 bin 包装，
与 `%DSH_HOME%` 不相交。我们的代码对 `%DSH_HOME%` 也只有**一个**写入点：
`clear_stale_locks()` 删陈旧的 `.credentials.yaml.lock`。

**不靠"设计上不该动"来交差。** 每次更新在停服后、安装后各取一次
`contract.home_fingerprint()`（文件数 + 总字节数，只统计不复制），
一旦更新后反而变少就明确报出来。统计有上限（60000 项 / 20 秒），
**超出上限时报告"未做比对"，不把"没查"伪装成"查过了"。**

> 为什么不做快照：`%DSH_HOME%` 的规模不可预估——光 `profiles\` 一项就是 200MB，
> 而它还是可重建的依赖。按用户的话说，"我们无法想象这会在未来给存储带来什么压力"。
> 只统计不复制，零存储成本地回答同一个问题。

### 4.7 必须说清的边界：回滚不保证格式可逆

会话格式是**带代次的**，仓库里 `persistence-changes/` 光 9 月 11–14 号就有 4 条变更记录，
并有 `historical-formats/`、`migration-verifier.ts`，以及专门的
`SessionFormatUnsupportedError`（"新构建拒绝解读旧格式"）。

| 操作 | 数据风险 |
|---|---|
| 升级 | 低——旧记录会被迁移或兼容读取 |
| **升上去再回滚** | **中**——新版写出的会话，旧版可能打不开 |

所以 `update_to()` 里的「失败自动回滚」保护的是**应用能启动**，
**不保护数据格式的可逆性**。这一条明确写在更新对话框里（用面向使用者的说法），
不作为隐含假设。

---

## 5. 遇到破坏性更新怎么处置（实操手册）

假设 0.1.7 改了 URL 输出格式，症状是启动卡在"等待服务就绪"直到超时。

1. 先跑自测定位断在哪一环：

   ```
   python tools/contract_selftest.py --keep
   ```

   `--keep` 保留 `.tmp-selftest/`，里面有 `svc.log`（DSH 的真实 stdout）。

2. 看 `svc.log` 里 URL 变成了什么样。

3. **不用改 Python、不用重新打包**——在
   `%LOCALAPPDATA%\DSH-Web\compat.json` 里覆盖正则：

   ```json
   {
     "overrides": [
       {
         "range": ">=0.1.7",
         "note": "0.1.7 改了 URL 输出格式",
         "url_regexes": ["https?://127\\.0\\.0\\.1:\\d+/[^\\s\"']*"]
       }
     ]
   }
   ```

   改完重启桌面壳即生效。

4. 如果连"怎么启动"都变了（新增/改名参数），覆盖 `cli` 段：

   ```json
   { "overrides": [ { "range": ">=0.1.7",
       "cli": { "web": ["serve"], "port": ["--listen-port"] } } ] }
   ```

外部 `compat.json` 与内置表**合并**（只写要改的键），优先级：
内置默认 < 内置区间覆盖 < 外部默认 < 外部区间覆盖。

---

## 6. 首次运行引导（零到可用）

用户拿到的只是一个 exe。如果这台机器上没有 Node.js、或者没装 DSH，
程序不应该只说"启动失败"，而应该把人带过去。这条路径叫**环境供给**
（`dsh_host/provision.py` + 契约层的 `diagnose()`）。

### 6.1 关键约束：npm 的全局 prefix 等于 node.exe 所在目录

```
npm/node_modules/@npmcli/config/lib/index.js:331
  // c:\node\node.exe --> prefix=c:\node\
  this.globalPrefix = dirname(this.execPath)
```

所以在用官方 MSI 装 Node 的机器上，`npm install -g` 的目标是
`C:\Program Files\nodejs` —— **需要管理员权限**。引导流程如果照搬这条命令，
在真实用户机上会直接失败。这是整个引导流程成败的关键点。

**处置**：用 `--prefix` 指向我们自己管理的用户可写目录：

```
%LOCALAPPDATA%\DSH-Web\node-global
```

代价要说清楚：这里的 `dsh` 不在用户 PATH 上，终端里敲 `dsh` 用不了。
这是刻意取舍——桌面壳不需要 PATH，而免权限才是分发的前提。
想强制用别处的安装，在设置里指定 `dsh_bin`。

### 6.2 实测数据

| 项目 | 结果 |
|---|---|
| `npm install -g --prefix <私有目录>` | 成功，`added 518 packages` |
| 首次安装耗时 | **约 13 分钟** |
| 占用空间 | **259MB** |
| 安装后布局 | `dsh` / `dsh.cmd` / `dsh.ps1` + `node_modules/@deepseek-ai/dsh/lib/bin.js` |
| DSH 对 Node 的版本要求 | **无**（`package.json` 没有 `engines` 字段） |

**13 分钟这个数字是界面要求**：必须有流式进度，并明确告知"可以先去忙别的"。
否则用户会以为卡死而中途关掉，留下半个 `node_modules`。

因为 DSH 没声明版本要求，我们**不设版本下限**——装最新 LTS，然后实跑
`dsh --version` 验证。能跑就认，不替上游发明约束。

### 6.3 缺 Node 时怎么装

默认走 winget（微软官方包管理器，Node 由 OpenJS 官方发布）：

```
winget install --id OpenJS.NodeJS.LTS --exact \
  --accept-package-agreements --accept-source-agreements \
  --disable-interactivity --silent
```

已实测本机 winget 可用，`OpenJS.NodeJS.LTS` → 24.19.0。

装完后当前进程**看不到新 PATH**（环境变量变更不向已运行的进程传播），
所以 `provision.refresh_path_from_registry()` 会从注册表重读用户级 + 机器级 PATH
补进本进程，避免要求用户重启程序。读取注册表不需要管理员权限。

> 未实测项：MSI 作用域是全机，**预期会弹一次 UAC**。这条是推断，不是实测结论。

### 6.4 两类"没装"必须分开报

诊断层要区分这两种，因为它们给用户的建议完全不同：

| 情况 | 报告 |
|---|---|
| 根本没装 DSH | 「未安装」→ 引导安装 |
| 装了但 `dsh --version` 跑不起来 | 「找到了但无法运行，通常是 Node.js 版本过旧或安装不完整」 |

后者正是"不设版本下限"的配套：既然不拦版本，就必须在失败时给出可读的原因，
而不是笼统说"未检测到"。

---

## 7. 目录规划（已落地）

历史上 `%LOCALAPPDATA%\DSH-Web\` 同时承担两个角色——Nuitka 解包目录与桌面壳数据目录，
29 个 DLL 与 `logs/`、`qt-profile/` 混放。风险是解包目录按设计就是"排障时可随手清掉"的缓存，
清缓存会连带丢掉登录态与配置。现已分家：

```
%LOCALAPPDATA%\DSH-Web\                  桌面壳数据根（只归我们）
    settings.json                        宿主配置
    compat.json                          适配表外部覆盖（可手改，改完即生效）
    install.json                         安装定位缓存
    service.json                         服务运行时状态
    logs\
        dsh-web.log                      DSH 服务 stdout/stderr
        host.log                         桌面壳自身日志
    profile\                             QtWebEngine 持久 profile（登录态）
    node-global\                         首次运行引导安装的 DSH（私有 npm prefix，见第 6 节）

%LOCALAPPDATA%\DSH-Web-runtime\          Nuitka onefile 解包目录（纯缓存，可随时删）
```

卸载时删掉 `DSH-Web\` 即可，不影响系统里其它 Node.js 安装。

**边界规则（新增代码请遵守）**

- 所有路径只在 `dsh_host/config.py` 定义，其余模块一律 import，
  **不得自行拼 `LOCALAPPDATA`**（改造前路径散落在 config / compat / contract / GUI 四处）
- 删 `runtime_dir()` 永远安全；删 `data_root()` 会丢配置与登录态
- 桌面壳数据永不写进 `DSH_HOME`

打包参数已同步改为 `--onefile-tempdir-spec={CACHE_DIR}/DSH-Web-runtime`（见第 7 节）。

---

## 8. 打包

构建环境刻意复现为 **Python 3.13.9 + PySide6 6.9.2 + Nuitka 4.2.1**，与首次打包一致，
避免换环境引入无关变量。该环境装在独立 venv 里，用 `--system-site-packages` 继承 anaconda 的
PySide6，**不污染 anaconda base**：

```
# 一次性准备（已执行）
D:\DevVmEnv\anaconda3\python.exe -m venv --system-site-packages D:\AppData\dsh-launcher-buildenv
D:\AppData\dsh-launcher-buildenv\Scripts\python.exe -m pip install --upgrade nuitka

# 打包
cd D:\PersonApps\dsh-launcher
D:\AppData\dsh-launcher-buildenv\Scripts\python.exe -m nuitka ^
  --onefile --windows-console-mode=disable ^
  --windows-icon-from-ico=D:\PersonApps\dsh-launcher\icon.ico ^
  --enable-plugin=pyside6 ^
  --include-package=dsh_host ^
  --onefile-tempdir-spec={CACHE_DIR}/DSH-Web-runtime ^
  --output-dir=build --output-filename=DSH-Web.exe --lto=yes ^
  dsh_gui_qt.py
```

> `--include-package=dsh_host` 虽然 Nuitka 通常能自动跟随导入，但显式声明更稳妥——
> 漏收包的表现是打出来的 exe 运行时 ImportError，很难在打包阶段察觉。

**部署**：产物是单个 120MB 的 `build\DSH-Web.exe`，复制到桌面即可。
桌面上那个 `DSH-Web.exe` 就是这个 onefile 产物本身，不是快捷方式。

---

## 9. 自测

| 脚本 | 覆盖范围 | 用哪个解释器 |
|---|---|---|
| `tools/contract_selftest.py` | 安装定位、prefix 层级、通道查询、启动/就绪/URL/停止全链路，以及环境自检与探测回归（node 与 bin.js 分离、显式覆盖绕过缓存、私有 prefix 优先级） | 任意 Python 3.10+ |
| `tools/gui_selftest.py` | MainWindow 启动流程、接管已有服务、WebEngine 懒加载、菜单状态，以及引导窗口三种状态的渲染 | 需 PySide6（无头，`QT_QPA_PLATFORM=offscreen`） |

两个脚本都用临时 `DSH_HOME`，不会碰正在使用的 `D:\AppData\dsh`。
**每次 DSH 更新后建议跑一遍 `contract_selftest.py`**，用来第一时间发现破坏性变更落在哪一环。

---

## 10. 后续优化方向

### 9.1 冷启动

实测服务就绪约 7-8 秒，其中绝大部分是 DSH 自身的插件加载，我方可控的有三处：

| 项 | 现状 | 可省 |
|---|---|---|
| 版本探测在关键路径上 | `resolve_install()` 缓存命中后仍 spawn `dsh --version` 复验 | ~0.3-0.5s |
| 单实例检测等待 | `waitForConnected(300)` 固定等 300ms | ~0.15s |
| WebEngine 预热 | 服务就绪后才创建 profile/view（串行） | 首屏主观等待 |

另有一条**依赖上游**的：0.1.6 官方声明"减少 CLI 及 Web 启动等候时间"——
升级本身就是冷启动优化。

### 9.2 自定义标题栏

按老大的提醒，自绘标题栏必须尽量走系统接口。两条路线：

**路线 A（推荐）：保留原生标题栏，只改配色与材质**

用 Win32 DWM 接口，不碰窗口结构：

| 接口 | 作用 |
|---|---|
| `DwmSetWindowAttribute(DWMWA_CAPTION_COLOR, 35)` | 标题栏底色 |
| `DWMWA_TEXT_COLOR, 36` | 标题文字色 |
| `DWMWA_BORDER_COLOR, 34` | 边框色 |
| `DWMWA_USE_IMMERSIVE_DARK_MODE, 20` | 深色标题栏 |
| `DWMWA_SYSTEMBACKDROP_TYPE, 38` | Mica / Acrylic 材质 |

拖动、缩放、Aero Snap、Snap Layouts、右键系统菜单、多显示器 DPI **全部由系统维持**，
零维护成本。DSH 本体是深色 UI，配深色标题栏视觉上已相当接近自绘。

**路线 B：真去除系统标题栏（`Qt.FramelessWindowHint`）**

若要完全自绘，拖动与缩放**必须**用 Qt 提供的系统级接口，而不是自己算鼠标位移：

- `QWindow.startSystemMove()` —— 底层走 Win32 `SC_MOVE`，保留全部系统行为
- `QWindow.startSystemResize(edges)` —— 同理走 `SC_SIZE`

自查清单：最大化是否遮住任务栏、Snap Layouts 悬停菜单是否可用、双击标题栏是否最大化、
右键标题栏是否弹出系统菜单、跨显示器 DPI 变化是否错位。
这些细节靠自己实现基本做不干净——这也是路线 A 更划算的原因。

