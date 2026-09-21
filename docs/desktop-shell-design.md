# DSH 桌面壳设计说明

面向"DSH 处于 developer preview、每代都可能破坏兼容"这一前提做的适配层改造。
最后更新：2026-09-20

---

## 1. 分层职责

```
L2  dsh_host/updater.py    更新器    DSH 更新，只走官方 npm 通道
    dsh_host/provision.py  环境供给  首次运行补齐 Node 与 DSH
    dsh_host/selfupdate.py 自更新    桌面壳自身更新，走 GitHub Release
L1  dsh_host/compat.py     适配表   版本区间 → 探测顺序与降级策略（数据）
L0  dsh_host/contract.py   契约层   唯一允许知道 DSH 内部结构的地方
    dsh_host/config.py     路径     所有路径的唯一权威定义处
    dsh_host/version.py    版本     桌面壳版本号，只定义一次
    dsh_gui_qt.py          界面     只做界面 + 流程编排
```

**铁律：L0 以上的代码不得出现 DSH 的私有路径、私有文件名、私有日志格式。**
新增功能时如果发现需要知道 DSH 内部细节，那说明该加一个契约能力，而不是在界面里写 `if`。

**两个版本号相互独立**：

| | 版本号 | 更新源 | 换掉的是什么 |
|---|---|---|---|
| DSH | 如 `0.1.6-alpha.2` | npm registry | `%LOCALAPPDATA%\DSH-Web\node-global\` 里的程序 |
| 桌面壳 | `HOST_VERSION`，如 `2.0.0` | GitHub Release | **正在运行的这个 exe** |

界面上必须分开呈现。合成一个"检查更新"会让用户以为更新完界面就该变。

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

## 4.8 桌面壳自更新（2026-09-20 加）

走 GitHub Release，与 DSH 的 npm 通道完全无关。

### 为什么不能直接覆盖自己

Windows 不允许覆盖正在运行的 exe（文件被锁）。标准解法是找一个"局外进程"
在退出后做替换。这里用 `.bat` 而不是再打包一个 updater.exe：

- 零额外依赖，不用为更新功能再造一个 120MB 的 exe
- 天然可审计，出问题用户能自己打开 bat 看懂它在干什么
- 用 `move`（同目录重命名）而非 `copy`，**不需要管理员权限**

流程：下载到临时文件 → 校验 → 写 bat → 用户点「立即重启」→ 进程退出 →
bat 等待 PID 消失 → `move` 备份旧版 → `move` 放入新版 → 重启 → 自删除。

### bat 的五个坑（都踩过，勿回退）

| # | 坑 | 症状 | 正确做法 |
|---|---|---|---|
| 1 | **路径用正斜杠** | `move` 静默失败，文件根本没换，且无任何报错 | `os.path.normpath()` 强制反斜杠。实测 `move C:/x/a.exe` 不工作 |
| 2 | **`>nul 2>&1` 污染 errorlevel** | `if errorlevel 1` 误判，明明成功也走失败分支 | 只重定向 stdout（`>nul`），让 stderr 原样输出 |
| 3 | **`del "%~f0"` 放在中间** | 报 `The batch file cannot be found`，后续行全读不到 | 必须放最后，且前面用 `goto fin` 保证一定走到 |
| 4 | **PID 为 0 时进等待循环** | `tasklist /FI "PID eq 0"` 匹配到系统空闲进程，死等到超时，替换永不发生 | PID 无效时直接跳过等待 |
| 5 | **`find` 解析到 Git Bash 的版本** | 只在自己机器上复现，正常 cmd 环境无此问题 | 写 `find.exe` |

第 1 条最阴——`move` 失败时不报错、不返回非零之外的信息，表现是"更新流程全跑完了但版本没变"。
回归断言：`tools/gui_selftest.py` 的 `host_update_checks()` 会检查生成的 bat 里路径全是反斜杠。

### 安全边界

只从**官方 GitHub Release** 拉取，且：

1. 校验 tag 与请求的版本号一致（防止被指向别的 release）
2. 校验是有效 PE 文件（`MZ` 头）——GitHub 出错时会返回 HTML，这一步能拦住
3. 体积下限 5MB——错误页 / 空文件不可能通过
4. 替换前把当前 exe 备份为 `DSH-Web.exe.old`，失败可人工改回

**没有做代码签名校验。** 若将来要对外大规模分发，应加签名（需证书）
或至少校验 SHA-256（可在 Release 资产里附带校验文件）。

### 数据影响

**零。** 桌面壳更新只替换 exe 本身，不碰 `%LOCALAPPDATA%\DSH-Web\`
（配置与登录态）也不碰 `DSH_HOME`（会话记录）。这一点写在更新对话框里。

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

## 8. 打包与发布

### 8.1 本地打包

**构建解释器不能用 conda 系**。用 anaconda（哪怕建 venv + `--system-site-packages`）
会让 Nuitka 报 `flavor 'Anaconda Python'`，其 PySide6 插件枚举 conda 元数据时
抛 `KeyError: 'files'` 直接崩。用 python.org 的独立解释器。

实测可用的组合：**Python 3.13.14 + PySide6 6.9.3 + Nuitka 4.2.1**。

```
python -m nuitka --onefile ^
  --windows-console-mode=disable ^
  --windows-icon-from-ico=assets/icon.ico ^
  --enable-plugin=pyside6 ^
  --include-package=dsh_host ^
  --onefile-tempdir-spec={CACHE_DIR}/DSH-Web-runtime ^
  --output-dir=build --output-filename=DSH-Web.exe --lto=yes ^
  dsh_gui_qt.py
```

> `--include-package=dsh_host` 虽然 Nuitka 通常能自动跟随导入，但显式声明更稳妥——
> 漏收包的表现是打出来的 exe 运行时 ImportError，很难在打包阶段察觉。

**`--windows-icon-from-ico` 指向 `assets/icon.ico`**（2026-09-20 从根目录移入 `assets/`）。
同理界面层读图标的路径也用 `os.path.join(ROOT, "assets", "icon.ico")`。

**构建时不要混入 `rm`**：Nuitka 会产生数千个中间文件，安全删除保护会拦截大批量
`rm` 且让 `&&` 链整体挂起（表现为卡住不报错）。清理单独执行。

### 8.2 发布流程（推荐走这条）

版本号只在 `dsh_host/version.py` 定义一次：

```
1. 改 HOST_VERSION（如 2.0.0 → 2.0.1）
2. git commit && git push
3. git tag v2.0.1 && git push origin v2.0.1
4. GitHub Actions 自动构建 → 跑自测 → 创建 Release → 附带 DSH-Web.exe
```

`.github/workflows/release.yml` 会在构建前**校验 tag 与源码里的版本号一致**，
不一致直接失败——避免发出去的 exe 自称的版本和 tag 对不上，
那会让客户端的自更新判断彻底错乱。

工作流还会跑两个自测脚本，任何一个失败都不发布。

手动触发（`workflow_dispatch`）只构建、不建 Release，产物作为 artifact 供下载，
用来验证构建环境是否正常。

**部署**：Release 附带的 `DSH-Web.exe` 是单个约 120MB 的 onefile 产物，
复制到桌面即可。桌面上那个 `DSH-Web.exe` 就是这个 onefile 产物本身，不是快捷方式。

---

## 9. 自测

| 脚本 | 覆盖范围 | 用哪个解释器 |
|---|---|---|
| `tools/contract_selftest.py` | 安装定位、prefix 层级、通道查询、启动/就绪/URL/停止全链路，以及环境自检与探测回归（node 与 bin.js 分离、显式覆盖绕过缓存、私有 prefix 优先级） | 任意 Python 3.10+ |
| `tools/gui_selftest.py` | MainWindow 启动流程、接管已有服务、WebEngine 懒加载、菜单状态、引导窗口三状态渲染，**托盘菜单结构语义**、**桌面壳自更新纯逻辑**（版本比较、bat 生成要素、反斜杠规范化、自删除位置、PID=0 分支）、**标题栏取色与配色**（含 WCAG 对比度择优与真实采样规模的杂色门槛） | 需 PySide6（无头，`QT_QPA_PLATFORM=offscreen`） |
| `tools/dwm_verify.py` | **实机**验证 DWM 上色：GDI 截屏逐行采样核对像素、算法链路、真实采样规模下的取色门槛 | 需 PySide6 + 真实窗口（会闪现） |
| `tools/titlebar_e2e.py` | **端到端**验证 `_apply_titlebar` 接线：config → blend → contrast_text → 四个 DWM 调用 → 像素落地 | 需 PySide6 + 真实窗口（会闪现） |
| `tools/selfupdate_e2e.py` | **端到端**验证自更新全链路：`running_as_exe` 判定（含 Nuitka 的 `__compiled__`）、`current_exe` 不指向解释器、download 的体积/PE 校验（正反两向）、apply 写出并拉起 bat、bat 真的替换目标并留 `.old` | 任意 Python 3.10+ |

两个无头脚本都用临时 `DSH_HOME`，不会碰正在使用的 `D:\AppData\dsh`。
**每次 DSH 更新后建议跑一遍 `contract_selftest.py`**，用来第一时间发现破坏性变更落在哪一环。

后两个脚本需要真实窗口（DWM 要求窗口映射到屏幕），不进 CI；
改动标题栏相关代码后手动跑一次。

CI 里用 `DSH_SELFTEST_LITE=1` / `DSH_GUI_SELFTEST_STATIC=1` 走纯逻辑分支——
**用显式开关而不是"检测不到就跳过"**，后者会让本机漏跑变成静默通过。

> **写取色/阈值类的测试时，必须用真实采样规模造数据。**
> 见 10.2 节末尾的计数器例子——拿小图验门槛会得出差两个数量级的结论。

---

## 10. 后续优化方向

### 10.1 冷启动

实测服务就绪约 7-8 秒，其中绝大部分是 DSH 自身的插件加载，我方可控的有三处：

| 项 | 现状 | 可省 |
|---|---|---|
| 版本探测在关键路径上 | `resolve_install()` 缓存命中后仍 spawn `dsh --version` 复验 | ~0.3-0.5s |
| 单实例检测等待 | `waitForConnected(300)` 固定等 300ms | ~0.15s |
| WebEngine 预热 | 服务就绪后才创建 profile/view（串行） | 首屏主观等待 |

另有一条**依赖上游**的：0.1.6 官方声明"减少 CLI 及 Web 启动等候时间"——
升级本身就是冷启动优化。

### 10.2 标题栏自适应配色（已实现，2026-09-20）

用系统原生标题栏 + DWM 上色，**不做完全自绘**。
这样拖动、缩放、Aero Snap、Snap Layouts、右键系统菜单、
多显示器 DPI **全部由系统维持**，零维护成本。

实现落在 `dsh_host/dwm.py`（纯逻辑 + DWM 封装），界面侧调用点两个：
`MainWindow._sample_titlebar_color()` 取色、`_apply_titlebar()` 上色。

#### 取色：走截图，不走 DOM

| 路 | 做法 | 评价 |
|---|---|---|
| A ✅ | `QWebEngineView.grab()` 截图 → 取顶部若干行像素 → 统计主色 | 只依赖"渲染出来的像素" |
| B ❌ | 注入 JS 读 `document.body` 的 computed style | 精确但**依赖 DSH 的 DOM 结构** → 违反分层铁律，而 DSH 是 developer preview，DOM 随时会变 |

选 A。三个实现细节：

**1. 取众数，不取平均。** 把颜色量化到 16 级一档再统计出现最多的那一档。
求平均会把高饱和主色和背景色混成一片灰——**那正是"突兀"的来源**。

**2. 低饱和度像素降权。** 饱和度低于 6% 的像素（页面留白、滚动条、灰边）
权重减半。否则大面积灰底会盖过真正的主题色。

**3. 只取顶部 6% 高度、左右各让开 2%。** 避开滚动条与窗口圆角。
只取顶部是因为要匹配的是"界面顶部的观感"。

#### 上色：三个属性必须一起设

| 属性 | 值 | 不设的后果 |
|---|---|---|
| `DWMWA_CAPTION_COLOR` (35) | 取到的主色（可降饱和一点） | 标题栏还是系统色 |
| `DWMWA_TEXT_COLOR` (36) | 按底色**算对比度**择优 | **浅底上白字，读不了** |
| `DWMWA_USE_IMMERSIVE_DARK_MODE` (20) | 按底色亮度 | 系统绘制的关闭按钮/边框高光配不上色 |
| `DWMWA_BORDER_COLOR` (34) | 同底色 | 边框出现一条异色线 |

#### 文字色判据：算对比度，不要用阈值二选一

```python
def contrast_text(bg, dark=(24,26,31), light=(240,242,245)):
    return dark if (contrast_ratio(bg, dark) >= contrast_ratio(bg, light)) else light
```

**为什么不用"亮度过阈值就换色"**（早期实现）：

1. **结果不连续** —— 背景在阈值附近动一点，文字色整个翻转。
2. **不保证可读** —— 它只回答"背景算深还是浅"，不回答"这字色够不够看清"。

实测扫描整个灰阶，旧判据在 **120-127 这 8 个点上选错**：

| 灰阶 | 旧选（浅字）对比度 | 新选（深字）对比度 |
|---|---|---|
| 121 | 3.88 | 4.00 |
| **127** | **3.57** ← 明显不达标 | **4.35** |

算对比度取值高的那个，结果连续、且给出可证明更优的解。

**对比度计算必须走 WCAG 公式**，不能拿 BT.601 亮度比大小：

```python
# sRGB 线性化（gamma 解码），不能省
c = ch / 255
lin = c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
# 相对亮度
L = 0.2126*lin(r) + 0.7152*lin(g) + 0.0722*lin(b)
# 对比度比值（0.05 是环境光补偿项）
ratio = (max(La,Lb) + 0.05) / (min(La,Lb) + 0.05)     # 1.0 ~ 21.0
```

直接用 sRGB 数值比大小是常见错误：sRGB 是 gamma 编码过的，
同样差 50 个数值，在暗部比亮部实际差得多。

`dwm.luminance()`（BT.601）**仅用于**"要不要给系统开暗色模式"这类粗判，
不再参与文字色抉择。

**主色降饱和 35%** 再上色（`config: titlebar_mute`）。

必须是**降饱和**（往它自己的灰度拉），不能是"往固定深灰 blend"：

| 取到的界面色 | 往深灰 blend 35%（错） | 降饱和 35%（对） |
|---|---|---|
| `#ffffff`（浅色主题） | **`#acadae`** 灰 ❌ | `#ffffff` ✅ |
| `#f5f5f7` | `#a6a6a9` ❌ | `#f5f5f6` ✅ |
| `#22c1a3`（品牌色） | `#1c8472` | `#48af9c`（亮度不变 142，饱和 .62→.40） |
| `#151517` | `#141517` | `#151516` ✅ |

> **老大实机发现的 bug**：浅色主题下"界面是白的、标题栏是灰的"。
> 日志铁证：取色 `#ffffff`（完全正确），上色却成了 `#acadae`
> —— 就是"往固定深灰拉"造成的。中性色（白/灰/黑）降饱和后应当
> **保持不变**，因为它们的灰度就是自己。

原理：降饱和把颜色往**自身 BT.601 亮度**对应的灰色拉，
所以只降彩度、不改明暗，观感"退后"但不脏。
早期方案往固定深灰拉，对鲜艳色尚可，对中性浅色是灾难。

#### 主题跟踪：低频 + 只读计算样式 + 变了才动

DSH 的主题可能在运行中切换（不用重启），所以启动时取一次色不够。

**关键是让"跟踪"比"取色"便宜一个量级**：

| 做法 | 代价 |
|---|---|
| ~~定期截图取色~~ | 跨进程回读整幅位图，**重** ❌ |
| **只跑 L1（JS 读 computed style）** | 查 CSSOM，几十次调用**微秒级** ✅ |
| 颜色**真的变了**才动 DWM（容差 `THEME_CHANGE_TOL`） | 绝大多数 tick 是空转 |
| 窗口最小化/不可见时跳过 | 没人在看就不跟 |

- 间隔 `config: theme_watch_ms`，默认 **8000ms**，`0` = 关闭跟踪
- 有下限 `_THEME_WATCH_MIN_MS = 1500ms` —— 防止有人设成 50ms 变成忙轮询
- 变更判定带容差，避免渐变/抗锯齿噪声导致反复重设 DWM 属性

#### 时序：立即试 + 退避重试（不要固定死等）

`loadFinished` 只代表 DOM 就绪，**样式和首绘可能还没完成**。

早期实现用 `QTimer.singleShot(4000, ...)` 死等 4 秒 —— 结果老大反馈
**"加载完网页后有一些明显的延迟"**，正是这 4 秒空等。

现在改成 **loadFinished 触发 + 失败退避重试**：

```
loadFinished(ok) → _schedule_titlebar_probe(0)
     ↓ 250ms 后第一次尝试
  三层取色 ──成功──→ 上色，结束
     └──失败──→ 按 _TB_RETRY_MS 退避后重试（400/600/900/1300/1800/2400ms）
```

总预算约 **7.7 秒**，而正常情况**第一次（250ms）就成功** ——
因为有了空白检测（见下），能区分"页面还没画出来"和"页面本身是纯色"，
所以可以在渲染完成的瞬间上色，而不是干等一个猜出来的固定时长。

**不做持续监听**：截图是重操作，而界面主题在使用中基本不变。

取色全程 try/except 包住且失败只写日志——标题栏保持系统默认，
不影响任何功能。菜单里有开关可关掉（视觉偏好因人而异）。

#### 图标必须跟着标题栏深浅走

**黑色图标在深色标题栏上等于看不见**（老大实机发现）。

系统默认是浅色主题时，黑色图标本来没问题；但**是我们把标题栏改成深色
的**，所以图标也得跟着换：

| 状态 | 图标 |
|---|---|
| 取色后底色是深色 | `assets/icon-light.ico`（**白色**） |
| 取色后底色是浅色 | `assets/icon-dark.ico`（深色 #181a1f） |
| 未接管标题栏（开关关闭／取色失败） | 跟随**系统**主题（读注册表 `AppsUseLightTheme`） |

两份 ico 由 `tools/make_icons.py` 从 `assets/deepseek.svg` 生成，
**每个文件内含 16~256 共 9 个尺寸** —— 单尺寸 ICO 在小图标下会糊。

注意 SVG 源文件的 `fill="currentColor"`：脱离上下文时它默认渲染成
**黑色**，所以生成时必须显式替换颜色。

重设图标会让任务栏图标闪一下，所以用 `_icon_dark_bg` 记住上次状态，
同色不重复设置。图标设置失败**不能影响上色**，单独 try 住。

#### 性能预算（实测，勿退化）

整条链路跑在 **GUI 线程**上，耗时直接体现为界面卡顿。所以每一环都量过：

| 环节 | 1200x800 | 备注 |
|---|---|---|
| `grab + toImage + RGBA8888` | ~4.9 ms | 固定成本，无法再省 |
| 切片拷贝顶部 6% | ~0.03 ms | **整图拷贝要 1.0 ms（5.7MB）** |
| `dominant_color` 统计 | ~1.2 ms | 优化前 19.6 ms |
| **合计** | **~6.1 ms** | 属于"一次性的轻微停顿" |

两个优化点（都实测验证过效果）：

**① 用 memoryview 直接切片，绝不整图转 bytes。**

```python
mv = img.constBits()
if isinstance(mv, (bytes, bytearray)):
    mv = memoryview(mv)
elif getattr(mv, "itemsize", 1) != 1:
    mv = mv.cast("B")            # 默认视图元素是 4 字节结构体，必须 cast

stride = img.bytesPerLine()      # 不一定等于 width*4
data = b"".join(bytes(mv[y*stride + x0*4 : y*stride + (x0+span)*4])
                for y in range(rows))
```

只拷顶部 6% 而非整图：

| 做法 | 耗时 | 峰值内存 |
|---|---|---|
| `bytes(img.constBits())` 再切 | 1.025 ms | 5.7 MB |
| memoryview 直接切 | **0.033 ms** | **0.33 MB** |

→ **快 31 倍，峰值内存降 17 倍**，结果逐位一致。4K 下差距更大
（整图 33MB）。

这一步容易白做：只优化 `dominant_color` 而留着上面那句整图
`bytes()`，省的量会被整图拷贝完全抵消。

必须用 `bytesPerLine()` 而不是假设 `width*4` —— 行有对齐填充时两者不等。

**② `dominant_color` 内部走 C 层操作。**

| 手法 | 作用 |
|---|---|
| `bytes.find(b"\x00", 3, end)` | C 层查透明像素，绝大多数截图无透明 → 完全跳过 Python 循环 |
| 带步长切片 `buf[0::4]` + bytearray 步长赋值 | 剔除 A 通道、交织 RGB，全在 C 层 |
| `bytes.translate(_QUANT_TABLE)` | 量化查表，一次处理整块数据 |
| 分桶前降采样到 `_MAX_VOTES` | 投票点数 5.5 万 → 几千 |

实测：**19.6 ms → 1.2 ms（约 13 倍）**，且随分辨率近乎线性
（2560x1440 也只 3 ms）。

回归护栏写在 `gui_selftest.py`：断言 < 40 ms。
阈值故意留宽，只拦"退化回逐像素循环"这种量级的问题，不做精确计时断言。

#### 验证方法（踩过两个坑，务必照此办）

**坑一：这三个属性是「只写」的，不能回读。**

对 `DWMWA_CAPTION_COLOR(35)` / `TEXT_COLOR(36)` / `BORDER_COLOR(34)` 调用
`DwmGetWindowAttribute`，**恒返回 `E_INVALIDARG (0x80070057)`**。
这不代表设置失败——这些属性微软压根不支持读。

> 曾经拿"设置后回读比对"当验收标准，看到 `E_INVALIDARG` 就判定
> "DWM 上色不可用"，白花了时间排查一个根本不存在的问题。
> **功能一直是好的，是验收方式错了。**

**坑二：Qt 的 `grabWindow` 不能用来验证。**

`QScreen.grabWindow(winId)` 在本机（Windows 11 24H2 / build 26200）
返回整片 `#f3f3f3`，**所有行都是同一个颜色**——真窗口不可能这样，
说明拿到的是陈旧/空白的合成表面。

**正确做法：Win32 GDI 截屏。**

```
PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT)   # 必须用 2，否则只画客户区
  ↓
GetDIBits(...)  →  BGRA 缓冲
  ↓
逐行采样中心列，看设定色是否出现
```

实测证据（上色 `#242a38`）：

| 阶段 | y=1..37（标题栏） | y=39+（客户区） |
|---|---|---|
| 上色前 | `#f3f3f3` | `#f3f3f3` |
| 上色后 | `#242a38` **逐像素精确** | `#f3f3f3` |

38 行像素与设定值零偏差，这才叫验证通过。

可复跑的脚本：`tools/dwm_verify.py`（17 项，含算法链路与像素级验收）。

#### 主色调统计：灰像素处理

界面顶部通常是**大面积中性灰底 + 小面积高饱和主题色**。按像素数投票的话，
灰底必然压倒主题色，取到的主色调就是灰——标题栏跟着变灰，等于没做自适应。

所以分两轮：先在有彩像素里取众数；只有确实无彩时才退回灰像素定色。

**但"有彩"要有门槛，否则抗锯齿杂色会带偏标题栏。**

如实测：深灰底 + 只有 1 列彩色像素，会取到那个彩色，
一个**深色主题**的界面被带成亮青标题栏。那些像素来自图标边缘的
次像素渲染，不代表主题色。

| 方案 | 实测结果 |
|---|---|
| 灰像素降权 0.5 | 2:1 的像素数优势仍让灰胜出（取到 `(24,24,24)`）❌ |
| "有彩像素 ≥ 24 个"数量门槛 | **不可移植**：同样 48 个像素，小窗口里是可观的一条带，1200x800 窗口里只是 0.09% ❌ |
| **有彩像素占比 ≥ 0.5%** | ✅ |

**只看占比，不看绝对数量。** 真实调用一次采样 4w~12w 像素
（`rows = 6%高` × `宽 - 4%`），0.5% 相当于 200~600 个像素——
这个量级才是"界面上真有一块彩色"。

实测（按真实采样规模 55296 像素）：

| 彩色占比 | 结果 |
|---|---|
| 0.09%（1 列） | 退回灰色 ✅ |
| 0.35% | 退回灰色 ✅ |
| 0.52% | 认出主题色（边界） |
| 1% ~ 50% | 认出主题色 ✅ |

> **验证门槛时务必用真实采样规模造数据。**
> 我最初拿 80×8（640 像素）试，看着挺合理；换算到真实规模才发现
> 差了**两个数量级**——48 个像素在 640 里是 7.5%，在 55296 里是 0.087%。

#### 若将来要真去除系统标题栏

`Qt.FramelessWindowHint` 之后，拖动与缩放**必须**用
`QWindow.startSystemMove()` / `startSystemResize(edges)` —— 底层走 Win32
`SC_MOVE` / `SC_SIZE`，保留全部系统行为，**不要自己算鼠标位移**。

自查清单：最大化是否遮住任务栏、Snap Layouts 悬停菜单是否可用、
双击标题栏是否最大化、右键标题栏是否弹出系统菜单、跨显示器 DPI 变化是否错位。
这些细节靠自己实现基本做不干净——这也是继续用原生标题栏的原因。

