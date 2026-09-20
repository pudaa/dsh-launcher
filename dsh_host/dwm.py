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
        ctypes.windll.dwmapi          # noqa: B018
        build = sys.getwindowsversion().build
        return build >= 22000
    except Exception:                                          # noqa: BLE001
        return False


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


# ---------------------------------------------------------------- 取色

def luminance(rgb: tuple[int, int, int]) -> float:
    """感知亮度（ITU-R BT.601）。

    不能简单取平均值——人眼对绿色最敏感、蓝色最不敏感。
    直接用均值会导致"深蓝"被判成浅色，出现白底白字。
    """
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


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
    """给背景色配一个可读的标题文字色。

    这一步不能省。只设底色不设文字色的话，浅色背景上会沿用系统的
    浅色文字（白底白字），深色背景上会沿用深色文字——两者都读不了。
    """
    return light if is_dark(bg) else dark


def dominant_color(image_bytes: bytes, width: int, height: int,
                   sample_rows: int = 0, skip_left: int = 0,
                   skip_right: int = 0) -> tuple[int, int, int] | None:
    """从 RGBA 像素数据里统计主色调。

    参数
    ----
    sample_rows : 取最上方的多少行参与统计。0 表示全部。
                  只取顶部是因为要匹配的是"界面顶部的观感"。
    skip_left / skip_right : 左右各跳过多少像素。

    做法是量化到 16 级一档再统计众数，而不是求平均——
    平均会把高饱和的主色和背景色混成一团灰，那正是"突兀"的来源。

    另外做了一个饱和度加权：完全灰的像素（页面留白/滚动条）权重降一档，
    否则大面积灰底会盖过真正的主题色。
    """
    if not image_bytes or width <= 0 or height <= 0:
        return None

    rows = min(sample_rows, height) if sample_rows else height
    x0 = max(0, skip_left)
    x1 = min(width, width - skip_right)
    if x1 <= x0:
        return None

    stride = width * 4                     # RGBA8888
    buckets: dict[tuple[int, int, int], float] = {}

    for y in range(rows):
        base = y * stride
        for x in range(x0, x1):
            i = base + x * 4
            if i + 3 >= len(image_bytes):
                break
            a = image_bytes[i + 3]
            if a < 8:                      # 全透明像素不参与
                continue
            r, g, b = image_bytes[i], image_bytes[i + 1], image_bytes[i + 2]

            # 量化到 16 级一档，减少噪声导致的"每像素一个颜色"
            qr, qg, qb = r >> 4, g >> 4, b >> 4

            # 饱和度低（接近灰）的权重减半
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / 255.0
            weight = 1.0 if sat > 0.06 else 0.5

            key = (qr, qg, qb)
            buckets[key] = buckets.get(key, 0.0) + weight

    if not buckets:
        return None

    best = max(buckets.items(), key=lambda kv: kv[1])[0]
    # 还原到档位中心，避免取色偏暗
    return (best[0] << 4 | 0x8, best[1] << 4 | 0x8, best[2] << 4 | 0x8)


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
