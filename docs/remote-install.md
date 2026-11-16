# 远程安装策略

> 2026-11-16 在真实 Linux 主机（192.168.102.24，Ubuntu 22.04 / 1.8 GB 内存）上
> 完整走了一遍远程安装与连接。这篇文档回答两个问题：
> **远程安装该不该只装 dsh？用户装了别的变体我们还接得上吗？**

---

## 一、结论

**只装官方 `@deepseek-ai/dsh`，不碰任何变体。**
但"给用户选择"的空间在**安装方式**上，不在**安装对象**上：

| 选项 | 适用场景 | 代价 |
|---|---|---|
| **我帮你装（用户级）** | 默认。不需要 sudo | 改 `~/.npmrc` 的 prefix、往 `~/.profile` 加一行 PATH |
| **我帮你装（系统级）** | 用户有免密 sudo，希望所有账户可见 | 需要提权；多用户机器才划算 |
| **已经装好了，只连不装** | 用户自己维护环境 | 我们要能识别"装在哪"，见第三节 |
| **给我命令，我自己跑** | 用户不想让程序动他的机器 | 装完回来点"重试" |

**不建议**做成"随便装什么客户端都行"。理由是本项目的契约是**服务端契约**：
我们要的是 `dsh web --port 0 --no-open` 打印出的那条带令牌的地址。

---

## 二、dsh-tui 接不上（这是关键判断）

`npm view dsh-tui` 的实测结果：

```
description  Terminal client for DeepSeek Harness: a TUI over the DSH client contract (ctx.remote)
bin          {'dsh-tui': 'bin/tui.js'}
dependencies {'ink': '^7.1.1', 'react': '^19.2.8'}
repository   github.com/MashedPotato817/dsh-tui
```

三条事实：

1. **它是客户端，不是服务端。** 描述写得很清楚——"a TUI over the DSH client
   contract"，它是通过 DSH 的客户端契约去连一个**已经在跑的 DSH** 的终端界面。
   它自己不监听端口、不产出 `dsh web:` 地址。
2. **它不是官方包。** 不在 `@deepseek-ai` scope 下，作者是第三方，版本号
   0.2.19 与官方 `0.2.2-rc.1` 也不同步。
3. **它的依赖是终端 UI 框架**（ink + react），不是 DSH 运行时。

所以：**装 dsh-tui 替代不了 dsh，换上它我们没有任何东西可连。**
反过来，用户如果装了 dsh-tui，几乎必然也装了官方 dsh（他要连的那个服务端）
——那种情况下我们直接连他已有的 dsh 就行，不需要装。

**判断一个变体能不能接，只看一条**：它是否提供
`dsh web --host 127.0.0.1 --port 0 --no-open` 这个子命令，并且在 stdout 打印
`dsh web: http://127.0.0.1:<port>/?token=<token>`。
不满足就不用谈别的——我们的隧道、令牌解析、复用判据全都挂在这个格式上。

---

## 三、实测踩到的三个坑（都写进实现要求）

### 3.1 裸包名装不上：必须带 tag

```
$ npm install -g @deepseek-ai/dsh
npm error code ETARGET
npm error notarget No matching version found for @deepseek-ai/dsh@*.
```

但 `npm view @deepseek-ai/dsh version` 又明明能打出 `0.2.2-rc.1`。

原因：**这个包所有版本都是预发布**，`latest` tag 也指向预发布（`0.2.2-rc.1`），
npm 10 对"通配安装 + 全是 prerelease"的组合会拒绝解析。

正确写法：`npm install -g @deepseek-ai/dsh@latest`（或显式版本）。

> 我们自己代码里本来就带 `@<tag>`，没踩这个坑；但**给用户看的提示文案**里
> 写过裸包名，用户照着敲必然失败。已改成 `@latest`（`provision.py` / `remote.py`）。

### 3.2 没有 sudo 时，安装位置与 PATH 都要一起处理

这台机器 `sudo -n` 需要密码，`npm config get prefix` 是 `/usr`（写不进去）。
只能用户级安装：

```bash
npm config set prefix ~/.npm-global
npm install -g @deepseek-ai/dsh@latest
echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> ~/.profile
```

**第三行不能省。** 我们的远端脚本走 `sh -l`（登录 shell），
`~/.profile` 里没有这一行，`command -v dsh` 就找不到——用户会看到
"装好了但连不上"。加 PATH 后实测：用**默认命令** `dsh web` 直接起得来（7.7 秒），
用户不需要填任何路径。

### 3.3 node 版本警告可以忽略，但要记录

```
npm warn EBADENGINE  package: '@deepseek-ai/dsh@0.2.2-rc.1',
  required: { node: '>=24.9.0' },  current: { node: 'v22.23.3' }
```

实测结论：**node 22.23.3 上 `dsh --version` 与 `dsh web` 都能正常跑**，
完整链路（启动 → 打印地址 → 401 → 带令牌 200）全部通过。
所以这个引擎约束在 22.x 上是"声明"而非"硬阻断"。

但不要据此就去放宽本机的版本校验——应该在界面上**如实显示警告**，
让用户知道自己在跑不受支持的组合。等哪天 DSH 真的用了 24 才有的 API，
这个警告就是唯一线索。

---

## 四、完整验证记录（真实 DSH，非模拟）

| 环节 | 结果 |
|---|---|
| 安装 | 6 分 11 秒，656 个包，`~/.npm-global` |
| `dsh --version` | `0.2.2-rc.1`（node 22.23.3） |
| 首次启动 | **8.9 秒**，端口 38513，pid 1650033 |
| 第二次 `ensure_running` | **0.4 秒，`reused=True`**（复用判据成立） |
| 隧道 | 0.6 秒，本地 13436 → 远端 38513 |
| 裸访问 `/` | `401 dsh web authentication required` |
| 带令牌 + Cookie 访问 | **200**，返回 DSH 页面 HTML |

最后一条要注意：**令牌必须配合 Cookie 才有用**。第一次请求
`/?token=xxx` 会 `Set-Cookie`，之后靠 Cookie 维持会话；不带 Cookie 反复
请求永远 401。QtWebEngine 自带 Cookie 存储，所以真实使用没问题——
但写探针时容易误判成"令牌无效"（我这次就误判了一轮）。

---

## 五、给实现的四条要求

1. **安装对象固定**：只 `npm i -g @deepseek-ai/dsh@<tag>`，tag 由用户选
   （latest / next / alpha），不接受任意包名。
2. **装完必须验 PATH**：跑一次 `sh -lc 'command -v dsh'`，找不到就补
   `~/.profile`（幂等，先 grep 再写），然后再报成功。
3. **不要静默提权**：没有免密 sudo 时不要尝试，直接走用户级，并说明
   "这次装在你的 home 下，不进系统目录"。
4. **展示 EBADENGINE 警告**：node 版本不满足时照实说，不要因为"实测能跑"
   就吞掉。
