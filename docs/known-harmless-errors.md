# 控制台里那些可以忽略的报错

排查 DSH Web 界面问题时，开发者工具里会出现几条红色报错。下面几条**已经核到
上游源码层面，不是故障**——看到它们不需要重装、不需要清缓存、也不用升级。

写这份文档的原因：这几条报错长得都很像"接口坏了"，每次看到都会把人引到
"版本没装好 / 网络有问题"的岔路上去。记下来，省一轮排查。

---

## 1. `/api/changes.summary?...&seq=N` → 404

**控制台原文**

```
:3080/api/changes.su…37230f17&seq=1743:1
Failed to load resource: the server responded with a status of 404 (Not Found)
```

**它是什么**：右侧栏「本轮改动」的摘要接口，实现在
`@deepseek-ai/dsh-client-ui-deliverables`（`CHANGED_FILES_PATH = "/api/changes.summary"`）。
注意路径是 **summary**，不是 subscribe——DevTools 会把中间截断成 `changes.su…`，
很容易看错。

**为什么 404 是正常的**：这个摘要的数据源是 `dsh-workspace-changes` 的
**进程内内存**，只在**当前这个 dsh 进程**跑过的轮次里可查。DSH 重启之后，
上一个进程的轮次记录就没了，前端仍拿着会话里记录的 `seq` 去查 → 404。

上游源码里三处写明这是设计：

```js
// client.js:194
/** Forget every state and abandon in-flight reads; a replaced connection
    may reach a Host that no longer serves them. */
// client.js:227
if (response.status === 404) return "missing";
// client.js:1636
/** ... A summary the Host no longer serves leaves no card. */
```

也就是说：**查不到就不画那张卡片**，不弹错、不影响使用。文件改动本身在磁盘和
git 里，摘要只是展示层。

**怎么自证**（不用开浏览器）：

```
GET /api/changes.summary?seq=1   → 400 "Invalid change summary coordinates."
GET /api/changes.diff?seq=1      → 400 "Invalid changed file coordinates."
```

端点存在、返回 400（坐标格式不对）；只有"坐标合法但那个轮次已经不在内存里"
才是 404。两种状态码含义不同，别混。

---

## 2. `[deepseek-account] account read failed` / `field: "profile"`

**控制台原文**

```
client.js:4502 [deepseek-account] account read failed
  Object { field: "profile" }
```

**它是什么**：`@deepseek-ai/dsh-client-ui-settings-account/lib/client.js:4502`
逐字段读取账号信息，某个字段被标成 `failed` 就打这条。`field` 指明是哪一项。

**根因在上游账号接口**，服务端日志（`%LOCALAPPDATA%\DSH-Web\logs\dsh-web.log`）能看到：

```
[deepseek-account] request failed { path: '/auth-api/v0/users/current', status: 403 }
[deepseek-account] response rejected { path: '/api/v0/users/get_user_summary',
                                       status: 202, stage: 'parse-json' }
```

前端对此**有降级建模**（`status === "failed"` 是预期内的状态），界面表现为
**「账号资料暂不可用」**，而登录状态和余额照常显示——因为那两处走的是别的数据源。

**怎么办**：到 **设置 → 账号** 退出登录，再重新登录一次。这一步会刷新账号 token。
如果刷新后 403 依旧，那就是上游账号服务侧的问题，与本地安装无关。

---

## 3. `Failed to create GLES3 context, fallback to GLES2`

**控制台原文**（来自桌面壳 stderr，不是页面 console）

```
[.../gpu/ipc/service/gpu_channel_manager.cc:920] Failed to create GLES3 context, fallback to GLES2.
[.../gpu/ipc/service/gpu_channel_manager.cc:931] ContextResult::kFatalFailure: Failed to create shared context for virtualization.
```

**它是什么**：QtWebEngine 的 GPU 进程建 GLES3 上下文失败，回退到 GLES2。
**带了 `--disable-gpu` 也会出现**——viz 合成器仍要建共享上下文，建不成即回退。

**为什么可以忽略**：回退链是生效的，页面照常渲染。判断依据不是"有没有这两行"，
而是**页面有没有画面**：`mem.log` 里网页进程 WS 在涨、DOM 节点数正常就是正常
（2026-10-25 实测：`--disable-gpu` 下报两次，页面 861 MB WS / 3034 节点，渲染正常）。

## 4. `Message 0 rejected by interface blink.mojom.WidgetHost`

**它是什么**：打开开发者工具（或 DevTools 面板重新布局）的瞬间，WidgetHost 接口
收到一条失效请求被拒。与 `host.log` 里"已打开开发者工具"**逐秒对应**。

**结论**：DevTools 的常规噪音，与应用功能无关。

## 5. `The AudioContext was not allowed to start`

**控制台原文**：`widget.js:129 The AudioContext was not allowed to start. ... dshwvAudio`

**它是什么**：`@opencode2dsh/dsh-plugin` 的鲸鱼挂件在用户手势之前就创建了
AudioContext，被 Chromium 自动播放策略拦下，一次加载连报 7–8 条。

**结论**：插件侧写法问题，无害（挂件不出声而已）。**不要为此去改浏览器标志。**

---

## 判断标准：什么情况才真的要动手

| 现象 | 结论 |
|---|---|
| 只有上面几条红字，界面功能正常 | **忽略**。这是上游设计 + 账号数据降级 |
| 出现 `GLES3 context` / `WidgetHost` / `AudioContext` 报错，页面正常 | **忽略**，见 §3–§5 |
| 右侧栏「本轮改动」看不到历史轮次 | 正常。重启后只有当前进程跑过的轮次有摘要 |
| 账号页显示「账号资料暂不可用」 | 退出重登一次；仍不行就是上游 |
| 白屏 / 页面加载不出来 / 接口 500 | 这才是真故障，按正常流程排查 |
| 桌面壳连同托盘一起消失、无任何 traceback | **不是**这几条噪音。按 `docs/webengine-angle-crash.md` 查 |
| 大量 404 且**功能确实缺失** | 先比对"前端实际加载的字节"与磁盘版本，别只看版本号 |

**一条通用经验**：控制台里 `client.js:4502` 这种行号，直接去磁盘上的同名文件
找**同一行**，能读到触发条件和降级逻辑。版本对得上时，这是最快的归因方式——
比翻日志、猜端点都快。
