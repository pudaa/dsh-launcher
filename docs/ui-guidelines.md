# DSH 官方美术规范（Qt 转译版）

> 这份文档解决一个问题：**DSH 的美术是 Web 的，我们的壳是 Qt 的。**
> 颜色可以照抄，但层次、焦点、动效、控件的尺寸语义必须逐条翻译，
> 否则会出现"颜色对了但气质不对"的界面。
>
> 规范来源：DSH 0.2.1-rc.2 安装目录下的
> `@deepseek-ai/dsh-client-ui-theme/lib/client.js`（`--dsw-*` / `--dsh-*` 令牌）
> 与各 `dsh-client-ui-*` 组件的 CSS。**不要凭观感调色。**
> 代码侧唯一实现是 `dsh_theme.py` + `dsh_panel.py`。

## 1. 令牌从哪来（可复现）

```bash
# 官方令牌（字体、圆角、动效、静态色阶）
grep -o -- '--dsw-[a-z0-9-]*:[^;]*' \
  "$DSH_INSTALL/node_modules/@deepseek-ai/dsh-client-ui-theme/lib/client.js" | sort -u

# 组件里出现频率最高的字号 / 行高 / 控件高度
# （对 dsh-client-ui-*/lib/client.js 统计 font-size / line-height / height）
```

设计令牌是**单一事实来源**：面板色表在 `dsh_panel.PALETTE_*`，转译层
（字号/圆角/间距/动效）在 `dsh_theme.py`。任何地方再写一份色值都算缺陷。

## 2. 颜色

语义令牌 → 我们面板里的键名（浅色 / 深色）：

| 令牌 | 键 | 浅色 | 深色 | 用途 |
|---|---|---|---|---|
| `--dsw-alias-bg-base` | `bg` | `#ffffff` | `#151517` | 窗口底 |
| `--dsw-alias-bg-layer-1` | `card` | `#ffffff` | `#232324` | 卡片 / 弹层 |
| `--dsw-specific-sidebar-fill` | `side` | `#f9fafb` | `#1b1b1c` | 侧栏 / 标题条 |
| `--dsw-alias-label-primary` | `text` | `#0f1115` | `#f9fafb` | 主文字 |
| `--dsw-alias-label-secondary` | `dim` | `#61666b` | `#cfd3d6` | 正文 |
| `--dsw-alias-label-tertiary` | `muted` | `#81858c` | `#adb2b8` | 说明 |
| `--dsw-alias-label-caption` | `caption` | `#adb2b8` | `#81858c` | 角标 |
| `--dsw-alias-border-l2` | `line` | `#0000001a` | `#ffffff1f` | 描边 |
| `--dsw-alias-interactive-bg-hover` | `hover` | `#2631480f` | `#ffffff14` | 悬停底 |
| `--dsw-specific-sidebar-nav-item-active` | `nav_active` | `#ebeef2` | `#43454a` | 选中底 |
| `--dsw-alias-link` | `accent` | `#4176e6` | `#679efe` | 链接 / 焦点 |
| `--dsw-alias-button-primary-fill` | `primary_fill` | `#0f1115` | `#f9fafb` | 主按钮底 |
| `--dsw-alias-state-success-primary` | `ok` | `#22c55e` | `#22c55e` | 成功 |
| `--dsw-alias-state-error-primary` | `bad` | `#ec1313` | `#f25a5a` | 失败 |
| `--dsw-alias-state-warn-primary` | `warn` | `#f59e0b` | `#f59e0b` | 警告 |
| `--dsw-alias-scrollbar-hover-l1` | `scroll` | `#d4d4d4` | `#545557` | 滚动条 |

**分层原则（最容易翻车的一条）**

- **浅色主题靠描边分层**：`bg-layer-1/2/3` 在浅色下**全是纯白**，
  靠 `1px` 描边区分层次；不要给浅色卡片加深色底。
- **深色主题靠底色浮起**：卡片 `#232324` 压在 `#151517` 上，描边更淡。

## 3. 字体

官方字体栈（`--dsh-font-family-text-default`）：

```
-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
"Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Helvetica, Arial, sans-serif
```

代码字体（`--dsh-font-family-code-default`）：

```
"SF Mono", "JetBrains Mono", "Fira Code", Consolas,
"Liberation Mono", Menlo, Courier, "PingFang SC", "Microsoft YaHei"
```

Qt 侧统一写法：`FONT = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'`；
等宽用 `"Cascadia Mono", "Consolas", monospace`。

**字号阶梯**（统计自组件 CSS，出现频率最高的几档）：

| 档位 | px / 行高 / 字重 | 用途 |
|---|---|---|
| display | 20 / 28 / 600 | 空状态大标题 |
| h1 | 17 / 24 / 600 | 面板页标题（我们取 19；官方 Web 主面板为 20） |
| h2 | 14 / 22 / 600 | 卡片标题、设置项标题 |
| body | 13 / 20 / 400 | 常规正文 |
| small | 12 / 18 / 400 | 说明、辅助信息 |
| caption | 11 / 16 / 400 | 角标、时间戳 |

**规则：QSS 一律写 px，不写 pt。** pt 会随系统 DPI 换算，和 Web 的视觉尺寸对不上。

## 4. 圆角

| 令牌 | 值 | 用途 |
|---|---|---|
| `--dsw-radius-xs` | 4px | 小角标、内嵌块 |
| `--dsw-radius-sm` | 8px | 按钮、输入框、菜单项 |
| `--dsw-radius-md` | 12px | 卡片 |
| `--dsw-radius-lg` | 16px | 窗口（面板实际取 16） |
| `--dsw-radius-xl` | 20px | 大容器 |
| `--dsw-radius-panel` | 28px | Web 主面板容器 |

官方还有 `--dsw-corner-shape: superellipse(1.5)`（超椭圆圆角）。
**Qt 无法表达超椭圆**，我们用标准圆角近似；这是已知差异，不要试图用路径拟合。

## 5. 间距与控件高度

- 间距节奏是 **4 的倍数**：4 / 8 / 12 / 16 / 20 / 24 / 28。
- 控件高度（官方出现频率最高的几档）：
  - `28px` 紧凑行（设置项行高、标签）
  - `32px` 次按钮 / 小输入框
  - `36px` 标准输入框、设置项控件
  - `40px` 主按钮、搜索框
  - `44px` 触控档
- 面板的页面留白：左右 28、上下 22（比官方 Web 略宽，因为原生窗口没有外层容器）。

## 6. 动效

| 令牌 | 值 |
|---|---|
| `--ds-transition-duration-fast` | 100ms |
| `--ds-transition-duration` | 200ms |
| `--ds-transition-duration-slow` | 300ms |
| `--ds-ease-in-out` | `cubic-bezier(.4, 0, .2, 1)` |

Qt 对应：`QPropertyAnimation` + `QEasingCurve.OutCubic`（`dsh_theme.EASE`）。
**不做逐条错开淡入**（观感像文字在浮动）；整页一次性短淡入，时长 140ms。

## 7. 状态与交互

| Web | Qt 转译 | 备注 |
|---|---|---|
| `:hover` | `:hover` | 底色用 `hover` 令牌 |
| `:active` | `:pressed` | 底色再压 4~6% |
| `:disabled` | `:disabled` | 文字降为 `muted`，不改透明度 |
| `:focus-visible` | **不画焦点环** | 官方在指针模态下 outline 透明；Qt 的系统虚线框必须去掉 |
| `::-webkit-scrollbar` 5px 圆角 | `QScrollBar` 8px 圆角 | 面板改用浮动指示条，见 §9 |
| `box-shadow` 浮层 | `1px` 描边 + 底色层次 | Qt 叠阴影代价高；QWindows11Style 还会给 QMenu 加一层被窗口裁切的投影（§11.2） |

**焦点环的处理要特别说明**：官方用 `:focus-visible` 区分"键盘操作"和"鼠标点击"，
Qt 的 `:focus` 没有这个区分。我们的选择是**一律不画环**，用底色/描边表达选中；
代价是键盘用户的可见焦点变弱，但面板里没有表单流，这个代价可以接受。

## 8. Qt 转译差异（不能硬凑的地方）

| 官方实现 | Qt 的现实 | 我们的处理 |
|---|---|---|
| `superellipse` 圆角 | 只支持圆弧 | 标准圆角近似 |
| 多层 `box-shadow` | 需 custom paint | 描边 + 底色 |
| CSS `var()` 链式 | 无 | 一次性在 Python 里解析成实值 |
| CSS `#RRGGBBAA` | Qt 是 `#AARRGGBB` | `dsh_panel._css()` 统一转换 |
| `:focus-visible` | 无指针/键盘区分 | 一律不画 |
| 半透明 vs 不透明 | 只要文字底下是不透明像素，ClearType 照常 | 详见 §11（早期结论有误，已更正） |
| `transition` | 需 `QPropertyAnimation` | 见 §6 |
| Web 字体回退链 | QSS 支持 `font-family` 列表 | 直接照抄 |

## 9. 落地清单（改界面时对表）

| 位置 | 实现 | 关键点 |
|---|---|---|
| 控制面板 | `dsh_panel.py` | 自绘圆角 + 固定 880×600；三块色带画在裁剪路径内；**贴角的关闭键要自带同半径圆角**，子控件不会被父级裁剪 |
| 托盘菜单 | `dsh_theme.Menu` + `apply_menu` | 圆角前提是半透明底；**每次显示都必须清掉 QWindows11Style 装的 QGraphicsDropShadowEffect**，否则它按矩形裁切，在右下露出黑边（见 §11.2） |
| 提示窗 | `dsh_theme.MessageDialog` | 替代 `QMessageBox`；图标取 Lucide |
| 更新/引导对话框 | `dsh_theme.style_dialog` + `dlgTitle/dlgBody/dlgHint` | 统一字号与按钮形态 |
| 启动加载页 | `dsh_theme.style_loading_page` | 深色界面不闪白 |
| 开发者工具窗口 | `dsh_devtools.InspectorWindow` | 顶部 32px 工具条 + 置顶 |

## 10. 禁止事项

- **不用 Emoji 当图标**（Lucide 内联 SVG，见 `dsh_icons.py`）。
- **不写 pt 字号**、不写系统默认控件的原色。
- **不自己调"看起来差不多"的颜色**：一律取令牌。
- **不在深色主题里用 `#ffffff` 纯白文字**（用 `#f9fafb`）。
- **不给按钮加系统焦点框**。
- **不用 `QMessageBox` / 裸 `QMenu`** 直接上界面；菜单统一走 `dsh_theme.Menu`。

## 11. 半透明只用来做圆角，不是"淡淡透一层"

面板与托盘菜单用了 `WA_TranslucentBackground`，但**只为了让四个角能透出桌面**：
窗口 99.9% 的面积都画着不透明的底色，肉眼看不到任何"半透明"效果。
所以系统里关掉"透明效果"（`HKCU\...\Themes\Personalize\EnableTransparency = 0`）
对观感没有影响 —— 这是预期的，不是失效。

官方文档没有要求半透明；这是 Qt 给无边框窗口做圆角的唯一可行手段
（不透明窗口要么方角，要么用 `setMask` 得到锯齿边）。

### 11.1 文字抗锯齿：半透明**不会**关掉 ClearType

早期曾误判"半透明窗口会让文字变成灰度抗锯齿"。**这个结论是错的**，
当时的实验里文字底下是真正的透明像素。补测（同一段 19px/600 中文、DPR 1.5）：

| 场景 | 彩色边缘像素（次像素特征） |
|---|---|
| 不透明窗口 + 实底 | 5827 |
| 半透明窗口 + 实底 | 5831 |
| 半透明窗口 + 透明底 | 5259 |

只要文字**底下是不透明像素**，Qt 就按 ClearType 渲染。控制面板实测：
整窗采样 295777 个像素里有 1104 个彩色边缘像素 —— **ClearType 是开着的**。

真正影响"加粗文字观感"的是字重：

- 微软雅黑 UI 只有 Light / Regular / Bold，没有 Semibold。
  `font-weight: 600` 会就近匹配到 **Bold(700)**，`QFontInfo.exactMatch()` 为 False。
- 150% 缩放下 13px 文字的 device 尺寸是 24.75px（非整数），
  Qt 按分数尺寸栅格化，笔画宽窄会有 1px 级的不均匀 —— 这是 Qt 与 Chromium
  排版引擎的差异，不是 DPI 适配算错。

要更"细"的观感，只能降字重（600→400 会丢掉层级）或换有 Semibold 的字体族
（如 Inter），代价是中文回退到雅黑时字重又变回 Bold。当前取舍：保持 600。

### 11.2 托盘菜单右边那条"被切割的阴影"：Qt 的 QGraphicsDropShadowEffect，不是 DWM

**现象**：半透明圆角菜单的右/下边缘有一条 3~6px 的灰色硬边，外侧没有渐变，
像是阴影被窗口矩形裁掉了；左/上没有。老大截图里的"被容器切割的阴影"就是它。

**根因**（Qt 6.10/6.11 + Windows 11，实测）：默认样式 `QWindows11Style` 在
`polish()` 里给**每个 QMenu** 装一个 `QGraphicsDropShadowEffect`
（`blurRadius=3`、`offset=(3,3)`、`#3f3f3f` alpha 180），并把菜单设成
半透明无边框。效果画在**窗口内部**，而 QMenu 窗口只和菜单一样大 →
offset 到右下的阴影被窗口边缘硬裁，正好露出 3px。

**为什么之前没修掉**：`NoDropShadowWindowHint` 关的是窗口类/系统阴影，
`DWMWA_NCRENDERING_POLICY` 关的是 DWM 的非客户区渲染，两者都管不到
**控件级 QGraphicsEffect**。实测菜单窗口外侧根本没有 DWM 阴影
（屏幕截图逐像素与背景一致），之前那层 DWM 是误判。

**修法**：
- `dsh_theme.strip_shadow(widget)`：`graphicsEffect()` 是
  `QGraphicsDropShadowEffect` 就 `setGraphicsEffect(None)`；
- `apply_menu()` 在 `setStyleSheet()` **之后**清一次 —— setStyleSheet 会
  repolish，阴影会当场装回来；
- `aboutToShow` 再清一次 —— 第一次 show 的 polish 发生在 apply_menu 之后；
- `dsh_theme.Menu` 子类在 `showEvent` 兜底（覆盖 `show()` 不经过
  aboutToShow 的路径）。

**验证**：离屏 `theme_checks()` 查 `graphicsEffect() is None`，并扫菜单
右/下**直边中段** 3px（避开圆角抗锯齿）不应有半透明暗像素；真机截图确认
右/下与桌面背景一致，无渐变、无硬边。

