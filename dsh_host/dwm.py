# -*- coding: utf-8 -*-
"""标题栏美化：读界面主色调，用系统 DWM 接口给标题栏上色。

设计取向（重要）
----------------
**不做完全自绘。** 用系统原生标题栏，只改它的颜色与材质。
这样：拖动、缩放、Aero Snap、Snap Layouts、右键系统菜单、
多显示器 DPI 切换**全部由系统维持**，零维护成本。
自绘要照顾大量细节且效果通常更差，系统联动也差。

老大的经验（wxPython 时期）值得照搬：**读界面最上方一层像素，
统计主色调，把标题栏调成该颜色** —— 主题自然衔接，不突兀。

为什么取色要用截图而不是读 DOM
------------------------------
另一条路是注入 JS 读 `document.body` 的 computed style。更精确，
但**依赖 DSH 的 DOM 结构**——那就违反了分层铁律（L0 以上不得知道
DSH 内部结构），而 DSH 是 developer preview，DOM 随时会变。

所以走截图路线：只基于"渲染出来的像素"，不管它是什么做的。
代价是截图比较重，因此**只在启动后取一次**，不做持续监听。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------- DWM 常量

# 这些值来自 Windows SDK 的 dwmapi.h。名称里带版本后缀的（如 _19）是
# 预览版 SDK 时期的值，正式版去掉后缀。我们两套都试，取生效的那个。
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38

#: 让 DWM 自己决定颜色（而不是我们指定）
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
#: 让 DWM 按深色/浅色主题自动选对比色（用于文字色）
DWMWA_COLOR_NONE = 0xFFFFFFFE

# 背景材质类型（DWMWA_SYSTEMBACKDROP_TYPE 的取值）
BACKDROP_AUTO = 0
BACKDROP_NONE = 1
BACKDROP_MICA = 2
BACKDROP_ACRYLIC = 3
BACKDROP_TABBED = 4


def available() -> bool:
    """DWM 上色是否可用。需要 Windows 11（Win10 137+ 只有部分支持）。"""
    if not IS_WINDOWS:
        return False
    try:
        _dwmapi()                     # 顺手确认能加载
        build = sys.getwindowsversion().build
        return build >= 22000
    except Exception:                                          # noqa: BLE001
        return False


# ---------------------------------------------------- ctypes 签名（关键）
#
# 必须显式声明 argtypes/restype。省略时 ctypes 会把 Python 整数按 C int
# 封送，而 HWND 在 64 位下是指针宽度 —— 值小的时候碰巧能用，句柄一变大
# 就悄悄传错。声明后由 ctypes 负责转换，且异常变成可捕获的错误。
#
# 另一个坑：DwmSetWindowAttribute 第四参是 cbAttribute，第五参才是
# pvAttribute。ctypes 不做参数名匹配，顺序错了会得到 E_INVALIDARG。

_dwm_declared = False


def _dwmapi():
    """返回已声明签名的 dwmapi，只声明一次。"""
    global _dwm_declared
    api = ctypes.windll.dwmapi
    if _dwm_declared:
        return api
    api.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND,      # hwnd
        wintypes.DWORD,     # dwAttribute
        ctypes.c_void_p,    # pvAttribute
        wintypes.DWORD,     # cbAttribute
    ]
    api.DwmSetWindowAttribute.restype = ctypes.c_long
    api.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.DwmGetWindowAttribute.restype = ctypes.c_long
    _dwm_declared = True
    return api


def _hwnd(widget) -> int:
    """取窗口的原生句柄。

    Qt 的 winId() 返回的是 winId 类型，在 PySide6 里可以直接 int()。
    必须在窗口 show() 之后调用，否则句柄无效（表现为调用成功但无效果）。
    """
    try:
        return int(widget.winId())
    except Exception:                                          # noqa: BLE001
        return 0


def _set_attr(hwnd: int, attr: int, value: int) -> bool:
    """设置一个 DWM 属性。返回是否成功。

    静默失败是这里的常态——旧系统、远程桌面、某些显卡驱动下属性会被忽略。
    所以设计成返回 bool 而不是抛异常，调用方也不需要处理错误。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        v = ctypes.c_uint(value)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(attr),
            ctypes.byref(v), ctypes.sizeof(v))
        return hr == 0
    except Exception:                                          # noqa: BLE001
        return False


def set_caption_color(widget, rgb: tuple[int, int, int]) -> bool:
    """设置标题栏底色。rgb 为 (r, g, b)，各 0-255。

    DWM 期望的是 COLORREF：0x00BBGGRR（注意字节序与直觉相反）。
    """
    r, g, b = rgb
    colorref = (b << 16) | (g << 8) | r
    return _set_attr(_hwnd(widget), DWMWA_CAPTION_COLOR, colorref)


def set_border_color(widget, rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return _set_attr(_hwnd(widget), DWMWA_BORDER_COLOR, (b << 16) | (g << 8) | r)


def set_text_color(widget, rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return _set_attr(_hwnd(widget), DWMWA_TEXT_COLOR, (b << 16) | (g << 8) | r)


def set_dark_mode(widget, dark: bool) -> bool:
    """切换标题栏的深色/浅色模式。

    这一项影响的是系统绘制的部分（关闭按钮、边框高光等），
    光设 CAPTION_COLOR 不改它的话，深色标题栏上会出现浅色系的按钮。
    """
    return _set_attr(_hwnd(widget), DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)


def set_backdrop(widget, kind: int) -> bool:
    """设置背景材质（Mica / Acrylic）。需要 Windows 11 22H2+。"""
    return _set_attr(_hwnd(widget), DWMWA_SYSTEMBACKDROP_TYPE, kind)


def reset(widget) -> None:
    """恢复系统默认外观。"""
    for attr in (DWMWA_CAPTION_COLOR, DWMWA_BORDER_COLOR):
        _set_attr(_hwnd(widget), attr, DWMWA_COLOR_DEFAULT)


# ------------------------------------------------- 验证（只能靠 GDI 截屏）
#
# 重要：DWMWA_CAPTION_COLOR / TEXT_COLOR / BORDER_COLOR 是**只写**属性。
# 对它们调 DwmGetWindowAttribute 一律返回 E_INVALIDARG (0x80070057)，
# **不代表设置失败**。曾经因为拿 Get 的回读当验收标准，误判成"DWM 不可用"。
#
# 另一个坑：Qt 的 QScreen.grabWindow(winId) 在本机返回整片纯色
# （陈旧/空白合成表面），也不能用来验证。
#
# 唯一可靠方式 = Win32 GDI `PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)`。
# 完整实现见 `tools/dwm_verify.py`（会创建真实窗口，不适合放进被测代码）。


def color_near(a: tuple[int, int, int], b: tuple[int, int, int],
               tol: int = 12) -> bool:
    """两色是否足够接近（用于截图验证的容差比较）。

    容差不设 0 是因为标题栏在 Windows 11 上会叠一层轻微的高光渐变，
    截出来的像素不会与设定值逐位相等。
    """
    return all(abs(a[i] - b[i]) <= tol for i in range(3))


# ------------------------------------------------- 窗口像素取色（GDI）
#
# 为什么不用 Qt 的 widget.grab()
# ------------------------------
# `QWidget.grab()` / `QScreen.grabWindow()` 都**不保证**能拿到子窗口的
# 实际绘制内容。实测 `QWebEngineView.grab()` 返回的是 widget 自身的
# 空白背景（整片纯色），因为 WebEngine 的网页在独立合成器里渲染。
#
# Win32 的 `PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)` 直接向系统要
# **窗口的真实像素**，不依赖控件怎么实现绘制。已实测能正确截到窗口
# 标题栏与客户区（见 tools/dwm_verify.py）。
#
# PrintWindow 同样可能拿到陈旧帧，所以取完做「一片纯色」校验，
# 不合格就返回 None 让调用方降级——不要把一个空白色当成主题色。


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


#: PrintWindow 的 flag：不传这个只画客户区，拿不到 DWM 绘制的部分
PW_RENDERFULLCONTENT = 2


def _grab_window_bgra(hwnd: int):
    """GDI 截取整个窗口（含扩展边框）。返回 (BGRA bytes, w, h) 或 (b'', 0, 0)。"""
    if not IS_WINDOWS or not hwnd:
        return b"", 0, 0
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        rect = _RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return b"", 0, 0
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return b"", 0, 0

        hdc = user32.GetWindowDC(wintypes.HWND(hwnd))
        memdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(memdc, bmp)
        user32.PrintWindow(wintypes.HWND(hwnd), memdc, PW_RENDERFULLCONTENT)

        bi = _BMIH()
        bi.biSize = ctypes.sizeof(_BMIH)
        bi.biWidth = w
        bi.biHeight = -h               # 负值 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)

        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(wintypes.HWND(hwnd), hdc)
        return buf.raw, w, h
    except Exception:                                          # noqa: BLE001
        return b"", 0, 0


def capture_window_top(hwnd: int, rows_frac: float = 0.06
                       ) -> tuple[int, int, int] | None:
    """截取窗口**客户区**顶部一条，统计主色调。截不到有效内容返回 None。

    两段式判断（重要，别简化成一段）
    -------------------------------
      · **取色**只看顶部一条（要匹配的是界面顶部的观感）
      · **空白判定**看整个客户区

    为什么不能只拿顶部一条做空白判定：真实界面的顶部 6% 很可能本身
    就是一条纯色工具栏——那是完全正常的，若据此判"没渲染"就会误杀，
    标题栏永远上不了色。

    客户区偏移用 GetClientRect + ClientToScreen 算，不猜：
    PrintWindow 给的是含标题栏与边框的整窗位图，直接裁顶部会取到
    系统标题栏的颜色（那就成了"标题栏跟随标题栏"，毫无意义）。
    """
    data, w, h = _grab_window_bgra(hwnd)
    if not data or w <= 0 or h <= 0:
        return None

    # 客户区在整窗位图中的原点
    try:
        user32 = ctypes.windll.user32
        crect = _RECT()
        user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(crect))
        pt = _POINT(0, 0)
        user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt))
        wrect = _RECT()
        user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(wrect))
        ox = max(0, pt.x - wrect.left)
        oy = max(0, pt.y - wrect.top)
        cw = min(crect.right, w - ox)
        ch = min(crect.bottom, h - oy)
    except Exception:                                          # noqa: BLE001
        ox, oy, cw, ch = 0, 0, w, h

    if cw < 8 or ch < 8:
        return None

    stride = w * 4                      # PrintWindow 按**窗口**宽给的步长

    def _rgba_rows(y_from: int, n_rows: int) -> bytes:
        """取客户区从 y_from 起的 n_rows 行，转成 RGB 连续缓冲。"""
        seg = bytearray()
        for y in range(oy + y_from, min(oy + y_from + n_rows, oy + ch)):
            s = y * stride + ox * 4
            e = s + cw * 4
            if e > len(data):
                break
            row = bytearray(data[s:e])
            row[0::4], row[2::4] = row[2::4], row[0::4]      # BGRA -> RGBA
            seg += row
        return bytes(seg)

    # --- 第一段：整个客户区是否"一片纯色"（= 没渲染出来）---
    full = _rgba_rows(0, ch)
    if not full or flat_ratio(full, cw, ch) >= FLAT_RATIO:
        return None

    # --- 第二段：取顶部一条的主色调 ---
    rows = max(1, min(ch, int(ch * rows_frac)))
    pad = max(0, int(cw * 0.02))
    x0 = max(0, pad)
    span = max(1, cw - pad * 2)

    top = bytearray()
    for y in range(oy, oy + rows):
        s = y * stride + (ox + x0) * 4
        e = s + span * 4
        if e > len(data):
            break
        row = bytearray(data[s:e])
        row[0::4], row[2::4] = row[2::4], row[0::4]
        top += row
    if not top:
        return None

    return dominant_color(bytes(top), span, len(top) // (span * 4))


# ---------------------------------------------------------------- 取色

def luminance(rgb: tuple[int, int, int]) -> float:
    """感知亮度（ITU-R BT.601）。用于"这颜色算深还是算浅"的粗判。

    不能简单取平均值——人眼对绿色最敏感、蓝色最不敏感。
    直接用均值会导致"深蓝"被判成浅色，出现白底白字。

    注意：**这个函数不用于文字色的最终抉择**（那是 contrast_text 的事，
    用 WCAG 对比度）。它服务于"要不要给系统暗色模式"这类开关判断。
    """
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def _linearize(channel: float) -> float:
    """sRGB 分量线性化（WCAG 2.x 定义）。

    对比度必须在**线性光空间**里算。直接用 sRGB 数值比大小是常见错误：
    sRGB 是 gamma 编码过的，同样差 50 个数值，在暗部比在亮部实际差得多。
    """
    c = channel / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 相对亮度，取值 0.0（黑）~ 1.0（白）。

    与 `luminance()` 的区别：这个是 gamma 线性化后的值，专门用来算对比度。
    """
    r, g, b = rgb
    return (0.2126 * _linearize(r) + 0.7152 * _linearize(g)
            + 0.0722 * _linearize(b))


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG 对比度比值，范围 1.0（完全相同）~ 21.0（纯黑白）。

    `(L_bright + 0.05) / (L_dark + 0.05)`，公式里的 0.05 是环境光补偿项。
    WCAG AA 正文要求 >= 4.5，大字号 >= 3.0。
    """
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_dark(rgb: tuple[int, int, int], threshold: float = 128.0) -> bool:
    """亮度低于阈值算深色。

    加 epsilon 是因为三个系数（0.299/0.587/0.114）浮点表示不精确，
    它们的和是 0.9999999999999999 而非 1.0 —— 于是正中间的中灰
    (128,128,128) 算出来是 127.99999999999999，会被误判成深色。
    这个偏差在阈值附近会翻转结论，所以显式补偿。
    """
    return luminance(rgb) < threshold - 1e-6


def contrast_text(bg: tuple[int, int, int],
                  dark: tuple[int, int, int] = (24, 26, 31),
                  light: tuple[int, int, int] = (240, 242, 245)
                  ) -> tuple[int, int, int]:
    """给背景色挑一个可读的标题文字色。

    用 **WCAG 对比度比值择优**，而不是"亮度过阈值就换色"。

    为什么不能用阈值二选一
    ----------------------
    阈值判据有两个毛病（实测）：
      1. **结果不连续**。背景色在阈值附近动一点，文字色就整个翻转，
         视觉上很突兀。
      2. **不保证可读**。它只回答"背景算深还是浅"，不回答"这个字色
         到底够不够看得清"。中间调背景（如 #22c1a3 这种亮青绿，
         BT.601 亮度 142）会配上深字，对比度只有 4 左右，属于勉强及格；
         换个梯度再亮一点的品牌色就会掉到 3 以下。
    直接算两个候选色的对比度取高的那个，没有这些问题：
    结果连续、且总是给出可证明更优的解。

    这只在背景**恰好等于**候选色时才会平手（对比度都趋近 1），
    此时 dark 优先——那种极端情况下换哪个都一样。
    """
    return dark if (contrast_ratio(bg, dark) >= contrast_ratio(bg, light)
                    ) else light


def dominant_color(image_bytes: bytes, width: int, height: int,
                   sample_rows: int = 0, skip_left: int = 0,
                   skip_right: int = 0,
                   require_variety: bool = False
                   ) -> tuple[int, int, int] | None:
    """从 RGBA 像素数据里统计主色调。

    参数
    ----
    sample_rows : 取最上方的多少行参与统计。0 表示全部。
                  只取顶部是因为要匹配的是"界面顶部的观感"。
    skip_left / skip_right : 左右各跳过多少像素。
    require_variety : 要求画面"有变化"。整片纯色时返回 None 而不是
                  返回那个纯色本身——空白截图（未渲染）取出来的
                  "主色调"是空白色，拿它上标题栏比不上色更糟。

    做法是量化到 16 级一档再统计众数，而不是求平均——
    平均会把高饱和的主色和背景色混成一团灰，那正是"突兀"的来源。

    灰像素处理（两轮，这是关键）
    ----------------------------
    界面顶部通常是大面积中性灰底（工具栏、留白）+ 小面积高饱和主题色
    （品牌色按钮、强调条）。若只按像素数投票，灰底必然压倒主题色，
    取到的"主色调"就是灰色 —— 标题栏跟着变灰，等于没做自适应。

    所以分两轮：先在有彩像素里取众数；只有整屏确实无彩（纯灰界面）
    时才退回灰像素定色。

    但"存在即优先"不能是无限的
    --------------------------
    实测踩到：深灰底 + 只有 8 个淡彩像素时，会取到那个淡彩
    （`#28b898`），一个**深色主题的界面**被极少数字节带成亮青标题栏。
    那些像素来自图标边缘的抗锯齿/次像素渲染，不代表主题色。

    所以要求有彩像素**占采样面积的比例**达到 CHROMA_MIN_RATIO。
    低于这个比例就认为界面无彩，退回灰色方案。

    为什么不能用「像素数量」当门槛（踩过两次）
    ----------------------------------------
    数量门槛**不可移植**——同样的 48 个像素，在小窗口里可能是可观的
    一条带，在 1200x800 的窗口里只是 0.09%。而真实调用一次要采样
    4w~12w 像素（`rows=6%高` × `宽-4%`），此时"一列 48 行"的杂色
    也能凑够任何固定的低数量门槛。所以只看**占比**，不看绝对数量。

    这个坑的教训：验证门槛时必须用**真实采样规模**造数据。
    我最初拿 80x8（640 像素）试，看着挺合理；换算到真实规模才
    发现门槛差了两个数量级。

    早期还试过只给灰像素降权 0.5，实测 2:1 的像素数优势仍让灰胜出。

    性能（重要）
    -----------
    这个函数跑在 **GUI 线程**上，卡住它就是卡住界面。所以刻意优化过：

      · 用 `bytes.find()` 在 C 层找透明像素，**快路径完全不进 Python 循环**
      · 量化用 `bytes.translate()`（C 实现的查表），一次处理整块数据
      · 用带步长的切片剔除 A 通道，避免逐元素拷贝
      · 分桶统计前先降采样，把参与投票的像素压到几千个

    实测（1200x800 窗口顶部 6% = 55296 像素）：
      朴素逐像素 Python 循环    ~20 ms
      当前实现                  ~2 ms

    别小看这个差别：取色是在界面刚画出来时做的，20ms 的卡顿肉眼可见。
    """
    if not image_bytes or width <= 0 or height <= 0:
        return None

    buckets = _bucketize(image_bytes, width, height, sample_rows,
                         skip_left, skip_right)
    if buckets is None:
        return None
    chroma, neutral, n_votes = buckets

    # 有彩像素要占到一定比例，才算界面的主题色
    n_chroma = sum(chroma.values())
    strong = bool(n_votes) and n_chroma / n_votes >= _CHROMA_MIN_RATIO

    pool = chroma if (strong and chroma) else neutral
    if not pool:
        return None

    best, top_n = max(pool.items(), key=lambda kv: kv[1])

    # 「一片纯色」检测：空白截图（未渲染）会整片同色，取出来的"主色调"
    # 就是那个空白色 —— 拿它上标题栏比不上色更糟，所以判为失败。
    if require_variety and n_votes and top_n / n_votes >= FLAT_RATIO:
        return None

    # 还原到档位中心，避免取色偏暗
    return (best[0] << 4 | 0x8, best[1] << 4 | 0x8, best[2] << 4 | 0x8)


#: 单个量化档位占比达到此值即视为「一片纯色」（未渲染/空白）
FLAT_RATIO = 0.97

#: 有彩像素占采样面积的比例达到此值，才认为界面确实有主题色。
#  取值依据：真实一次采样 4w~12w 像素，0.5% 相当于 200~600 个像素 ——
#  这个量级才是"界面上真有一块彩色"，低于它多半是图标边缘抗锯齿、
#  滚动条、圆角过渡之类的杂色。
_CHROMA_MIN_RATIO = 0.005


def _bucketize(image_bytes: bytes, width: int, height: int,
               sample_rows: int = 0, skip_left: int = 0,
               skip_right: int = 0):
    """降采样 + 量化 + 分桶，返回 (chroma, neutral, n_votes)；无效入参返回 None。

    键是 3 字节的量化档位三元组，值是计数。chroma = 有彩像素，
    neutral = 近灰像素。

    抽出来是为了让 `dominant_color` 与 `flat_ratio` 共用同一套裁剪／
    量化／降采样逻辑——两处各写一遍必然漂移。
    """
    if not image_bytes or width <= 0 or height <= 0:
        return None

    rows = min(sample_rows, height) if sample_rows else height
    x0 = max(0, skip_left)
    x1 = min(width, width - skip_right)
    if x1 <= x0:
        return None

    stride = width * 4                     # RGBA8888

    # 整块检查有没有透明像素。bytes.find 在 C 层跑，没有就完全不用
    # 走 Python 循环逐像素查 alpha —— 而绝大多数截图都是全不透明的。
    scan_end = min(len(image_bytes), rows * stride)
    no_alpha = image_bytes.find(b"\x00", 3, scan_end) < 0

    # 每行只取关心的横向区间，拼成连续 RGBA 缓冲，方便后续 C 层操作
    buf = b"".join(image_bytes[y * stride + x0 * 4: y * stride + x1 * 4]
                   for y in range(rows))
    span = x1 - x0

    rgb = _strip_alpha(buf) if no_alpha else _strip_alpha_masked(buf)
    n_pix = len(rgb) // 3
    if not n_pix:
        return None

    # 量化到 16 级一档。translate 是 C 实现的查表，一次处理整块数据，
    # 比 Python 里逐字节 >>4 快一到两个数量级。
    q = rgb.translate(_QUANT_TABLE)

    # 降采样：只保留若干行参与投票，把点数压到几千级。
    # 主色调是明显的聚类，几千个点足够稳定，再多只是浪费。
    if n_pix > _MAX_VOTES:
        step = (n_pix + _MAX_VOTES - 1) // _MAX_VOTES
        keep = b"".join(q[y * span * 3: (y + 1) * span * 3]
                        for y in range(0, rows, step))
    else:
        keep = q

    # 逐像素分桶（此时点数已降到几千，代价可忽略）
    chroma: dict[bytes, int] = {}
    neutral: dict[bytes, int] = {}
    n_votes = len(keep) // 3
    for k in range(0, n_votes * 3, 3):
        key = keep[k:k + 3]
        r, g, b = key[0], key[1], key[2]
        if (max(r, g, b) - min(r, g, b)) * 17 >= _SAT_MIN_Q:
            chroma[key] = chroma.get(key, 0) + 1
        else:
            neutral[key] = neutral.get(key, 0) + 1

    return chroma, neutral, n_votes


def flat_ratio(image_bytes: bytes, width: int, height: int,
               sample_rows: int = 0, skip_left: int = 0,
               skip_right: int = 0) -> float:
    """最大颜色档位占采样像素的比例。1.0 = 整片同色。

    用途：判断一次截图是不是**空白**（控件没渲染出来）。空白截图整片
    同色，取出的"主色调"是那个空白色 —— 拿它上标题栏比不上色更糟，
    所以要能识别出来并降级。

    注意：判断"整体是否空白"要**看整块区域**，而不是只看要取色的那条。
    真实界面的顶部 6% 可能本身就是一条纯色工具栏（完全正常），
    若拿它做纯色判断会误杀。
    """
    b = _bucketize(image_bytes, width, height, sample_rows,
                   skip_left, skip_right)
    if b is None:
        return 1.0
    chroma, neutral, n_votes = b
    if not n_votes:
        return 1.0
    top = 0
    for d in (chroma, neutral):
        if d:
            top = max(top, max(d.values()))
    return top / n_votes


#: 量化查表：每字节 >> 4（用 translate 在 C 层完成）
_QUANT_TABLE = bytes(b >> 4 for b in range(256))

#: 分桶统计前最多保留多少个像素参与投票。
_MAX_VOTES = 4096

#: 饱和度阈值换算到"量化档位差"：档位差 * 17 >= SAT_MIN * 255
_SAT_MIN_Q = int(0.15 * 255)


def _strip_alpha(buf: bytes) -> bytes:
    """从 RGBA 连续缓冲里剔掉 A 通道，返回 RGB 连续缓冲。

    全程用**带步长的切片**（`buf[0::4]`）在 C 层取单通道，
    再用 bytearray 的步长赋值交织回 RGB，避免 Python 逐元素循环。
    """
    n = len(buf) // 4
    out = bytearray(n * 3)
    out[0::3] = buf[0::4]
    out[1::3] = buf[1::4]
    out[2::3] = buf[2::4]
    return bytes(out)


def _strip_alpha_masked(buf: bytes) -> bytes:
    """含透明像素时的退回路径：逐像素过滤。

    只在检测到 alpha 有 0 时才走这里。真实截图（窗口不透明）
    几乎不会命中，所以不做进一步优化——保持简单比省这点时间重要。
    """
    out = bytearray()
    for i in range(0, len(buf) - 3, 4):
        if buf[i + 3] >= 8:
            out += buf[i:i + 3]
    return bytes(out)


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


def blend(a: tuple[int, int, int], b: tuple[int, int, int],
          ratio: float) -> tuple[int, int, int]:
    """按比例混合两色。ratio=0 取 a，ratio=1 取 b。

    用途：把取到的主色调往中性方向拉一点再上到标题栏。
    直接用界面原色往往太饱和（尤其 DSH 这种带蓝紫调的界面），
    标题栏会抢视线。混一点深灰让它"退后"一格。
    """
    ratio = max(0.0, min(1.0, ratio))
    return tuple(int(round(a[i] * (1 - ratio) + b[i] * ratio)) for i in range(3))
