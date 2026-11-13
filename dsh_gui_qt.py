# -*- coding: utf-8 -*-
"""DSH Web 桌面启动器 —— PySide6 / QWebEngineView 版

职责边界
--------
本文件只做两件事：**界面** 与 **流程编排**。

一切与 DSH 的接触（装在哪、什么版本、怎么启动、URL 从哪来、怎么停）
全部通过 dsh_host.contract —— 那里是唯一允许知道 DSH 内部结构的地方。

设计要点
--------
- 不硬编码 DSH 路径 / 端口 / 令牌格式：端口优先 3080，被别家占用时降级为系统分配
- URL 与令牌来自官方 stdout 输出行 `dsh web: <url>`，不再解析 DSH 私有日志
- 停止服务以自己记录的 PID 为准，端口反查仅作兜底
- 更新只走官方 npm 通道；「加入预览计划」为显式 opt-in，且可一键回归稳定版
"""
import json
import os
import sys
import time

# 输出强制 UTF-8。默认 stdout 编码跟随系统区域设置——在 cp1252 的机器上
# （GitHub Actions 的 windows runner 就是这样）打印中文会直接
# UnicodeEncodeError 崩掉，而且异常会把调用处的返回码一起带走。
# tools/ 下的自测脚本都做了这件事，主程序同样需要。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass


def _system_ui_language() -> str:
    """Windows 的界面语言（BCP-47，如 `zh-CN`）。取不到返回空串。

    为什么需要它：QtWebEngine **不会**自动把系统 UI 语言传给 Chromium，
    没设 `--lang` 时内核一律按 en-US 起，DevTools 也就永远是英文
    （截图里那条 "DevTools is now available in Chinese!" 正是这么来的）。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(85)
        n = ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, len(buf))
        return buf.value if n else ""
    except Exception:                                          # noqa: BLE001
        return ""


# 必须在**任何 PySide6 导入之前**设置：Chromium 在 QtWebEngine 初始化时
# 一次性读取这组 flags，之后再改就来不及了。
_UI_LANG = _system_ui_language()

def _hw_accel_enabled() -> bool:
    """本次是否走 GPU 渲染。环境变量优先（排障用），否则用导入时的结论。"""
    env = os.environ.get("DSH_WEBENGINE_HW_ACCEL")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(_HW_ACCEL)


#: 本次启动是否走硬件加速。**唯一来源是用户设置**，没有任何自动干预。
#:
#: 2026-10-24 曾做过"崩溃哨兵"：上次非正常退出 → 本次自动降级为软件渲染一次。
#: 2026-10-25 撤掉，不是因为它有 bug，而是因为它错在方向：
#:   1) 降级是概率缓解，不是修复。QTBUG-142165 已定位根因（ANGLE D3D11 的
#:      PackedAttributeLayout 填充字节未初始化，污染 InputLayoutCache），
#:      但仍要等含修复的 WebEngine 构建。降级期间照样崩过（10-24 14:58）。
#:   2) 一致性：用户选了硬件加速，就不该背着他改配置。自动降级和用户手动关
#:      面对的是同一个"降级后仍可能崩"的事实，区别只在于把落差藏起来。
#:      所以这里只读一个布尔：用户开就是开，用户关就是关，面板一个开关说完。
_HW_ACCEL = True
# 模块导入时就要定 flags（Chromium 只读一次），所以这里先读用户设置。
try:
    from dsh_host import config as _early_cfg
    _HW_ACCEL = bool(_early_cfg.get("webengine_hw_accel", True))
except Exception:                                          # noqa: BLE001
    _HW_ACCEL = True

_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
if _UI_LANG and "--lang=" not in _flags:
    _flags = (_flags + " --lang=" + _UI_LANG).strip()
if not _hw_accel_enabled() and "--disable-gpu" not in _flags:
    _flags = (_flags + " --disable-gpu").strip()
if _flags:
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _flags
    try:
        from dsh_host import config as _flag_log_config
        with open(_flag_log_config.host_log(), "a", encoding="utf-8") as _f:
            _f.write("%s  WebEngine flags: %s（硬件加速=%s）\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), _flags,
                        "开" if _hw_accel_enabled() else "关"))
    except Exception:                                      # noqa: BLE001
        pass

from PySide6.QtCore import (QCoreApplication, QEvent, QStandardPaths, Qt,
                            QThread, Signal, QTimer, QUrl)
from PySide6.QtGui import (QIcon, QAction, QDesktopServices, QImage,
                           QKeySequence, QShortcut)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QSystemTrayIcon, QDialog,
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QStackedLayout,
    QStackedWidget, QPlainTextEdit, QPushButton, QFileDialog,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile

from dsh_ui import console as dsh_console
from dsh_ui import devtools as dsh_devtools
# 工具与常量（ASSET_DIR / icon_for / resource_report 等）
from dsh_ui.prelude import *   # noqa: F401,F403
# 下划线开头的工具 star-import 不带，但 tools/ 里一直在直接取用，
# 继续从入口导出（保持这些脚本不用改）。
from dsh_ui.prelude import _asset_dir, _parse_probe_color  # noqa: F401
from dsh_ui.shell.frame import WindowFrameMixin
from dsh_ui.shell.panel import PanelMixin
from dsh_ui.shell.service import ServiceMixin
from dsh_ui.shell.titlebar import TitlebarMixin
from dsh_ui.shell.tray import TrayMixin
from dsh_ui.shell.updates import UpdatesMixin
from dsh_ui.shell.view import ViewMixin
from dsh_ui import panel as dsh_panel
from dsh_ui import theme as dsh_theme
import dsh_ui.dialogs
import dsh_ui.workers
from dsh_host import (config, contract, dwm, lifecycle, procmem, provision,
                      remote, selfupdate, storage, updater)
# 拆分后这些名字住在 dsh_ui，但自测脚本一直用 `dsh_gui_qt.MainWindow` 这类
# 写法取它们，所以从这里继续导出（保持向后兼容，别让 tools/ 跟着改一遍）。
from dsh_ui.dialogs import (DataTaskDialog, HostUpdateDialog, OnboardingDialog,
                            ReinstallDialog, UpdateDialog)
from dsh_ui.workers import (CheckWorker, DataTaskWorker, DiagnoseWorker,
                            HostCheckWorker, HostUpdateWorker, NotesWorker,
                            ProvisionWorker, RemoteConnectWorker,
                            ServiceSwapWorker, StartupWorker, UpdateWorker,
                            VersionReverifyWorker)
from dsh_host.contract import DshError, ServiceHandle
from dsh_host.logutil import host_log
from dsh_host.version import HOST_NAME, HOST_TAGLINE, HOST_VERSION







#: 早期内置的单色图标，仅作兜底

# 随标题栏深浅切换的两套图标。
#
# 为什么必须两套：DSH 的暗色主题下标题栏是深色，**黑色图标等于看不见**
# （老大实机发现）。所以深色标题栏配白色图标、浅色标题栏配深色图标。
#
# 两份 ico 由 tools/make_icons.py 从 assets/deepseek.svg 生成，
# 每个文件内含 16~256 共 9 个尺寸——单尺寸 ICO 在小图标下会糊。




#: 环境变量：设成文件路径时，应用只做资源自检、写报告、退出。
#
# 为什么需要它
# ------------
# "图标在源码里能用、打包后静默失效"这类问题，**只能在打包产物上验**。
# 源码树里 assets/ 永远存在，测试全绿也说明不了包里带没带。
# 所以给打包产物一个可自动化的自检入口，CI 构建后立刻跑它。
#: 产物自检入口的环境变量名。名字定义在 selfupdate 里 —— 那是 updater 与 exe
#: 之间的契约（updater 要靠它预检候选版本），两边必须一致，不能各写一份。
RESOURCE_CHECK_ENV = selfupdate.SELFTEST_ENV




#: 主题跟踪的"变色"容差。小于这个差值不动手——避免因渐变/抗锯齿噪声
#  反复重设 DWM 属性（既无意义，也会让标题栏偶尔闪）。

#: 主题跟踪每秒允许的最大检查次数上限的间隔（毫秒）。配置值低于它会被抬到它，
#  防止有人把 theme_watch_ms 设成 50 之类把渲染进程变成忙轮询。




# ---------------------------------------------------------------- 主题色探针
#
# L1 取色：在**界面顶部**取几个采样点，读它们"实际生效"的背景色。
#
# 设计要点
# --------
# · **只用标准 DOM API**：elementFromPoint 取该点最上层元素，
#   getComputedStyle 读它的背景色；背景透明就沿父链上溯，
#   直到找到一个不透明的。
#   → 不依赖 DSH 的类名、id 或任何层级结构，所以不违反"界面层不得
#     知道 DSH 内部结构"的分层铁律。DSH 怎么改 DOM 都不影响。
# · 返回最多的那个颜色（众数），避免被个别异色元素带偏。
# · 取不到就返回 null，让 Python 侧降级到像素截图。
#
# 为什么不用像素截图作为唯一来源：见 _sample_titlebar_color 的 docstring。


# ------------------------------------------------------------------ 服务规划







# ------------------------------------------------------------------ 后台工作





















# ------------------------------------------------------------------ 首次运行引导





# ------------------------------------------------------------------ 更新对话框













# ------------------------------------------------------------------ 主窗口

LOADING_HTML = """
<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  body { margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
         background:#16181d; font-family:'Segoe UI','Microsoft YaHei',sans-serif; color:#dfe3e8; }
  .box { text-align:center; }
  .ring { width:52px; height:52px; margin:0 auto 22px; border-radius:50%;
          border:3px solid #2d333c; border-top-color:#4a9eff;
          animation:spin 0.9s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  h2 { font-size:20px; font-weight:600; margin:0 0 8px; }
  p  { font-size:13px; color:#8b949e; margin:0; }
</style></head><body>
  <div class="box"><div class="ring"></div>
  <h2>正在启动 DSH</h2><p>DeepSeek Harness · 首次启动需要 10-30 秒</p></div>
</body></html>
"""


#: 内存采样间隔（毫秒）。60 秒一次够画出"一天里怎么涨"的曲线，
#: 而写入量只有 1440 行/天。
MEM_SAMPLE_MS = 60_000
#: mem.log 的单文件上限；超了就只保留最后一段重写（不滚动归档，
#: 因为这份日志的价值全在"最近几小时"）。
#: 网页进程 PrivateBytes 超过它就在日志里写一条"可以重新加载页面"的提示。
#: **只提示，不自动重载**——自动重载会打断正在跑的对话，代价比省内存大。

#: 手动「重新加载页面」连按判定窗口（秒）。窗口内第二次按 = 重建视图：
#: 软重载治不了"合成表面丢失"（2026-10-24 实测），重建一定治得了。

#: 页面重建（换掉整个 QWebEngineView）的防循环窗口与次数。
#: 2026-10-24 现场：页面 DOM 活着但合成表面丢失，reload 无效，只有换视图
#: 才能恢复。但"重建→还是坏→再重建"会变成每 30 秒重载一次页面，所以给
#: 上限：窗口内超过次数就停止，明确告知用户重启应用。


class MainWindow(WindowFrameMixin, ViewMixin, TitlebarMixin, TrayMixin,
                 ServiceMixin, UpdatesMixin, PanelMixin, QMainWindow):
    """主窗口。

    按职责拆成七个 mixin（`dsh_ui/shell/`）：每个分支只放方法，状态全部在
    下面的 `__init__` 里初始化。改"更新"不必先滚过"标题栏取色"。
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        # 图标在下方 _set_theme_icon(dwm.system_uses_dark()) 里设置——
        # 单色图标 ICON_PATH 已不够用：黑色图标在深色标题栏上看不见。
        self.resize(1360, 860)
        self.setMinimumSize(960, 600)

        self._tray_hint_shown = False
        self.handle: ServiceHandle | None = None
        self.update_info: updater.UpdateInfo | None = None
        #: 当前界面主题（供控制面板跟随）。初值取上次记住的，这样面板在标题栏
        #: 取色探针跑出结果之前就能用对配色，不会先亮一下再翻黑。
        self._theme_dark = bool(config.get("last_theme_dark", True))
        self.host_release = None          # 有值表示桌面壳有新版本可用
        self.view = None
        self.profile = None
        self.downloads = None             # 下载处理器（父对象是 profile，随它回收）
        self.loading_page = None
        # 页面观测记录在窗口创建之前就存在：开发者模式可能在运行中被打开，
        # 那时要能回看"打开之前"发生的报错。
        self.recorder = dsh_devtools.Recorder()
        self.interceptor = None
        self._devwin = None               # 轻量日志窗口（按需创建，关闭只隐藏）
        self._inspector = None            # Chromium DevTools 窗口（关闭即销毁）
        self._theme_timer = None          # 主题跟踪定时器（低频）
        self._last_titlebar_rgb = None    # 上次上色用的界面色，用于变更比对
        self._mem_timer = None            # 内存采样定时器（每分钟一行日志）
        self._data_worker = None          # 数据目录重活的当前 worker
        self._surface_resets: list[float] = []   # 视图重建时间戳（防循环）
        self._reset_pending = False       # 已排队的重建请求（去重）
        self._manual_reloads: list[float] = []   # 手动重载时间戳（连按判定）
        # 远程会话（2026-11-12）：SSH 隧道 + 远端 dsh web。
        # 同一时刻只允许一条远程会话，与本地服务互斥——两套 handle 同时活着，
        # 状态栏、菜单、退出的收尾逻辑就得分叉判断，复杂度不值当。
        self._remote = None               # remote.RemoteSession
        self._remote_host = None          # remote.SshHost
        self._remote_worker = None        # RemoteConnectWorker
        self._remote_state: dict = {}     # 推给面板显示的那一份

        self.stack = QStackedWidget()
        # 关键：**StackAll** —— 所有页都参与排版、且都保持 show 状态，
        # 切页只是把目标页 raise 到最上层，不会 hide 另一页。
        #
        # 为什么必须这样（2026-10-20 实测，tools/webview_flicker_diag.py）
        # -----------------------------------------------------------------
        # 默认的 StackOne 只给**当前页**排版。于是隐藏的 Web 视图一直停在
        # Qt 的默认几何 100x30 上，而 Chromium 一开加载就按当时的视口排版。
        # 实测（真窗口 + 真 GPU，125% 缩放）：
        #   · 切页前：视图 100x30，网页 innerWidth/innerHeight = 0x0；
        #   · 切页后 +48ms：视图已是全尺寸，网页视口**仍是 100x30**；
        #   · 切页后 0~600ms：窗口里**没有任何网页像素**（露的是底色）。
        # 这正是"每次启动 WebEngine 先以很小的状态出现在左上角"的来源。
        # 换成 StackAll 后同样的实测变成：切页前视口就是全尺寸，
        # 切页第一帧（+17ms）即满幅，没有空白期。
        #
        # 附带好处：视图不再经历 hide/show 与"一步跳变"，它的合成器
        # surface 从创建起就跟着容器走，缩放时少一次尺寸突变。
        self.stack.layout().setStackingMode(
            QStackedLayout.StackingMode.StackAll)
        self.setCentralWidget(self.stack)
        # 初始图标跟随**系统**主题——此刻还不知道界面是什么色。
        # 取色成功后会被 _set_theme_icon 换成匹配界面底色的那套。
        self._icon_dark_bg = None
        self._set_theme_icon(dwm.system_uses_dark())
        self._build_loading_page()

        # WebEngine 的创建是一笔固定成本（约 2 秒，含 QtWebEngineProcess 子进程），
        # 原先排在"服务就绪之后"串行执行——正好压在用户最焦虑的那一刻。
        # 挪到这里与服务启动并行：代价是加载页刚出现时顿一下，但那时用户还没
        # 盯住它，比结尾白 2 秒好得多。（2026-10-20，对照官方桌面端后的优化）
        QTimer.singleShot(80, self._ensure_webview)
        self._build_tray()
        # 原生弹层（<select> 等）出现/关闭时给 Web 视图补一帧。
        # 2026-10-24 实测：QtWebEngine 6.11 打开原生弹层后主视图会停帧（透明），
        # 而 view.update() 一次就恢复——比"重建视图"轻得多，放在第一道防线。
        self._popup_guard = dsh_devtools.PopupRepaintGuard(
            lambda: self.view, self)

        self.starter = None
        self._launch_startup()
        # 内存采样（2026-10-21）：先启动、后采样——它只是在旁边记数字，
        # 不参与任何启动决策，绝不能拖慢首屏。
        self._start_mem_watch()


    # ----------------------------------------------------------- 界面





    # ----------------------------------------------------------- 标题栏自适应




    #: 各层取色失败的记录，用一次日志把"为什么没上色"讲清楚
    #: 当前是第几次尝试（用于退避重试）




    # ----------------------------------------------------------- 托盘



    # ----------------------------------------------------------- 启动回调




    #: 前端首屏渲染探针。返回 "1"/"0" 两个**字符串**：
    #  实测 PySide6 6.11 对 array/object 的返回值一律给空串（见 memory 里的坑），
    #  布尔虽然没踩到，但统一走字符串最省心。




    # ---------------------------------------------------- 主题跟踪（低频）
    #
    # 老大问："有没有办法兼顾性能的同时做主题色的跟踪？"
    #
    # 有的，关键是**跟踪动作必须比取色本身便宜一个量级**：
    #
    #   · 只跑 L1（JS 读计算样式）——**完全不做截图**。
    #     像素截图要跨进程回读整幅位图，是重操作；读 computed style
    #     只是查 CSSOM，几十次调用在渲染进程里是微秒级。
    #   · 颜色**真的变了**才动 DWM（容差见 THEME_CHANGE_TOL）。
    #     绝大多数 tick 是"读一次、发现没变、什么都不做"。
    #   · 窗口最小化/不可见时直接跳过——没人在看就不用跟。
    #
    # 代价核算：默认 8 秒一次，每次一次 JS 往返。相对于 DSH 自己在跑的
    # 推理流，这个开销可以忽略。想彻底关掉把 theme_watch_ms 设成 0。




    #: 标题栏取色的重试节奏（毫秒）。首次很快，之后逐步退避，
    #  累计约 7.7 秒 —— 足够覆盖冷启动的样式计算与首绘，
    #  而正常情况第一次就成功，用户感知不到等待。





    # ----------------------------------------------------------- 服务控制


    # ----------------------------------------------------------- 用户可见反馈













    # ----------------------------------------------------------- 桌面壳更新











    # ------------------------------------------------------------- 内存采样
    #
    # 老大问的是"WebEngine 占了 1.7 GB，是不是泄露"。这个问题没法靠看任务
    # 管理器回答：那一个数字里混着三块内存，而且 Windows 会随时裁剪
    # WorkingSet（窗口最小化时 1.2 GB 能缩到 16 MB，看着像"释放了"，其实
    # PrivateBytes 一点没动）。所以先把三块分开、按时间记下来。
    #
    # 判读方法写在 `dsh_host/procmem.py` 的模块注释里；结论看 `logs/mem.log`。
    # 采样本身 = 一次进程快照 + 一次 JS 往返，每分钟一次，成本可以忽略。

    #: JS 侧内存探针：Chromium 的 `performance.memory` 是非标准接口，取不到就
    #: 只报 DOM 节点数。返回值必须是字符串——PySide6 对 object 一律给空串。







    # ------------------------------------------------------ 开发者模式






    # ---- 控制面板的适配层 ----
    #
    # 面板不认识 MainWindow，只认 PanelHost 暴露的这几个窄接口。开关与动作
    # **一律复用托盘那套既有方法**，不另写一份——否则两边行为迟早分叉。
    # 托盘菜单项虽然不再显示，但 QAction 还在，勾选/可用性状态依旧同步。



    # ---------------------------------------------------- 页面重建（自愈）



    # ---------------------------------------------------- 运行方式与数据目录








    # ----------------------------------------------------------- 远程会话











    # ----------------------------------------------------------- 窗口行为




    #: 退出时要等它收尾的后台任务（属性名）。面板自己的线程由 panel.shutdown() 管。




# ------------------------------------------------------------------ 单实例

def activate_existing() -> None:
    for w in QApplication.topLevelWidgets():
        if isinstance(w, MainWindow):
            w._show_main()


def main() -> int:
    # 只有开发者模式开启时才接管 fd 1/2。它要起一个读线程、重定向 stdout/stderr，
    # 属于"一直存在"的资源开销 —— 不开就不装，这是硬约束。
    # （运行中开关也能补装，见 MainWindow._apply_dev_mode。）
    if bool(config.get("dev_mode")):
        dsh_console.install()

    QApplication.setApplicationName("DSH-Web")
    QApplication.setOrganizationName("DSH")
    app = QApplication(sys.argv)

    # **最后一个窗口关闭不等于退出**（2026-10-20 体检发现，实测复现）。
    #
    # 这是个"关掉面板，整个程序就没了，但后台服务还在跑"的坑：
    #   · 主窗口的 closeEvent 是 ignore()+hide()（最小化到托盘），此时没有可见窗口；
    #   · 控制面板/开发者窗口都是**无父顶层窗口**，关掉它们就触发了 Qt 默认的
    #     "最后一个窗口关闭 → 退出应用"；
    #   · 但那个退出路径**不经过 `_quit()`**，于是后台服务不会被停掉：
    #     进程表里留着 node.exe 占着端口，`service.json` 也没清，用户下次启动
    #     会 attach 到这个孤儿服务上——表现是"关了个窗口，怎么后台一直在跑"。
    #
    # 所以：退出只走 `_quit()`（托盘菜单），窗口的开关一概不退出进程。
    app.setQuitOnLastWindowClosed(False)

    # 打包产物资源自检：设了环境变量就只检查、写报告、退出，不进 GUI。
    # CI 构建完 exe 后立刻跑这个，用来兜住"包里没带 assets"这类问题。
    #
    # **必须排在单实例检查之前**，两个理由：
    #   ① 自检是纯检查动作，不该因为"已经有一个实例在跑"就被跳过；
    #   ② 更重要的：自更新要用它**预检候选版本能不能启动**
    #      （见 dsh_host/selfupdate.preflight）。若排在单实例之后，
    #      预检会连上旧实例、把它的窗口弹出来、然后返回 0 ——
    #      这是个**假的"通过"**，坏包会就这么放过去。
    _rc = os.environ.get(RESOURCE_CHECK_ENV)
    if _rc:
        # 小心：报告含中文，而 CI（Windows runner）的 stdout 是 cp1252，
        # 直接 print 会抛 UnicodeEncodeError —— 异常会把这个分支的
        # 返回码一起带走，退出码变成 1，让人误以为是"自检不通过"。
        # 这个 cp1252 的坑我们的自测脚本早就踩过并加了 reconfigure，
        # 这里忘了照做。所以：先写文件（关键产物），打印只当附带输出。
        text, ok = resource_report()
        try:
            with open(_rc, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as e:
            try:
                sys.stderr.write("写报告失败: %s\n" % e)
            except Exception:                                  # noqa: BLE001
                pass
        try:
            print(text)
        except Exception:                                      # noqa: BLE001
            # 控制台编码不支持中文时忽略即可——报告已经落盘，
            # 判定只看返回码，不依赖这里的打印。
            pass
        return 0 if ok else 3

    # 单实例：已经在跑就把那个窗口叫出来，然后自己退出。
    # 判定依据是"能否连上那根管道"，不是"有没有同名进程"。
    sock = QLocalSocket()
    sock.connectToServer(SINGLETON_ID)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.close()
        return 0

    QLocalServer.removeServer(SINGLETON_ID)

    # 2026-10-25：这里原本写崩溃哨兵（启动写、退出删、下次读到就降级）。
    # 已随"不做自动降级"一起撤掉——见文件顶部 _HW_ACCEL 处的决策说明。
    # 旧版本留下的 webengine-state.json 在 config.purge_legacy_webengine_state()
    # 里顺手清掉，避免下次误以为还有人在读它。
    config.purge_legacy_webengine_state()

    # 全局图标先跟系统主题；MainWindow 取到界面色后会再切一次
    _ic = icon_for(dwm.system_uses_dark())
    if not _ic.isNull():
        app.setWindowIcon(_ic)

    # 上次自更新留下的 `.old` 备份，到这里可以删了。
    #
    # 位置刻意选在"能跑到主窗口"之后、且在服务启动之前的极早期：
    # 能走到这一行就说明**新版 exe 已经真的起来了**，那份退路不再需要。
    # 反过来，如果新版是坏文件，它根本起不来，`.old` 就一直留着——
    # 这正是我们想要的保护，所以清理只能放在"启动成功"这一侧，
    # 不能放进 bat 里"替换完就删"。
    try:
        removed = selfupdate.cleanup_leftover_backup()
        if removed:
            host_log("已清理上次自更新留下的备份：%s" % removed)
    except Exception as e:                                       # noqa: BLE001
        host_log("清理旧版备份失败（不影响使用）：%s: %s"
                 % (type(e).__name__, e))

    # 其他版本的 onefile 解包目录也一并清掉。
    # 每个版本用自己的解包目录（这样新旧才能同跑，见 release.yml 的说明），
    # 不清就会一版一版攒下来。正在运行的旧实例锁着自己那个，删不掉是正常的。
    #
    # **推迟到窗口出来之后再做**（2026-10-20 体检）：一次更新之后这里可能要删
    # 几份旧解包目录（每份几百 MB），rmtree 是同步的——排在创建主窗口之前，
    # 用户看到的就是"点了图标等好几秒才出现窗口"。它对启动不是必需的。
    def _cleanup_old_runtimes():
        try:
            gone = selfupdate.cleanup_stale_runtimes()
            if gone:
                host_log("已清理 %d 个旧版解包目录" % len(gone))
        except Exception as e:                                   # noqa: BLE001
            host_log("清理旧版解包目录失败（不影响使用）：%s: %s"
                     % (type(e).__name__, e))

    QTimer.singleShot(2500, _cleanup_old_runtimes)

    server = QLocalServer()
    server.listen(SINGLETON_ID)
    holder = []

    def on_conn():
        conn = server.nextPendingConnection()
        if conn:
            conn.waitForReadyRead(100)
            conn.close()
            QTimer.singleShot(0, activate_existing)
    server.newConnection.connect(on_conn)

    win = MainWindow()
    # 单实例 server 要留给 MainWindow：更新重启时**必须先把它关掉**，
    # 否则新实例会把自己当成重复实例直接退出（用户看到"点了重启没反应"）。
    win._singleton_server = server
    holder.append(win)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    # 必须 sys.exit(main())：main() 在资源自检失败时返回非 0，
    # 而裸调 main() 会把返回值丢掉 → 退出码恒为 0 → CI 拦不住坏产物。
    sys.exit(main())
