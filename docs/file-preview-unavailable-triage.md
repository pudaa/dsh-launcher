# 「文件资源服务不可用」归因排查

- 排查日期：2026-09-22
- 现象：在右侧边栏打开某个文件预览时，面板显示 `文件资源服务不可用`
- 环境：桌面壳 PySide6 6.9.3 / QtWebEngine **Chromium 130.0.6723.192**，DSH **0.1.6-alpha.2**，端口 3080

## 结论（先行）

**不是我们桌面壳的代码问题，也不是 DSH 服务端的问题。**

最可能的定性：**DSH 0.1.6-alpha.2 的前端对浏览器内核的要求已经高于 QtWebEngine 6.9 提供的 Chromium 130。**

这条提示是 DSH 官方前端在「拿不到文件资源 provider」时的**正常降级文案**，不是报错，也不是我们注入的。

> **后续（2026-09-22 已闭环）**：按本文假设把 PySide6 从 `6.9.3` 升到 `6.11.2`
> （QtWebEngine 内核 **Chromium 130 → 140.0.7339.225**），重新用 Nuitka 打包后，
> **文件预览恢复正常**（老大人工核验：右侧边栏 `README.md` 正常渲染 Markdown）。
> 因此「壳内核偏旧」这一判断**已得到直接证实**——文中「尚未确证」一节记录的是
> 升级前的状态，保留作为排查过程的存档。

## 证据链

| # | 结论 | 证据 |
|---|---|---|
| 1 | 文案出自 DSH 官方前端 | `dsh-client-ui-sidebar-documentpreview/lib/client.js:1350` → `resourceUnavailable: "文件资源服务不可用"`。全项目检索 `D:\PersonApps\dsh-launcher` **零命中** |
| 2 | 触发条件：资源服务的 provider 未注册 | 同文件 `:807` → `meta.status === "none"` 时渲染该文案；`dsh-client-resources/lib/client.js:79,117,130` → `status "none"` 仅当 `dsh-resource://` 协议没有注册 provider |
| 3 | provider 由 DSH 官方包注册 | `dsh-api-workspace-files/lib/client.js:460` → `ctx.resources.register(provider)`；其 `inject = ["resources","remote","remote.workspaceFiles"]` |
| 4 | **宿主侧完全正常** | 直接向宿主发 RPC：`POST /api/workspaceFiles/list` → **200**，返回 `D:\Codes\MemoryServerTTS` 真实目录（24 条目）。对照组 `/api/noSuchService/nope` → 404 |
| 5 | 配置层面该插件是启用的 | `dsh --profile web --dump-config` 中 `- id: workspace-files / name: @deepseek-ai/dsh-api-workspace-files`，**无 `disabled`** |
| 6 | 前端资源下发正常 | boot 清单含 58 个模块，`dsh-api-workspace-files/client.js` 等模块 URL 均 200、内容完整 |
| 7 | 不存在陈旧缓存 | 主页 `cache-control: no-store`；模块 `public, max-age=31536000, immutable` + `rev` 哈希 |
| 8 | **标准 Chromium 下完全正常** | 用 Edge（headless + CDP）走同一条路径：右栏 → 工作区文件 → 点击 `environment.yml` → 预览面板 `data-textpreview-state="text"` **正常渲染全文**；`/api/workspaceFiles/list|read|stat` 全 200；控制台零输出、零网络失败 |
| 9 | **内核能力差距已确证** | `dsh-client-ui-sidebar-documentpreview/lib/client.pdf.js` 内嵌 **PDF.js 6.3.289**，使用 `RegExp.escape`（Chromium **136+**）、`Uint8Array.fromBase64`（Chromium **140+**）、`URL.parse`（126+）。Chromium 130 **不支持前两者** |

补充：主 bundle（`index-8VXBH-f-.js` / `vendor-CCJJTK99.js`）**没有**使用超版本 API，其中的 `fromBase64` 是自定义函数名（内部用 `Uint8Array.from(atob(...))` 兼容写法）。

## 尚未确证

- **没能在壳的内核里复现该报错。** 用独立 QtWebEngine 探针加载本机 DSH 页面时，连 `about:blank` 都返回 `loadFinished=False`（该探针环境本身不完整），因此「内核版本 → 这条报错」是**目前最合理的解释，但还不是直接观测到的事实**。

## 可立即验证的推论（零成本）

若「内核偏旧」成立，则 **PDF 预览在壳里必然失败**——因为 `RegExp.escape` 在 Chromium 130 上不存在。

请老大在壳里打开一个 PDF：若报 `预览器 PDF 不可用` 或渲染中断，则该假设得到强力支持。

## 建议的下一步

1. **排除 profile 脏状态**（1 分钟）：把壳的浏览器 profile 改名后再启动，等价于清空浏览器侧状态。
   `%LOCALAPPDATA%\DSH-Web\profile` → `profile.bak`
2. **取证**（能一锤定音）：给壳加诊断开关，启动前设 `QTWEBENGINE_REMOTE_DEBUGGING=<port>`，复现时用 CDP 抓控制台，即可看到确切的 JS 异常。
3. **升级内核**（很可能需要做）：PySide6 `6.9.3` → `6.11.x`（当前最新 6.11.2），内核由 Chromium 130 提升到 ~142，覆盖 DSH 所需能力。

## 附：本次使用的探针方法（可复用）

- 宿主能力直测：`POST /api/<namespace>/<method>`，body `{"type":"client-request","rpcId":"x","method":"<namespace>/<method>","payload":{"args":{...}}}`，凭 cookie 认证。**endpoint 未被认领时返回 404 `not found`**，被认领则返回 RPC 结果信封 → 可据此判定宿主是否注册了某服务。
- 标准浏览器对照：Edge `--headless=new --remote-debugging-port=<p>`，用 CDP `Runtime.evaluate` 驱动 UI 并读取 `[data-textpreview-state]`。
