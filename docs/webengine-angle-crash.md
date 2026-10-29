# QtWebEngine ANGLE 崩溃（Qt6WebEngineCore +0xd224d3）

> 面向：接手这个项目的 agent / 新会话。
> 读完这份文档你应该能：① 判断一次闪退是不是这个问题；② 判断某个 Qt/PySide6
> 版本该不该升；③ 知道哪几条路已经被试过、为什么不能重走。
>
> 最后更新：2026-10-25。数据都有实测来源，改结论请附证据。

---

## 0. 一句话结论

**Qt 6.11.2 内置的 ANGLE D3D11 `InputLayoutCache` 有一个累积型空指针崩溃，
上游已定位并修复（QTBUG-142165），但截至 2026-10-25 没有任何可安装版本包含它。**
现在唯一正确的动作是**等**——不要降级、不要自愈、不要换渲染后端、不要自己打补丁。

---

## 1. 怎么确认一次闪退是这个问题

**判据按可靠性排序：**

1. **Windows 事件日志 Application / ID 1000**（最硬，壳自己什么都不写）

   ```powershell
   Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000;
       StartTime=(Get-Date).AddDays(-7)} |
     Where-Object { $_.Properties[0].Value -like 'DSH-Web*' } |
     ForEach-Object { $p=$_.Properties
       "[{0}] {1} | {2} | {3} | {4}" -f $_.TimeCreated, $p[0].Value,
         $p[3].Value, $p[6].Value, $p[7].Value }
   ```

   命中形态：
   ```
   DSH-Web.exe | Qt6WebEngineCore.dll | c0000005 | 0000000000d224d3
   ```
   偏移随版本平移（6.11.2 是 `0xd224d3`，6.140.0 是 `0xd23c53`），
   **所以别拿偏移当唯一判据，看模块 + 异常码 + 线程。**

2. **转储文件**：`%LOCALAPPDATA%\CrashDumps\DSH-Web.exe.<pid>.dmp`（约 11–16 MB）

3. **应用侧"没有日志"本身就是特征**：`host.log` 停在最后一条正常业务日志
   （常见是"标题栏上色"），`console.log` 没有 Python traceback。
   崩在 Chromium 的 GPU 线程 C++ 层，Python 层的 `excepthook` / Qt 异常钩子
   完全没有机会执行。**"没留异常"不是日志坏了。**

4. **表象特征**：桌面壳连同托盘一起消失，但 **web 服务还活着**
   （`service.json` 的 pid 仍有效、端口 3080 仍响应）。这是
   `DETACHED_PROCESS` 脱离 Job Object 的预期行为，不是 bug。

5. **崩溃线程**：`Chrome_InProcGpuThread`（用 cdb / WinDbg 看 dump 才能确认，
   无 cdb 时靠判据 1 即可）。

> ⚠️ 别用 `PrintWindow(PW_RENDERFULLCONTENT)` 判断"网页有没有在画"——
> 它会触发重绘，把问题伪装成正常。要看用户像素就用屏幕 BitBlt。

---

## 2. 根因：一个 4 字节的 padding

`rx::PackedAttributeLayout` 是 ANGLE D3D11 的 `InputLayoutCache` 的 key：

```cpp
struct PackedAttributeLayout {
    uint32_t numAttributes;                  // 4 字节
    // ← 这里有 4 字节 padding（attributeData 要 8 字节对齐），从来没人写过
    gl::AttribArray<uint64_t> attributeData; // 8 字节对齐
};
```

这个结构体**整体被当 raw data 做哈希**，而 padding 是栈上残留的垃圾值。于是：

```
同一个 attribute 布局 → 这一次 hash 是 A，下一次 hash 是 B
→ 哈希表里同一个逻辑 key 散成多条
→ LRU 淘汰时删掉仍被引用的 d3d11::InputLayout
→ 后面查到悬垂对象 → Resource11Base::getSerial() 读 NULL → 0xc0000005
```

**这解释了为什么总是长会话才崩**：不是随机崩溃，缓存要慢慢坏到命中悬垂条目才发作。
实测三次（10-20 / 10-24 / 10-25）都在连续使用几十分钟之后。

上游修复（ANGLE `bbf320aa3`，chromium:565740567）实质就一处：

```cpp
+  uint32_t numAttributes                  = 0;
+  uint32_t pad0                           = 0;   // 显式占住 padding 并清零
+  gl::AttribArray<uint64_t> attributeData = {0};
```

`pad0` 占的就是原来的 padding 位 ⇒ **结构体大小与成员偏移都不变，ABI 兼容**。

---

## 3. 各版本状态（2026-10-29 实测）

| 构建 | Chromium | DLL PE 构建时间 | 指纹 | 含修复？ |
|---|---|---|---|---|
| PySide6 6.11.2 | 140.0.7339.225 | 2026-08-13 01:59 UTC | 0xd21aa3 | ❌ |
| PySide6 6.12.0 → WebEngine 6.12.0.140 | 140.0.7339.225 | 2026-09-27 12:38 UTC | 0xd23253 | ❌ 同上，**逐字节相同** |
| 修复 backport 合入 `140-based` | — | **2026-10-08 17:56 UTC** | — | （分水岭） |
| **PySide6 6.11.3**（Addons wheel） | **140.0.7339.264** | **2026-10-21 01:32 UTC** | **未找到** | ✅ **已含修复**（2026-10-29 判定，退出码 0） |

- **PySide6 6.11.3 已发布**（PyPI 上传 2026-10-26 12:49 UTC；wheel
  `pyside6_addons-6.11.3-cp310-abi3-win_amd64.whl`，160.6 MB，
  sha256 `94a17b92…24f86f` 已核）。两条判据同时满足：PE 构建晚于 10-08，
  且崩溃点 96 字节基线指纹**完全消失**。Chromium 从 `.225` 进到 `.264`。
  **判定命令**：`python tools/webengine_fix_check.py --wheel <whl>` → 退出码 0。
  > 仍待实机长会话浸泡（见 §6 第 2 步）后才改 `requirements.txt`。
- PySide6 `6.12.0.1`：官方开发笔记（10-22）称"下周"，**至 10-29 PyPI 仍无**；
  `PySide6_WebEngine` 最新仍只有 `6.12.0.140`（10-08，不含修复）。`6.12.1` 定 11-17。
- Qt 已停止公开的桌面 daily snapshot（`download.qt.io/snapshots/` 只剩 harmonyos）。
  ⇒ **含修复的 Windows 二进制现在只能从 PyPI 的 6.11.x 线拿到**（6.11.3），
  6.12 拆包线还要等。


**注意 6.12 的结构性变化**：WebEngine 从 PySide6 主包**拆出去了**，变成独立 wheel
`PySide6_WebEngine==6.12.0.140`，版本号改按 Chromium 基线走。证据：
`PySide6_Addons` 的 wheel 从 160.4 MB 掉到 38.6 MB。升级到 6.12 不只是换 DLL，
打包链路也要跟着改。

---

## 4. 已经被否掉的四条路（**别重走**）

### 4.1 崩溃后自动降级软件渲染 —— 已撤，不要再加回来

2026-10-24 上线过"崩溃哨兵"（启动写 `webengine-state.json`、正常退出删、下次读到
就加 `--disable-gpu` 并弹托盘提示），10-25 撤掉。撤掉的理由不是它有 bug：

1. **降级是概率缓解，不是修复**，而且不彻底——10-24 14:58 那次崩溃就发生在
   `--disable-gpu` 状态下（走另一条 `Qt6Core+0x2623cc` 路径）。
2. **用户勾了硬件加速，程序不该背着他改配置。** 自动降级与用户手动关掉，面对的是
   同一个"关掉之后仍可能崩"的事实，区别只在于把落差藏起来。

现在 `dsh_gui_qt.py` 里 `_HW_ACCEL` **只读用户设置**，没有任何自动干预。
`tools/gui_selftest.py` 有反向断言钉住（`CRASH_FALLBACK_WINDOW_S` 等符号必须不存在）。

### 4.2 崩溃后自动拉起壳 —— 明确不做

用户 2026-10-25 的决策：自动拉起和用户手动打开，心理落差一样，都改变不了
"问题已经发生"。**不要加看门狗、不要加自愈。** 托盘消失 = 进程全灭 = 手动重开。

### 4.3 换 ANGLE 后端绕开 D3D11 renderer —— 实测白屏

推论很干净（崩溃栈全在 `rx::` 即 D3D11 renderer，换后端就不构造
`PackedAttributeLayout`），**但实测跑不出画面**：

| flags | 实际 renderer | Qt grab 非白像素 | 判定 |
|---|---|---|---|
| 默认 | AMD 780M **Direct3D11** vs_5_0 | **30.0%**（青绿 25.6%） | 正常 |
| `--use-angle=gl` | AMD 780M **OpenGL 4.5.0** | **0.0%** | **白屏** |
| `--use-angle=vulkan` | RTX 4060 Vulkan 1.4.341 | **0.0%** | **白屏** |
| `--use-angle=d3d9` | AMD 780M **Direct3D9Ex** | — | WebGL 降到 1.0，EGL 报错 |

gl / vulkan 的 `WebGL renderer` 字符串正确、context 也建成了（**看起来切过去了**），
但日志里 `RasterDecoderImpl: Context lost during MakeCurrent` /
`CreateSharedImage failed`，合成输出全白；延长到 5s 重抓仍全白（排除抢跑）。

> 历史坑：`build/probe-results/crash/report.md` §8 曾把这两项评为"低中风险、
> 崩点与后端无关"，**那个评级是错的**——崩点确实在 D3D11 backend 专属代码里，
> 但换后端在 Windows 上根本没画面。§13 已补记更正。

### 4.4 自己打这个补丁 —— 成本远高于等

| 做法 | 为什么不行 |
|---|---|
| 重新编译 QtWebEngineCore.dll | Chromium 140 单体 DLL，100+ GB 磁盘、官方建议 32–64 GB 内存（本机 16 GB），且要与 PySide6 的 configure 完全一致 |
| 二进制 patch DLL | ABI 不变使理论可行（每个构造点补 `mov dword [this+4], 0`），但无 PDB 盲定位、release 下构造函数多处内联、每次升级重做、打错崩得更凶、分发第三方二进制麻烦 |

---

## 5. 工具

### 5.1 `tools/webengine_fix_check.py` —— 判断某版本该不该升

```bash
python tools/webengine_fix_check.py                 # 检查当前装的 PySide6
python tools/webengine_fix_check.py --wheel x.whl   # 检查一个 wheel（不必安装）
python tools/webengine_fix_check.py --dll path.dll
python tools/webengine_fix_check.py --json          # 给 agent 解析
```

判据两条，任一不满足都别升：① PE 构建时间晚于 2026-10-08 17:56 UTC；
② 崩溃点 96 字节机器码与基线**不再逐字节相同**。

退出码：`0` 已含修复 / `1` 不含 / `2` 需人工复核 / `3` 找不到 DLL。

输出示例（当前 6.11.2）：

```
DLL        : ...\PySide6\Qt6WebEngineCore.dll
大小       : 194.0 MB
PE 构建时间 : 2026-08-13 01:59:56 UTC
Chromium   : 140.0.7339.225
修复合入时刻 : 2026-10-08 17:56:00 UTC
崩溃指纹   : 找到（0xd21aa3）
--------------------------------------------------------
[NO]   不含修复
理由       : 崩溃点 96 字节机器码与 6.11.2 基线逐字节相同（代码未改）
```

实现要点（改这个脚本前先读）：
- 指纹 = 96 字节，`BASELINE_96` 常量。**必须全文件扫**——第一版把搜索上限设成
  8 MB 而崩溃点在 13.8 MB，直接漏检；这种错只表现为**假阴性，不报错**。
- 判定优先级：**指纹命中 > 时间戳**。指纹命中就说明代码没被动过，无论时间多新。
- 指纹消失但时间早于合并点 → `SUSPICIOUS`，不能当已修（可能是编译器差异）。

### 5.2 `tools/probe_angle_backend.py` —— 验证渲染后端 / 抓画面

```bash
python tools/probe_angle_backend.py                      # 默认 D3D11（应正常）
python tools/probe_angle_backend.py --use-angle=gl
python tools/probe_angle_backend.py --use-angle=vulkan
```

输出 `WebGL renderer` 字符串 + 两张图：`shot.png`（Qt 内部 grab）、
`screen.png`（屏幕 BitBlt，抓用户实际看到的像素）。
**判断"有没有画面"要看非白像素比例**，不要只看 renderer 字符串——
后者在 gl/vulkan 下也是对的，但画面是白的。

### 5.3 取证材料（不随产品发布）

- `build/probe-results/crash/report.md` —— 完整取证（自研 `.pdata` unwind 38 帧、
  机器码反汇编、上游对比），§13 是 2026-10-25 的补记
- `build/probe-results/crash/frames_*.json` —— 帧链原始数据
- `%LOCALAPPDATA%\CrashDumps\DSH-Web.exe.<pid>.dmp` —— WER 转储

---

## 6. 新版本出来时怎么走（标准流程）

1. `python tools/webengine_fix_check.py --wheel <新 wheel>` → 看结论与退出码。
2. `0`（已含修复）：再跑一次长会话浸泡（≥2h，加载真实 DSH 页面），确认不再出现
   `d224d3` 类事件；然后才动 `requirements.txt` 的版本约束。
3. `1`（不含）：**别升**，别为了"版本新"而升——6.12.0 就是白升的样本。
4. `2`（需复核）：先看 PE 时间戳是否真的晚于 2026-10-08，再考虑实机验证。
5. 无论哪种，`requirements.txt` 的 `PySide6>=6.11.2,<6.12` 上界都要跟着一起改，
   否则 `pip install -r` 会自动跨到拆包版本。

---

## 7. 相关代码位置

| 位置 | 说明 |
|---|---|
| `dsh_gui_qt.py` 顶部 `_HW_ACCEL` / `_hw_accel_enabled()` | 硬件加速唯一来源（只读用户设置） |
| `dsh_host/config.py` `DEFAULTS["webengine_hw_accel"]` | 默认 True；注释记录了决策理由 |
| `dsh_host/config.py` `purge_legacy_webengine_state()` | 清旧版 `webengine-state.json` |
| `dsh_panel.py` `FLAGS` 元组 | 面板开关文案（不许再承诺"自动降级"） |
| `tools/gui_selftest.py` | "无崩溃哨兵 / 无静默降级" 的反向断言 |
| `docs/desktop-shell-design.md` §20.2 | 设计文档里的完整论述 |

---

## 8. 时间线

| 日期 | 事件 |
|---|---|
| 2026-10-20 19:17 | 首次崩溃（v2.3.7 运行时），当时判为"上游未修" |
| 2026-10-24 12:02 / 14:00 | 又两次；14:58 出现另一条 `Qt6Core+0x2623cc`（自更新/退出路径，独立缺陷） |
| 2026-10-24 | 上线崩溃哨兵 + 一次性降级 |
| 2026-10-25 09:54 | pid 1812 崩于 `+0xd224d3`（2.4.2 版本），服务存活 |
| 2026-10-25 | 撤掉哨兵；查到 QTBUG-142165 官方根因与修复；实测换后端白屏；确认 6.12.0 不含修复 |
| 2026-10-29 22:35 | **PySide6 6.11.3 判定为含修复**（PE 2026-10-21 / 指纹消失 / 退出码 0）；待长会话浸泡 |
| 待定 | PySide `6.12.0.1` / `6.12.1`（拆包线）出来后再判一次，届时可直接跨到 6.12 |
