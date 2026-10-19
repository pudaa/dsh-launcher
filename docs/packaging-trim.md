# Nuitka 打包裁剪调研（2026-10-19）

**结论先说**：可以裁，但收益不在"体积"，在"启动"。

- 体积：125.2 MB → **106.9 MB（-14.6%）**，砍不到"大幅"——60% 的体积是 Chromium 本体，动不了。
- 启动：**每次启动省 1.0–1.5 秒**（448 ms/文件数相关，见 §4），冷启动省 2.4 秒。
  这比体积收益重要得多，且是**每次启动**都有，不是只有首次。

本文所有数字均为本机实测（Python 3.13.14 / PySide6 6.11.2 / Nuitka 4.2.1 / Windows），
构建命令与 CI 一致，仅把 `--lto=yes` 关掉做对照（LTO 只影响我们自己的代码段，约 1–2 MB）。

---

## 1. 当前产物构成（467 MB 未压缩 / 443 文件）

| 项 | 原始 | 说明 |
|---|---|---|
| `qt6webenginecore.dll` | 203.5 MB | Chromium 本体，**不可裁** |
| `qtwebengine_devtools_resources.debug.pak` | **75.8 MB** | DevTools 前端的**调试符号版**资源，运行时永不加载 |
| `PySide6/translations/` | **61.7 MB / 357 文件** | 304 个 `.qm`（Qt 界面翻译）+ 53 个 Chromium 语言包 |
| `qtwebengine_devtools_resources.pak` | 11.6 MB | DevTools 正式资源，**要留**（我们有 InspectorWindow） |
| `icudtl.dat` | 10.5 MB | ICU 数据，不可裁 |
| `qt6core/gui/widgets/network/opengl` | 40.2 MB | 不可裁 |
| `qt6quick + qml 族` | 13.9 MB | **不可裁**：`qt6webenginecore.dll` 的 import 表硬依赖 |
| `libcrypto/libssl` | 9.6 MB | WebEngine TLS，不可裁 |
| `qt6pdf.dll` | **4.6 MB** | 只被 `imageformats/qpdf.dll` 依赖，WebEngine 的 PDF 走 Chromium 内置 PDFium |
| `*.debug.pak`（3 个） | **3.1 MB** | 同上，调试版资源 |

**为什么 `qt6quick` 不能砍**：实测 `qt6webenginecore.dll` 的导入表里有
`Qt6Quick.dll / Qt6Qml.dll / Qt6Positioning.dll / Qt6WebChannel.dll`。
这是 Qt6 的设计（WebEngineCore 内部用 QML 实现部分对象），不是 Nuitka 多收。

**为什么 `qt6webenginecore.dll` 也裁不动**：PE 段检查过，11 个段，
`.text` 148.5 MB / `.rdata` 44.4 MB，**没有 `.debug` 段**——PySide6 wheel 里已是 release 构建，
没有可剥离的符号。

## 2. 削减项与实测结果

| 削减 | 原始 | 压缩后（zstd） | 风险 |
|---|---|---|---|
| `*.debug.pak`（4 个） | 78.9 MB | −15.5 MB | 无（调试符号，永不加载） |
| `PySide6/translations/*.qm` | 15.9 MB | −4.5 MB | 无（项目没装 `QTranslator`） |
| `qtwebengine_locales/*.pak` 只留 4 个 | 44.4 MB | −11.7 MB | 低（见 §3） |
| `qt6pdf.dll` + `qpdf.dll` | 4.7 MB | −2.5 MB | 低（PDF 由 Chromium 自己渲染） |
| **合计** | **143.9 MB** | **−34.2 MB** | |

实际产物：**467 MB / 443 文件 → 327 MB / 94 文件**，onefile 压缩后 **126.0 → 106.9 MB**。

> 注意：逐文件估算的 −34.2 MB 偏乐观，实际 −19.1 MB。原因是省下的内容本身压缩率低
> （`.debug.pak` 压缩比 17%、locale `.pak` 已内部压缩），不能按整体 26.76% 的压缩比外推。

## 3. 必须保留的语言包

Chromium 按系统 locale 加载 `qtwebengine_locales/<locale>.pak`（项目还会主动注入
`GetUserDefaultLocaleName`）。缺 pak 时 UI 文案会空/回退，属于"体验受损但不报错"的隐形问题。

**保留 `zh-CN` / `zh-TW` / `en-US` / `en-GB`**（合计约 2.4 MB 原始）。其余 49 个删掉。

## 4. 启动收益的真正来源：文件数，不是字节数

同一份源码、同一台机器，只差裁剪：

| | 冷启动（需解包） | 热启动（缓存命中） |
|---|---|---|
| 未裁剪（443 文件 / 467 MB） | 7882 ms | 4005 / 4309 ms |
| 裁剪后（94 文件 / 327 MB） | 5501 ms | 2309 / 2439 / 3030 ms |
| 差 | **−2.4 s** | **−1.0 ~ −1.5 s** |

**因果验证**：往裁剪版已解包的目录里塞 350 个空文件（共 0.35 MB），热启动立刻回到
3405–4229 ms；删除后回到 2309–2439 ms。→ 开销是**每文件固定成本**
（Defender 实时扫描 + 文件系统元数据），与字节数无关。

这解释了为什么"体积只减 15%，启动却减 30%"：文件数从 443 掉到 94（−79%）。

> 测量口径：`DSH_HOST_RESOURCE_CHECK` 分支（自检后立即退出，不起 DSH 服务、不进主窗口），
> 所以绝对值不含 DSH 服务就绪的 ~7 s，只有"解包 + Qt 初始化 + 自检"这一段。
> 热启动绝对值里仍有约 2.3 s 是 Qt/WebEngine 初始化，与裁剪无关。

## 5. 实现方式（踩过的坑）

### 5.1 `--include-data-files` 回补**无效**

想"先全排再补回 4 个语言包"是行不通的：`--noinclude-data-files` 是**最终过滤器**，
回补的文件会被一起吃掉（实测 dist 里 translations 目录直接为空）。
所以必须用**精确排除列表**，在 CI 里循环生成：

```bash
LOC="$SP/translations/qtwebengine_locales"
NOINC=()
for f in "$LOC"/*.pak; do
  case "$(basename "$f")" in
    zh-CN.pak|zh-TW.pak|en-US.pak|en-GB.pak) continue ;;
  esac
  NOINC+=("--noinclude-data-files=PySide6/translations/qtwebengine_locales/$(basename "$f")")
done
```

`--noinclude-data-files` / `--noinclude-dlls` 匹配的是**目标路径**（dist 内的相对路径），
不是源路径。根目录的 `*.debug.pak` 直接写 `*.debug.pak` 即可。

### 5.2 完整参数增量

```bash
  --noinclude-data-files="*.debug.pak" \
  --noinclude-data-files="PySide6/translations/*.qm" \
  --noinclude-dlls="qt6pdf.dll" \
  --noinclude-dlls="PySide6/qt-plugins/imageformats/qpdf.dll" \
  "${NOINC[@]}" \
```

### 5.3 预期警告

排除 `qt6pdf.dll` 后 Nuitka 会打：

```
Nuitka-Onefile:WARNING: Distribution folder is missing expected file 'qt6pdf.dll'. This might cause issues.
```

这是**预期**的（插件有硬编码的 DLL 列表），不是失败。

## 6. 验证方式（落地时必须做）

现有 `resource_report()` 只覆盖图标 / QtSvg / 自更新，**不覆盖 WebEngine**。
本轮验证用了三层：

1. 裁剪版 `--standalone` 跑 `DSH_HOST_RESOURCE_CHECK` → 报告"结论=通过"，
   SVG 上色比例 0.313、图标 17 个、自更新可用。
2. 资源清单核对：`icudtl.dat` / `qtwebengine_resources*.pak` / `v8_context_snapshot.bin` /
   `QtWebEngineProcess.exe` / `qtwebengine_devtools_resources.pak` 全部在位。
3. **真实启动验证（2026-10-19，已通过）**：老大在实机直接跑 `build/DSH-Web-trim.exe`，
   应用正常启动、DSH 页面正常渲染。命令行下 offscreen 模式因本机 GPU 上下文创建失败
   （`Failed to create GLES3 context`）无法自动化，所以只能靠实机人工确认；
   CI 里若要做黑盒检查，需要一条"打开窗口并等待 `loadFinished`"的用例。

## 7. 不建议动的

| 项 | 理由 |
|---|---|
| `--include-windows-runtime-dlls=no` | 省不到 1 MB，却要求目标机预装 VC 运行库 |
| 砍 `qtwebengine_devtools_resources.pak`（11.6 MB） | 会直接废掉 `InspectorWindow` |
| 砍 `qt6test.dll`（0.38 MB） | 收益可忽略，还会破坏 `gui_selftest` 的 QtTest 依赖 |
| 提高 onefile 压缩级别 | Nuitka 4.2 没有该选项，只有 `--onefile-no-compression`（反方向） |

## 8. 如果要进一步提速

体积这条路到头了（Chromium 占 60%）。启动的下一站不在打包参数：

- `--onefile-as-archive`（未实测）：payload 换归档格式，可能改善解包；仍需验证自更新链路。
- 热启动那 ~2.3 s 是 Qt + WebEngine 初始化，属上游成本。
- DSH 服务就绪的 ~7 s 是 DSH 自己的，桌面壳侧只能做感知优化（窗口先出 + 进度）。

---

## 9. 裁掉语言包会不会影响 i18n？

**对"我们自己的 i18n"没有影响；只影响"Qt / Chromium 原生界面文案跟不跟随系统语言"。**
两类翻译性质不同，要分开看。

### 9.1 `.qm`（304 个，15.9 MB）—— 现在是**死文件**

Qt 框架自身控件的文案翻译，**只有我们主动 `QTranslator.load()` 才生效**。
项目从头到尾没装过 translator，这 304 个文件运行时一次都不会被读到。

将来做多语言时：

- 我们自己的文案（面板、菜单、对话框）→ 自己维护 `.qm`，用
  `pyside6-lrelease` 编译，跟 PySide6 自带的这批**没有关系**。
- 唯一会用到 Qt 自带 `.qm` 的场景：**调用 Qt 原生标准控件并希望它跟随语言**，
  例如 `QMessageBox` 的 OK/Cancel、`QFileDialog` 的按钮、`QLineEdit` 右键菜单。
  目前 `dsh_theme.MessageDialog` 是自绘的、也没用 `QFileDialog` → 现在无影响。
- 真到那一天，加回 `qtbase_zh_CN.qm`（约 100 KB）+ `qt_zh_CN.qm` 即可，
  不需要整套 304 个。

### 9.2 `qtwebengine_locales/*.pak`（53 个，45.8 MB）—— 现在**留了 4 个**

这是 **Chromium 内建 UI** 的文案：网页右键菜单（复制 / 另存为 / 检查）、
PDF 查看器、证书错误页、DevTools 界面。与 DSH 前端自己的 i18n 无关。

- 保留 `zh-CN` / `zh-TW` / `en-US` / `en-GB`，覆盖中文用户 + 英文兜底。
- 系统语言是其它语言时（日 / 韩 / 法…），Chromium 会回退到 `en-US` ——
  **不会空白，只是英文**。对当前用户群可接受。
- 将来要支持某语言，就把对应 `<lang>.pak` 加回白名单即可
  （单个 0.6–1.7 MB，压缩后约 0.3–0.7 MB），**不需要改任何代码**。

### 9.3 给将来 i18n 留的接口

CI 里的白名单要写成变量，而不是散在命令行：

```bash
KEEP_LOCALES="zh-CN zh-TW en-US en-GB"     # 加语言 = 往这里加名字
```

将来的取舍：如果产品真的要做"界面语言可选"，`qtwebengine_locales` 的
保留集会随语言数线性增长（每语言约 0.5 MB）。到那时更合适的做法是
**做一个"全语言"构建变体**，而不是主产物无限膨胀。

---

## 10. 带 `-beta` 的版本发布注意

版本号写成 `X.Y.Z-beta.N` 后，`version_tuple()` 已按 semver 正确排序
（`beta.10 > beta.9`、`beta < 同号正式版`，2026-10-19 修复，两处自测已覆盖）。

发布时还有两件事要盯：

1. **Release 页面上要勾 `prerelease`**。GitHub 的 `releases/latest` 会跳过
   预发布版，稳定用户就不会被推上测试版。
   三条获取路径（API latest → API 列表 → 页面/Atom 降级）现在**共用
   `accept_release()` 一个判据**（2026-10-19 落地）：不取预发布时，降级路径也不取。
   判据取"或"——API 的 `prerelease` 标记，**或者** tag 文本自带预发布段；
   后者是为了兜住"发 beta 时忘了勾 prerelease"这种情况。
2. `--onefile-tempdir-spec` 里的 `${{ github.ref_name }}` 会变成
   `DSH-Web-runtime-v2.4.0-beta.1`，目录名合法，无需改动。

