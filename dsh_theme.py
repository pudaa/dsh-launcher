# -*- coding: utf-8 -*-
"""DSH 官方美术在 Qt 侧的转译层。

为什么需要这一层
----------------
DSH 的美术规范是 **Web 的 CSS 令牌**（`--dsw-alias-*` / `--dsh-font-*` / `--dsw-radius-*`），
而 Qt 只认样式表与像素。直接照抄 CSS 会得到"颜色对了、但气质不对"的界面：

  · Web 靠 `box-shadow` + 多层背景做层次；Qt 里叠加多层要么写 custom paint，
    要么用 1px 描边 + 底色差（官方浅色本来就是描边分层，正好对得上）。
  · Web 的 `:focus-visible` 只在键盘操作时出现；Qt 的 `:focus` 默认画一圈系统虚线，
    在中文界面上非常刺眼 —— 这里一律去掉，按键焦点改用底色差表达。
  · Web 的 `transition: .2s cubic-bezier(.4,0,.2,1)`；Qt 要自己用 QPropertyAnimation，
    时长/缓动在 `MS` / `EASE` 里给出，不要各写各的。
  · 圆角单位是 CSS px，Qt 的样式表也认 px —— **统一用 px，不用 pt**（pt 会随 DPI 漂移）。

颜色不在这里定义：唯一来源是 `dsh_panel.PALETTE_*`（逐条抄自官方令牌）。
本模块只做"令牌 → 控件"的翻译，避免第二份色表。

完整规范与转译差异见 `docs/ui-guidelines.md`。
"""
from __future__ import annotations

import re

from PySide6.QtCore import QEasingCurve, Qt
from PySide6.QtGui import QColor, QFont, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLabel, QMenu, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

import dsh_icons as icons
from dsh_host import config
from dsh_panel import FONT, palette

#: 字号阶梯（px）。取自官方组件里出现频率最高的几档：
#: 14/22 用于设置项与正文标题，13 用于常规正文，12/18 用于说明文字，11 用于角标。
TYPE = {
    "display": (20, 28, 600),
    "h1": (17, 24, 600),
    "h2": (14, 22, 600),
    "body": (13, 20, 400),
    "small": (12, 18, 400),
    "caption": (11, 16, 400),
}

#: 官方圆角（--dsw-radius-*）
RADIUS = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 20, "panel": 28}

#: 官方间距节奏：4 的倍数
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}

#: 官方动效（--ds-transition-duration*）
MS = {"fast": 100, "base": 200, "slow": 300}

#: 与官方 `cubic-bezier(.4, 0, .2, 1)` 最接近的 Qt 缓动
EASE = QEasingCurve.Type.OutCubic


def palette_now() -> dict:
    """当前主题的调色板。

    主题判定的唯一权威是主窗口标题栏取色（写入 `config.last_theme_dark`），
    这里只读，不自己判断深浅。
    """
    return palette("dark" if bool(config.get("last_theme_dark", True)) else "light")


# ------------------------------------------------------------------ 样式表

def dialog_qss(pal: dict) -> str:
    """对话框 / 提示窗的样式。

    字号一律 px；`QPushButton` 的默认形态是"次要按钮"，主操作由 `#primary` 指定，
    与官方「一个主按钮 + 若干幽灵按钮」的排布一致。
    """
    return """
QDialog { background: %(bg)s; }
QLabel { color: %(text)s; font-family: %(font)s; font-size: 13px; }
QLabel#dlgTitle { color: %(text)s; font-family: %(font)s;
                  font-size: 15px; font-weight: 600; }
QLabel#dlgBody { color: %(dim)s; font-family: %(font)s; font-size: 13px; }
QLabel#dlgHint { color: %(muted)s; font-family: %(font)s; font-size: 12px; }

QPushButton {
    color: %(text)s; background: %(btn_fill)s; border: 1px solid %(line)s;
    border-radius: %(r_sm)spx; padding: 6px 14px;
    font-family: %(font)s; font-size: 13px;
}
QPushButton:hover { background: %(btn_hover)s; }
QPushButton:disabled { color: %(muted)s; }
/* 官方在指针操作下不画焦点环（input-modality=pointer 时 outline 透明） */
QPushButton:focus { outline: none; }

QPushButton#primary {
    color: %(primary_text)s; background: %(primary_fill)s; border: none;
    border-radius: %(r_sm)spx; padding: 7px 16px;
    font-family: %(font)s; font-size: 13px; font-weight: 600;
}
QPushButton#primary:hover { background: %(primary_hover)s; }
QPushButton#primary:disabled { color: %(muted)s; background: %(hover)s; }

QPlainTextEdit, QLineEdit {
    background: %(card)s; color: %(text)s; border: 1px solid %(line)s;
    border-radius: %(r_sm)spx; padding: 8px;
    font-family: %(mono)s; font-size: 12px;
    selection-background-color: %(nav_active)s; selection-color: %(text)s;
}
QTextBrowser {
    background: %(card)s; color: %(text)s; border: 1px solid %(line)s;
    border-radius: %(r_sm)spx; padding: 8px;
    font-family: %(font)s; font-size: 13px;
    selection-background-color: %(nav_active)s; selection-color: %(text)s;
}
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: %(scroll)s; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
""" % {
        "bg": pal["bg"], "text": pal["text"], "dim": pal["dim"],
        "muted": pal["muted"], "line": pal["line"], "card": pal["card"],
        "hover": pal["hover"], "btn_fill": pal["btn_fill"],
        "btn_hover": pal["btn_hover"], "primary_fill": pal["primary_fill"],
        "primary_hover": pal["primary_hover"], "primary_text": pal["primary_text"],
        "nav_active": pal["nav_active"], "scroll": pal["scroll"],
        "font": FONT, "mono": '"Cascadia Mono", "Consolas", monospace',
        "r_sm": RADIUS["sm"],
    }


# ------------------------------------------------------------------ 更新说明

def _solid(argb: str, bg: str) -> str:
    """把带 alpha 的令牌合成到不透明底色上，返回 #RRGGBB。

    QTextDocument 的 HTML CSS 不认 Qt 的 #AARRGGBB：`#1a000000` 会被当成
    RGB 解析成近黑色，表格/描边因此变粗。富文本里统一用预混合的实色，
    与面板 QSS 的观感保持一致。
    """
    c, b = QColor(argb), QColor(bg)
    a = c.alphaF()
    return "#%02x%02x%02x" % (
        round(c.red() * a + b.red() * (1 - a)),
        round(c.green() * a + b.green() * (1 - a)),
        round(c.blue() * a + b.blue() * (1 - a)),
    )


def notes_css(pal: dict) -> str:
    """QTextDocument 的 HTML 样式（更新说明用）。

    QSS 作用在 QTextBrowser 的**控件**上，管不到文档里的 h1/p/li；这里用
    QTextDocument 的 defaultStyleSheet 补上元素样式。颜色全部取自同一套令牌，
    不另起色表。
    """
    return """
body { color: %(text)s; font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
       font-size: 13px; }
h1 { font-size: 17px; font-weight: 600; margin: 10px 0 6px 0; }
h2 { font-size: 15px; font-weight: 600; margin: 10px 0 6px 0; }
h3 { font-size: 14px; font-weight: 600; margin: 8px 0 4px 0; }
p { margin: 4px 0; }
ul, ol { margin: 4px 0 4px 18px; }
li { margin: 2px 0; }
a { color: %(accent)s; text-decoration: none; }
code { font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px;
       background-color: %(hover)s; }
pre { font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px;
      background-color: %(hover)s; border: 1px solid %(line)s; padding: 6px;
      white-space: pre-wrap; }
blockquote { color: %(dim)s; border-left: 3px solid %(line)s;
             margin: 6px 0 6px 0; padding-left: 10px; }
table { border-collapse: collapse; }
th, td { border: 1px solid %(line)s; padding: 4px 8px; }
th { background-color: %(hover)s; font-weight: 600; }
hr { border: none; border-top: 1px solid %(line)s; }
""" % {
        "text": pal["text"], "dim": pal["dim"],
        "line": _solid(pal["line"], pal["card"]),
        "hover": _solid(pal["hover"], pal["card"]),
        "accent": pal["accent"],
    }


def _clean_html(text: str) -> str:
    """去掉脚本 / 内嵌框架 / 远程图片。

    Atom feed 的 content 是 HTML，Qt 只认子集；script/style/iframe 不会执行，
    但 img 可能触发远端请求，这里直接剔除，避免打开说明窗口时联网。
    """
    text = re.sub(r"<\s*(script|style|iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>",
                  "", text or "", flags=re.I | re.S)
    text = re.sub(r"<\s*(script|style|iframe|object|embed|img)\b[^>]*/?>",
                  "", text, flags=re.I)
    return text


def _style_markdown_html(html: str, pal: dict) -> str:
    """把 Qt 的 markdown 输出改成可被 notes_css 接管的 HTML。

    Qt 会在生成的标签里塞内联 style（行距、缩进、Courier New、链接色），
    内联样式优先级高于 defaultStyleSheet，所以块级 style 要剥掉；
    span 上的 font-weight（粗体/标题）保留，只去掉标题的 font-size，
    让 h1/h2/h3 回到我们自己的字号阶梯。
    """
    html = re.sub(r'(<(?:p|h[1-6]|pre|blockquote|ul|ol|li|table|tr|td|th)\b[^>]*?)\s+style="[^"]*"',
                  r"\1", html, flags=re.I)
    # 标题 span 带 x-large/large；去掉字号，保留 font-weight。
    html = re.sub(r'(<h[1-6]\b[^>]*>\s*<span\b[^>]*?)\s*font-size:[^;"]+;?',
                  r"\1", html, flags=re.I)
    html = html.replace("Courier New", "Cascadia Mono")
    html = re.sub(r"<img\b[^>]*>", "", html, flags=re.I)
    # Qt 把代码块每一行都写成独立的 <pre>；合并成一个，底色才不会有横条缝隙。
    html = re.sub(r"</pre>\s*<pre\b[^>]*>", "\n", html, flags=re.I)
    html = re.sub(r"<table\b[^>]*>", "<table>", html, flags=re.I)

    def fix_link(match):
        return re.sub(r"color:\s*#[0-9a-fA-F]{3,8};?",
                      "color:%s;" % pal["accent"], match.group(0), flags=re.I)

    return re.sub(r"<a\b[^>]*>.*?</a>", fix_link, html, flags=re.I | re.S)


def _is_mono(font: QFont) -> bool:
    family = (font.family() or "").lower()
    return any(k in family for k in ("cascadia", "consolas", "courier", "mono"))


def _block_is_code(block) -> bool:
    """块内所有 fragment 都是等宽字体 → 这是一个代码块（不是行内代码）。"""
    it = block.begin()
    seen = False
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid() and frag.length():
            seen = True
            if not _is_mono(frag.charFormat().font()):
                return False
        it += 1
    return seen


def _polish_notes_document(doc, pal: dict) -> None:
    """渲染后的收尾：标题字号、代码块底色、行内代码底色。

    Qt 的 HTML importer 不认 defaultStyleSheet 里的 h* font-size，
    但会保留 blockFormat().headingLevel()；这里按层级合并字号。
    `pre` 的 CSS background 同样不画，改用 QTextBlockFormat 铺底色。
    """
    code_bg = QColor(_solid(pal["hover"], pal["card"]))
    heading_px = {1: 17, 2: 15, 3: 14}
    block = doc.begin()
    while block.isValid():
        level = block.blockFormat().headingLevel()
        if level:
            it = block.begin()
            if not it.atEnd():
                base = it.fragment().charFormat()
                font = QFont(base.font())
                font.setPixelSize(heading_px.get(level, 14))
                # headingLevel 会让 QTextDocument 再按 1.5x/1.17x 放大一次；
                # 清掉它，字号才完全由上面这张表决定。
                bf = block.blockFormat()
                bf.setHeadingLevel(0)
                bf.setTopMargin(8 if level <= 2 else 6)
                bf.setBottomMargin(3)
                cursor = QTextCursor(block)
                cursor.setBlockFormat(bf)
                # 不能从 base 复制：它带着 FontSize/pointSize 的 resolve
                # 标记，setFont() 里的 pixelSize 会被旧字号盖掉。这里只
                # 复制颜色与字重，字号用全新设置。
                fmt = QTextCharFormat()
                fmt.setForeground(base.foreground())
                fmt.setFontWeight(base.fontWeight())
                fmt.setFontItalic(base.fontItalic())
                fmt.setFont(font)
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                cursor.setCharFormat(fmt)
        else:
            if _block_is_code(block):
                bf = block.blockFormat()
                bf.setBackground(code_bg)
                # 一个 <pre> 里的多行会被 Qt 拆成多个 block；上下 margin
                # 必须为 0，否则每行之间会露出白缝，看起来像一条条横条。
                bf.setTopMargin(0)
                bf.setBottomMargin(0)
                bf.setLeftMargin(6)
                bf.setRightMargin(6)
                cursor = QTextCursor(block)
                cursor.setBlockFormat(bf)
            else:
                # 行内代码：只给等宽 fragment 铺底色，不整行铺
                it = block.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    if frag.isValid() and frag.length() and _is_mono(
                            frag.charFormat().font()):
                        cursor = QTextCursor(doc)
                        cursor.setPosition(frag.position())
                        cursor.setPosition(frag.position() + frag.length(),
                                           QTextCursor.MoveMode.KeepAnchor)
                        fmt = QTextCharFormat()
                        fmt.setBackground(code_bg)
                        cursor.mergeCharFormat(fmt)
                    it += 1
        block = block.next()


class NotesView(QTextBrowser):
    """更新说明 / 进度的富文本视图。

    说明可能是 GitHub 的 markdown（API）或 HTML（Atom 兜底），
    QPlainTextEdit 会把 `##` / `<p>` 原样打出来；这里统一渲染成
    DSH 的排版，再把进度行以纯文本追加进来。

    为什么用 QTextBrowser 而不是 QTextEdit：前者默认只读、支持链接点击。
    """

    def __init__(self, parent=None, pal: dict | None = None):
        super().__init__(parent)
        p = pal or palette_now()
        self._pal = p
        self._css = notes_css(p)
        self._font = QFont()
        self._font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"])
        self._font.setPixelSize(13)
        # QTextBrowser 的链接色取 palette，不取 HTML 的 a { color }
        link_palette = self.palette()
        link_palette.setColor(QPalette.ColorRole.Link, QColor(p["accent"]))
        self.setPalette(link_palette)
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDefaultStyleSheet(self._css)
        self.document().setDefaultFont(self._font)
        self.document().setDocumentMargin(6)
        self.setPlaceholderText("更新说明会显示在这里")

    def show_notes(self, text: str, fmt: str = "markdown") -> None:
        """渲染说明。fmt: markdown / html / text；空内容显示明确提示。"""
        text = (text or "").strip()
        doc = self.document()
        # setHtml / setPlainText 会重建文档内容，但保留 defaultStyleSheet/Font；
        # 这里每次再写一遍，避免调用方换过 document。
        doc.setDefaultStyleSheet(self._css)
        doc.setDefaultFont(self._font)
        if not text:
            self.setPlainText("该版本没有附带更新说明。")
        elif fmt == "html":
            self.setHtml(_clean_html(text))
            _polish_notes_document(self.document(), self._pal)
        elif fmt == "markdown":
            # setMarkdown 生成的是带内联样式的 HTML；转一道手，剥掉块级 style，
            # 让 notes_css 的字号/间距/代码底色真正生效。
            doc.setMarkdown(text)
            self.setHtml(_style_markdown_html(doc.toHtml(), self._pal))
            _polish_notes_document(self.document(), self._pal)
        else:
            self.setPlainText(text)
        self.verticalScrollBar().setValue(0)


def menu_qss(pal: dict) -> str:
    """托盘菜单：官方下拉菜单的形态（卡片底 + 1px 描边 + 6px 内边距 + 圆角）。"""
    return """
QMenu {
    background: %(card)s; border: 1px solid %(line)s;
    border-radius: %(r_md)spx; padding: 6px;
    color: %(text)s; font-family: %(font)s; font-size: 13px;
}
QMenu::item {
    padding: 7px 22px 7px 12px; border-radius: %(r_sm)spx;
    color: %(text)s;
}
QMenu::item:selected { background: %(nav_hover)s; }
QMenu::item:disabled { color: %(muted)s; }
QMenu::separator { height: 1px; background: %(line)s; margin: 6px 10px; }
QMenu::indicator { width: 14px; height: 14px; }
""" % {
        "card": pal["card"], "line": pal["line"], "text": pal["text"],
        "muted": pal["muted"], "nav_hover": pal["nav_hover"],
        "font": FONT, "r_md": RADIUS["md"], "r_sm": RADIUS["sm"],
    }


def strip_shadow(widget) -> bool:
    """移除 QWindows11Style 给菜单装的、会被窗口裁切的投影。

    只处理 `QGraphicsDropShadowEffect`：效果是窗口内绘制的，窗口和菜单同大，
    所以 offset(3,3)+blur(3) 的阴影只会在右/下露出 3~6px 的硬边。
    返回是否真的移除了东西；没有效果时返回 False（不是错误）。
    """
    effect = widget.graphicsEffect()
    if isinstance(effect, QGraphicsDropShadowEffect):
        widget.setGraphicsEffect(None)
        return True
    return False


def apply_menu(menu, pal: dict | None = None) -> None:
    """给 QMenu 套上官方形态，并去掉 Windows 11 自带的裁切阴影。

    真正的阴影来源（2026-10-19 实测）
    --------------------------------
    不是 DWM，也不受 `NoDropShadowWindowHint` 控制，而是 **QWindows11Style
    在 `polish()` 里给每个 QMenu 装了一个 `QGraphicsDropShadowEffect`**
    （blur 3、offset 3,3、#3f3f3f/180）。效果画在窗口**内部**，而 QMenu 的
    窗口只和菜单一样大 —— 于是阴影在右侧/下侧被窗口边缘硬生生裁断，
    正是托盘菜单右下那条"被容器切割的阴影"。

    Qt 的 `polish()` 在 show 时才跑，并且 `setStyleSheet()` 也会触发
    repolish（等于每次套主题都会重新装上）。所以这里做三步：
      1. 标志位 + 半透明底（圆角前提，跨非 Win11 风格也成立）；
      2. 套完样式马上清一次（覆盖 repolish）；
      3. `aboutToShow` 再清一次（覆盖第一次 show 的 polish）。
    `dsh_theme.Menu` 还在 `showEvent` 里兜底，见该类说明。
    """
    pal = pal or palette_now()
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    # 注意：`setWindowFlag(NoDropShadowWindowHint, True)` 对 QMenu **无效**
    # （实测标志位没变），必须整组 OR 后用 setWindowFlags 写入。
    # 只在缺标志时写：重复 setWindowFlags 会重建窗口——菜单正开着的时候
    # 切主题会把它关掉。
    want = Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
    flags = menu.windowFlags()
    if (flags & want) != want:
        menu.setWindowFlags(flags | want)
    menu.setStyleSheet(menu_qss(pal))
    # setStyleSheet() 会触发 QWindows11Style::polish()，阴影当场被重新装上。
    strip_shadow(menu)

    if not getattr(menu, "_dsh_shadow_hooked", False):
        menu._dsh_shadow_hooked = True
        # aboutToShow 早于 show/paint，而 polish 已经在此前跑完 —— 直接清即可，
        # 不能再用 singleShot(0)：那一拍可能已经画完第一帧（闪一下）。
        menu.aboutToShow.connect(lambda m=menu: strip_shadow(m))


class Menu(QMenu):
    """托盘 / 右键菜单：创建即套主题，并保证每次显示都没有 Qt 的裁切阴影。

    为什么需要子类：`QWindows11Style::polish()` 在 **show 时**才装阴影，
    而 `apply_menu` 通常在 `__init__` 里就调完了。`showEvent` 在窗口已创建、
    第一次 paint 之前执行，是清掉它的确定时机；这样即便将来有人用
    `menu.show()`（不经过 `aboutToShow`）也不会把阴影带出来。
    """

    def __init__(self, parent=None, pal: dict | None = None):
        super().__init__(parent)
        apply_menu(self, pal)

    def showEvent(self, event) -> None:                     # noqa: N802 (Qt 命名)
        super().showEvent(event)
        strip_shadow(self)


def style_dialog(widget: QWidget, pal: dict | None = None) -> None:
    """给对话框套上统一样式。子控件靠 objectName 取到标题/正文/提示三种字级。"""
    widget.setStyleSheet(dialog_qss(pal or palette_now()))


def style_loading_page(page: QWidget, pal: dict | None = None) -> None:
    """启动加载页：底色与提示文字跟随主题，避免深色界面先闪一片白。"""
    p = pal or palette_now()
    page.setStyleSheet("""
QWidget#loadingPage { background: %(bg)s; }
QLabel#loadingTip { color: %(dim)s; font-family: %(font)s; font-size: 13px; }
QProgressBar { background: %(hover)s; border: none; }
QProgressBar::chunk { background: %(accent)s; }
""" % {"bg": p["bg"], "dim": p["dim"], "hover": p["hover"],
       "accent": p["accent"], "font": FONT})


# ------------------------------------------------------------------ 提示窗口

class MessageDialog(QDialog):
    """替代 QMessageBox 的应用内提示窗。

    为什么不用 QMessageBox：它的图标是系统位图、按钮是系统样式，配色与 DSH
    差得很远；而且 `QMessageBox` 的正文样式几乎无法通过 QSS 控制。这里用
    同样的模态语义重画一个，图标取自 Lucide（与面板同源）。
    """

    KINDS = {
        "info": ("circle-check", "ok"),
        "warn": ("triangle-alert", "warn"),
        "error": ("circle-x", "bad"),
        "about": ("circle-dashed", "accent"),
    }

    def __init__(self, parent, title: str, text: str, kind: str = "info",
                 ok_text: str = "知道了", cancel_text: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        pal = palette_now()
        self.pal = pal
        self.setMinimumWidth(420)
        self.setMaximumWidth(620)
        self.setStyleSheet(dialog_qss(pal))

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        icon_name, state_key = self.KINDS.get(kind, self.KINDS["info"])
        icon = QLabel()
        icon.setPixmap(icons.qicon(icon_name, pal[state_key], 18).pixmap(18, 18))
        icon.setFixedSize(18, 18)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(icon)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("dlgTitle")
        title_lbl.setWordWrap(True)
        head.addWidget(title_lbl, 1)
        root.addLayout(head)

        body = QLabel(text)
        body.setObjectName("dlgBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setMinimumHeight(20)
        root.addWidget(body)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.btn_cancel = None
        if cancel_text:
            self.btn_cancel = QPushButton(cancel_text)
            self.btn_cancel.clicked.connect(self.reject)
            row.addWidget(self.btn_cancel)
        self.btn_ok = QPushButton(ok_text)
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(self.btn_ok)
        root.addLayout(row)

        self._fit()

    def _fit(self) -> None:
        """宽度自适应正文：短提示不要撑成一条大横条。"""
        hint = self.findChild(QLabel, "dlgBody")
        if hint is None:
            return
        width = hint.fontMetrics().horizontalAdvance(
            max(hint.text().splitlines() or [""], key=len)) + 90
        self.setMinimumWidth(max(380, min(560, width)))

    # ---- 便捷入口（与 QMessageBox 的用法一一对应） ----

    @classmethod
    def info(cls, parent, title: str, text: str, ok_text: str = "知道了") -> int:
        return cls(parent, title, text, "info", ok_text).exec()

    @classmethod
    def warn(cls, parent, title: str, text: str, ok_text: str = "知道了") -> int:
        return cls(parent, title, text, "warn", ok_text).exec()

    @classmethod
    def error(cls, parent, title: str, text: str, ok_text: str = "知道了") -> int:
        return cls(parent, title, text, "error", ok_text).exec()

    @classmethod
    def about(cls, parent, title: str, text: str, ok_text: str = "关闭") -> int:
        return cls(parent, title, text, "about", ok_text).exec()
