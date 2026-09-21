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

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QIcon, QAction, QDesktopServices, QImage
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QSystemTrayIcon, QMenu, QDialog, QMessageBox,
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QStackedWidget,
    QPlainTextEdit, QPushButton,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage

from dsh_host import config, contract, dwm, provision, selfupdate, updater
from dsh_host.contract import DshError, ServiceHandle
from dsh_host.version import HOST_VERSION

APP_TITLE = "DSH — DeepSeek Harness"
SINGLETON_ID = "DSH-Web-singleton"

ROOT = os.path.dirname(os.path.abspath(__file__))


def _asset_dir() -> str:
    """定位 assets 目录。**必须兼容打包后的布局。**

    这个函数存在的理由（2026-09-21 踩坑）
    -------------------------------------
    打包命令原先只有 `--windows-icon-from-ico=assets/icon.ico`，
    那只是把图标塞进 **exe 自身的 PE 资源**（资源管理器看的那个）。
    而运行时 `QIcon("assets/xxx.ico")` 是去**文件系统**读的 ——
    Nuitka onefile 不会自动带上 assets/ 目录，于是 QIcon 拿到空对象，
    `isNull()` 为真，图标切换**静默失效**（回落到 exe 资源里的那个）。

    所以两件事缺一不可：
      1. 打包命令要有 `--include-data-dir=assets=assets`
      2. 这里要能在"解包目录 / exe 同目录 / 源码目录"里找到 assets

    候选顺序覆盖三种运行形态：源码直跑、onefile 解包、standalone 目录。
    """
    bases: list[str] = []
    try:
        bases.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:                                          # pragma: no cover
        pass
    # Nuitka 打包后 __compiled__ 存在；frozen 覆盖其他打包器
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        bases.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    bases.append(os.getcwd())
    for b in bases:
        p = os.path.join(b, "assets")
        if os.path.isdir(p):
            return p
    # 全都找不到就返回首个候选下的 assets —— 后续 os.path.isfile 判空即可。
    # 不要在这里抛异常：图标缺失不该阻止应用启动。
    return os.path.join(bases[0] if bases else ".", "assets")


ASSET_DIR = _asset_dir()

#: 早期内置的单色图标，仅作兜底
ICON_PATH = os.path.join(ASSET_DIR, "icon.ico")

# 随标题栏深浅切换的两套图标。
#
# 为什么必须两套：DSH 的暗色主题下标题栏是深色，**黑色图标等于看不见**
# （老大实机发现）。所以深色标题栏配白色图标、浅色标题栏配深色图标。
#
# 两份 ico 由 tools/make_icons.py 从 assets/deepseek.svg 生成，
# 每个文件内含 16~256 共 9 个尺寸——单尺寸 ICO 在小图标下会糊。
ICON_FOR_DARK_BG = os.path.join(ASSET_DIR, "icon-light.ico")
ICON_FOR_LIGHT_BG = os.path.join(ASSET_DIR, "icon-dark.ico")


def icon_for(dark_bg: bool) -> QIcon:
    """按底色深浅挑图标。两套都找不到就退回内置单色图标。"""
    path = ICON_FOR_DARK_BG if dark_bg else ICON_FOR_LIGHT_BG
    if not os.path.isfile(path):
        path = ICON_PATH
    return QIcon(path) if os.path.isfile(path) else QIcon()


#: 环境变量：设成文件路径时，应用只做资源自检、写报告、退出。
#
# 为什么需要它
# ------------
# "图标在源码里能用、打包后静默失效"这类问题，**只能在打包产物上验**。
# 源码树里 assets/ 永远存在，测试全绿也说明不了包里带没带。
# 所以给打包产物一个可自动化的自检入口，CI 构建后立刻跑它。
RESOURCE_CHECK_ENV = "DSH_HOST_RESOURCE_CHECK"


def resource_report() -> tuple[str, bool]:
    """检查运行环境里的资源与能力是否齐备。返回 (报告文本, 是否通过)。

    只做纯粹的自检，不涉及业务逻辑。
    """
    packaged = ("__compiled__" in globals()) or bool(getattr(sys, "frozen", False))
    lines = [
        "DSH Launcher 产物自检",
        "  sys.frozen       = %s" % getattr(sys, "frozen", False),
        "  __compiled__     = %s" % ("__compiled__" in globals()),
        "  __file__         = %s" % globals().get("__file__", "(无)"),
        "  argv[0]          = %s" % (sys.argv[0] if sys.argv else "(无)"),
        "  sys.executable   = %s" % getattr(sys, "executable", "(无)"),
        "  ASSET_DIR        = %s" % ASSET_DIR,
        "  ASSET_DIR 存在    = %s" % os.path.isdir(ASSET_DIR),
    ]
    ok = True
    for label, p in (("icon.ico ", ICON_PATH),
                     ("icon-light", ICON_FOR_DARK_BG),
                     ("icon-dark ", ICON_FOR_LIGHT_BG)):
        exists = os.path.isfile(p)
        size = os.path.getsize(p) if exists else 0
        ic = QIcon(p) if exists else QIcon()
        loadable = not ic.isNull()
        if not (exists and loadable):
            ok = False
        lines.append("  %s  存在=%-5s 大小=%-7d 可加载=%s  %s"
                     % (label, exists, size, loadable, p))

    # --- 自更新可用性 ---
    # 这条专门防一个会静默失效的坑：Nuitka 不设 sys.frozen，
    # 若 running_as_exe() 只看 frozen，打包版会被判成"源码运行" →
    # 自更新菜单项被禁用 → 功能直接死掉。必须在产物上验。
    try:
        supported, reason = selfupdate.self_update_supported()
        exe = selfupdate.current_exe()
    except Exception as e:                                     # noqa: BLE001
        supported, reason, exe = False, "%s: %s" % (type(e).__name__, e), "?"
    lines.append("  自更新可用        = %s%s"
                 % (supported, ("  (%s)" % reason) if reason else ""))
    lines.append("  current_exe      = %s" % exe)
    if packaged and not supported:
        ok = False
        lines.append("  ↑ 打包版里自更新应当可用，否则菜单项会被禁用")
    if packaged and not exe.lower().endswith(".exe"):
        ok = False
        lines.append("  ↑ 打包版里 current_exe 应指向 .exe")

    lines.append("  结论             = %s" % ("通过" if ok else "**失败**"))
    return "\n".join(lines), ok


#: 主题跟踪的"变色"容差。小于这个差值不动手——避免因渐变/抗锯齿噪声
#  反复重设 DWM 属性（既无意义，也会让标题栏偶尔闪）。
THEME_CHANGE_TOL = 6

#: 主题跟踪每秒允许的最大检查次数上限的间隔（毫秒）。配置值低于它会被抬到它，
#  防止有人把 theme_watch_ms 设成 50 之类把渲染进程变成忙轮询。
_THEME_WATCH_MIN_MS = 1500


def _parse_probe_color(value) -> tuple[int, int, int] | None:
    """把 JS 探针返回的 "r,g,b" 解析成元组。解析失败返回 None。"""
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 3:
        return None
    try:
        rgb = tuple(int(p) for p in parts)
    except ValueError:
        return None
    if any(c < 0 or c > 255 for c in rgb):
        return None
    return rgb


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
_JS_THEME_PROBE = r"""
(function () {
  function parse(c) {
    var m = /rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/
              .exec(c || '');
    if (!m) { return null; }
    var a = (m[4] === undefined) ? 1 : parseFloat(m[4]);
    if (!(a > 0.5)) { return null; }          // 半透明及以上不算"看得见的底色"
    return [parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3], 10)];
  }
  function effectiveBg(el) {
    var guard = 0;
    while (el && el.nodeType === 1 && guard++ < 64) {
      var c = parse(getComputedStyle(el).backgroundColor);
      if (c) { return c; }
      el = el.parentElement;
    }
    return null;
  }
  var W = window.innerWidth, H = window.innerHeight;
  if (W < 4 || H < 4) { return null; }
  var ys = [0.015, 0.03, 0.05, 0.08];
  var xs = [0.03, 0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 0.97];
  var counts = {}, best = null, bestN = 0;
  for (var i = 0; i < ys.length; i++) {
    var y = Math.max(1, Math.round(H * ys[i]));
    for (var j = 0; j < xs.length; j++) {
      var x = Math.max(1, Math.round(W * xs[j]));
      var c = effectiveBg(document.elementFromPoint(x, y));
      if (!c) { continue; }
      var k = c.join(',');
      counts[k] = (counts[k] || 0) + 1;
      if (counts[k] > bestN) { bestN = counts[k]; best = c; }
    }
  }
  return best ? best.join(',') : null;
})();
"""


def host_log(msg: str) -> None:
    """桌面壳自身的运行日志。

    之前 `config.host_log()` 定义了却从没写过东西 —— 结果"点了检查更新没反应"
    这种事事后完全无从查起。凡是"用户看得见的现象"，背后都要有一条日志。
    """
    try:
        with open(config.host_log(), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


# ------------------------------------------------------------------ 服务规划

def plan_service(inst: contract.DshInstall, requested: int, allow_fallback: bool,
                 logf: str) -> tuple[str, int]:
    """决定"接管已有服务"还是"新起一个"，返回 (mode, port)。

    mode: 'attach' 端口上已是自家 DSH，直接接管
          'launch' 需要新起，port 是目标端口（0 表示交给系统分配）
    """
    host = inst.profile.get("service_host", "127.0.0.1")
    if not requested:
        return "launch", 0
    if not contract.tcp_open(host, requested):
        return "launch", requested

    # 端口被占：先用 HTTP 指纹分辨是不是自家服务
    probe_url = contract.recover_url(inst, requested, logf)
    if contract.looks_like_dsh(probe_url):
        return "attach", requested
    if allow_fallback:
        return "launch", 0
    raise DshError(
        f"端口 {requested} 已被其他程序占用（响应不像 DSH）。\n"
        "可以改端口，或在设置里允许占用时自动改用系统分配的端口。"
    )


def attach_handle(inst: contract.DshInstall, port: int, logf: str) -> ServiceHandle:
    """为已在运行的服务构造句柄。PID 取自我们自己的状态文件——不猜。"""
    host = inst.profile.get("service_host", "127.0.0.1")
    state = contract.load_state() or {}
    pid = int(state.get("pid") or 0) if int(state.get("port") or 0) == port else 0
    url = contract.recover_url(inst, port, logf)
    token = ""
    if "token=" in url:
        token = url.split("token=", 1)[1]
    return ServiceHandle(install=inst, pid=pid, host=host, requested_port=port,
                         port=port, url=url, token=token, log_path=logf,
                         started_at=time.time(), proc=None)


# ------------------------------------------------------------------ 后台工作

class StartupWorker(QThread):
    progress = Signal(str)
    ready = Signal(object)
    failed = Signal(str)
    needs_setup = Signal(object)          # EnvironmentReport

    def __init__(self, requested_port: int, allow_fallback: bool, logf: str):
        super().__init__()
        self.requested_port = requested_port
        self.allow_fallback = allow_fallback
        self.logf = logf

    def run(self):
        try:
            # 先做环境自检：缺 Node / 缺 DSH 时不是"启动失败"，而是"还没准备好"，
            # 这两件事对用户的意义完全不同，走不同的界面
            self.progress.emit("正在检查运行环境…")
            rep = contract.diagnose()
            if not rep.ready:
                self.needs_setup.emit(rep)
                return

            inst = rep.install
            self.progress.emit(inst.describe())

            mode, port = plan_service(inst, self.requested_port,
                                      self.allow_fallback, self.logf)
            if mode == "attach":
                self.ready.emit(attach_handle(inst, port, self.logf))
                return

            self.progress.emit("正在启动 DSH 服务…" if port else
                               "端口被占用，改用系统分配的端口启动…")
            handle = contract.launch(inst, port, self.logf, cwd=config.log_dir())
            contract.save_state(handle)

            def tick(h):
                self.progress.emit("等待服务就绪… %ds" % int(time.time() - h.started_at))

            contract.wait_ready(handle, on_tick=tick)
            contract.save_state(handle)
            self.ready.emit(handle)
        except DshError as e:
            self.failed.emit(str(e))
        except Exception as e:                                   # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class ProvisionWorker(QThread):
    """跑环境供给：装 Node → 装 DSH。每一步都会重新诊断，不假定成功。"""
    line = Signal(str)
    phase = Signal(str, str)
    done = Signal(bool, str)

    def run(self):
        try:
            ok, msg = provision.provision_all(
                on_line=self.line.emit,
                on_phase=lambda key, text: self.phase.emit(key, text),
            )
            self.done.emit(ok, msg)
        except Exception as e:                                   # noqa: BLE001
            self.done.emit(False, f"{type(e).__name__}: {e}")


class CheckWorker(QThread):
    """后台检查更新。

    失败**必须回报**。静默失败只适用于无人触发的自动检查；
    用户手动点了「检查更新」却什么都看不到，是最糟的一种反馈。
    """
    found = Signal(object)
    failed = Signal(str)
    done = Signal()

    def run(self):
        try:
            inst = contract.resolve_install()
            info = updater.check(inst)
            host_log("检查更新：当前 %s → 目标 %s（%s 通道，动作 %s）"
                     % (info.current, info.target, info.channel, info.action))
            self.found.emit(info)
        except Exception as e:                                   # noqa: BLE001
            host_log("检查更新失败：%s: %s" % (type(e).__name__, e))
            self.failed.emit("%s: %s" % (type(e).__name__, e))
        finally:
            self.done.emit()


class NotesWorker(QThread):
    ready = Signal(str)

    def __init__(self, version: str):
        super().__init__()
        self.version = version

    def run(self):
        try:
            self.ready.emit(updater.fetch_release_notes(self.version) or "")
        except Exception:                                        # noqa: BLE001
            self.ready.emit("")


class HostCheckWorker(QThread):
    """检查桌面壳自身的更新（GitHub Release）。

    与 CheckWorker 分开是因为两者查的是**完全不同的源**：
    一个是 npm registry（DSH），一个是 GitHub API（这个 exe）。
    合成一个会让"检查更新"这个动作的语义变模糊。
    """
    found = Signal(object, str)
    failed = Signal(str)
    done = Signal()

    def run(self):
        try:
            rel, note = selfupdate.check_host_update()
            host_log("检查桌面壳更新：%s" % note)
            self.found.emit(rel, note)
        except Exception as e:                                   # noqa: BLE001
            host_log("检查桌面壳更新失败：%s: %s" % (type(e).__name__, e))
            self.failed.emit("%s: %s" % (type(e).__name__, e))
        finally:
            self.done.emit()


class HostUpdateWorker(QThread):
    """下载桌面壳新版本。下载完只做准备，不退出进程。"""
    line = Signal(str)
    done = Signal(bool, str)

    def __init__(self, rel):
        super().__init__()
        self.rel = rel

    def run(self):
        try:
            def on_progress(got: int, total: int):
                if total > 0:
                    self.line.emit("已下载 %.1f / %.1f MB"
                                   % (got / 1048576, total / 1048576))
                else:
                    self.line.emit("已下载 %.1f MB" % (got / 1048576))

            msg = selfupdate.apply(self.rel, on_progress=on_progress)
            self.done.emit(True, msg)
        except selfupdate.SelfUpdateError as e:
            host_log("桌面壳更新失败：%s" % e)
            self.done.emit(False, str(e))
        except Exception as e:                                   # noqa: BLE001
            host_log("桌面壳更新异常：%s: %s" % (type(e).__name__, e))
            self.done.emit(False, "%s: %s" % (type(e).__name__, e))


class UpdateWorker(QThread):
    line = Signal(str)
    done = Signal(bool, str, object)

    def __init__(self, target: str, handle, restart_port: int, logf: str):
        super().__init__()
        self.target = target
        self.handle = handle
        self.restart_port = restart_port
        self.logf = logf

    def run(self):
        box: dict = {}

        def stop_service():
            contract.stop(self.handle, port=(self.handle.port if self.handle else None))
            contract.save_state(None)
            self.line.emit("后台服务已停止")

        def start_service():
            inst = contract.resolve_install(force=True)
            port = self.restart_port
            if port and contract.tcp_open(inst.profile.get("service_host", "127.0.0.1"), port):
                port = 0
            h = contract.launch(inst, port, self.logf, cwd=config.log_dir())
            contract.wait_ready(h, on_tick=lambda x: self.line.emit(
                "等待服务就绪… %ds" % int(time.time() - x.started_at)))
            contract.save_state(h)
            box["handle"] = h

        try:
            inst = contract.resolve_install()
            ok, detail = updater.update_to(inst, self.target, stop_service,
                                           start_service, on_line=self.line.emit)
            self.done.emit(ok, detail, box.get("handle"))
        except Exception as e:                                   # noqa: BLE001
            self.done.emit(False, f"{type(e).__name__}: {e}", box.get("handle"))


# ------------------------------------------------------------------ 首次运行引导

class DiagnoseWorker(QThread):
    done = Signal(object)

    def run(self):
        try:
            self.done.emit(contract.diagnose())
        except Exception:                                        # noqa: BLE001
            self.done.emit(contract.EnvironmentReport(detail="环境检测失败。"))


class OnboardingDialog(QDialog):
    """首次运行引导：把缺的运行环境补齐。

    文档要求：文案面向使用者。用户不需要知道 npm prefix、node_modules 是什么，
    这些只进日志区；正文只说"需要准备什么、要多久、能不能离开"。
    """

    def __init__(self, parent, report):
        super().__init__(parent)
        self.setWindowTitle("首次运行准备")
        self.setMinimumWidth(660)
        self.resize(700, 540)
        self.report = report
        self.succeeded = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(14)

        intro = QLabel("运行 DSH 需要先准备好运行环境。点「开始安装」，程序会自动完成。")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        holder = QWidget()
        self.rows_box = QVBoxLayout(holder)
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(6)
        lay.addWidget(holder)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("安装过程会显示在这里")
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.site_btn = QPushButton("打开 Node.js 官网")
        self.site_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(provision.NODE_WEBSITE)))
        self.recheck_btn = QPushButton("重新检测")
        self.recheck_btn.clicked.connect(self._recheck)
        self.go_btn = QPushButton("开始安装")
        self.go_btn.clicked.connect(self._start)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        for b in (self.site_btn, self.recheck_btn, self.go_btn, self.close_btn):
            row.addWidget(b)
        lay.addLayout(row)

        self._render()

    # -------------------------------------------------- 渲染

    def _render(self):
        while self.rows_box.count():
            item = self.rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for r in self.report.rows():
            line = QWidget()
            h = QHBoxLayout(line)
            h.setContentsMargins(0, 0, 0, 0)
            name = QLabel(r["name"])
            name.setMinimumWidth(190)
            state = QLabel(r["text"])
            state.setStyleSheet("color: %s;" % ("#0F6E56" if r["ok"] else "#A32D2D"))
            h.addWidget(name)
            h.addWidget(state)
            h.addStretch(1)
            self.rows_box.addWidget(line)

        self.hint.setText(self.report.detail or "")
        self.site_btn.setVisible(bool(self.report.need_node))
        self.go_btn.setText("环境已就绪" if self.report.ready else "开始安装")
        self.go_btn.setEnabled(not self.report.ready)

    def _set_busy(self, busy: bool):
        for b in (self.go_btn, self.recheck_btn, self.close_btn):
            b.setEnabled(not busy)
        if not busy:
            self._render()

    # -------------------------------------------------- 动作

    def _start(self):
        self._set_busy(True)
        self.log.clear()
        self.log.appendPlainText("开始准备运行环境…")
        self.hint.setText("正在准备…")
        self._worker = ProvisionWorker()
        self._worker.line.connect(self.log.appendPlainText)
        self._worker.phase.connect(lambda _k, text: self.hint.setText(text))
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, msg: str):
        self.log.appendPlainText("\n" + msg)
        if ok:
            self.succeeded = True
            QTimer.singleShot(1200, self.accept)
            return
        self._set_busy(False)
        self._recheck()

    def _recheck(self):
        self._set_busy(True)
        self.recheck_btn.setText("检测中…")
        self._diag = DiagnoseWorker()
        self._diag.done.connect(self._on_recheck)
        self._diag.start()

    def _on_recheck(self, rep):
        self.report = rep
        self.recheck_btn.setText("重新检测")
        self._set_busy(False)
        if rep.ready:
            self.succeeded = True
            self.log.appendPlainText("环境已就绪，正在启动。")
            QTimer.singleShot(800, self.accept)


# ------------------------------------------------------------------ 更新对话框

class UpdateDialog(QDialog):
    def __init__(self, parent, info: updater.UpdateInfo, handle):
        super().__init__(parent)
        self.setWindowTitle("DSH 更新")
        self.setMinimumWidth(620)
        self.resize(640, 460)
        self.info = info
        self.handle = handle
        self.new_handle = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        self.headline = QLabel()
        self.headline.setWordWrap(True)
        lay.addWidget(self.headline)

        home = contract.resolve_home() or "（由 DSH 自行决定）"
        detail = QLabel(
            f"当前版本　{info.current}（{updater.channel_note(info.current)}）\n"
            f"目标版本　{info.target}（{updater.channel_note(info.target)}）\n"
            f"更新方式　npm install -g {updater.PKG_NAME}@{info.target}\n"
            f"\n"
            f"你的会话记录、登录状态与设置存放于：\n{home}\n"
            f"（如需自行备份，复制上面这个目录即可）"
        )
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(detail)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.notes_btn = QPushButton("查看更新说明")
        self.notes_btn.clicked.connect(self._load_notes)
        self.go_btn = QPushButton("开始更新")
        self.go_btn.clicked.connect(self._start)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.notes_btn)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

        self._refresh_headline()

    def _refresh_headline(self):
        if self.info.action == "downgrade":
            self.headline.setText(
                f"将把 DSH 从 {self.info.current} 切回稳定版 {self.info.target}。\n\n"
                "注意：旧版本可能打不开新版本产生的会话记录。"
                "这些记录不会被删除，只是旧版本读不了——这是上游的格式版本机制，"
                "我们无法规避。")
        else:
            extra = "这是预览通道版本，官方明示可能存在破坏性变更。" \
                if self.info.channel == "alpha" else ""
            self.headline.setText(
                f"将把 DSH 从 {self.info.current} 更新到 {self.info.target}。{extra}\n\n"
                "更新只替换程序本身，不会改动你的会话记录、登录状态和设置。")

    def _load_notes(self):
        self.notes_btn.setEnabled(False)
        self.log.appendPlainText("正在获取更新说明（GitHub，可能较慢）…")
        self._notes = NotesWorker(self.info.target)
        self._notes.ready.connect(self._on_notes)
        self._notes.start()

    def _on_notes(self, text: str):
        self.notes_btn.setEnabled(True)
        if not text:
            self.log.appendPlainText("未能获取更新说明（网络原因或该版本无发布说明）。")
            return
        self.log.appendPlainText("\n===== 更新说明 =====\n" + text.strip() + "\n")

    def _start(self):
        self.go_btn.setEnabled(False)
        self.notes_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.log.clear()
        self._worker = UpdateWorker(self.info.target, self.handle,
                                    int(config.get("port") or 0), config.service_log())
        self._worker.line.connect(self.log.appendPlainText)
        self._worker.line.connect(host_log)      # 更新全过程也进桌面壳日志
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, detail: str, new_handle):
        self.new_handle = new_handle
        self.close_btn.setEnabled(True)
        host_log("更新%s：%s" % ("成功" if ok else "失败", detail.replace("\n", " | ")[:400]))
        if ok:
            self.log.appendPlainText("\n更新完成：DSH %s" % detail)
            QTimer.singleShot(1200, self.accept)
        else:
            self.log.appendPlainText("\n更新失败，已尝试回滚：\n" + detail)
            self.go_btn.setEnabled(True)
            self.notes_btn.setEnabled(True)


class HostUpdateDialog(QDialog):
    """桌面壳自身更新的确认与进度窗口。

    与 UpdateDialog 分开，因为两者做的事完全不同：
    那个换的是 DSH 程序（npm 装），这个换的是**正在运行的这个 exe**
    （下载替换 + 重启）。合成一个窗口会让人以为更新完 DSH 界面就该变。
    """

    RESTART_NOW = 100          # 自定义返回值，区别于 Accepted / Rejected

    def __init__(self, parent, rel):
        super().__init__(parent)
        self.setWindowTitle("桌面壳更新")
        self.setMinimumWidth(620)
        self.resize(640, 440)
        self.rel = rel
        self._prepared = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        headline = QLabel(
            f"DSH Launcher 有新版本 v{rel.version}（当前 v{HOST_VERSION}）。\n\n"
            "这是桌面壳程序自身的更新，与 DSH 无关——\n"
            "更新后你的 DSH 版本、会话记录、登录状态都不受影响。")
        headline.setWordWrap(True)
        lay.addWidget(headline)

        size_txt = ""
        if rel.asset and rel.asset.size:
            size_txt = f"（约 {rel.asset.size / 1048576:.1f} MB）"
        detail = QLabel(
            f"下载地址　GitHub Release {size_txt}\n"
            f"替换方式　下载完成后自动替换当前程序并重启\n"
            f"\n"
            f"更新过程会关闭本窗口。若替换失败，旧版本会保留为\n"
            f"　　{selfupdate.current_exe()}.old\n"
            f"可手工改回。")
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(detail)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.notes_btn = QPushButton("查看更新说明")
        self.notes_btn.clicked.connect(self._load_notes)
        self.go_btn = QPushButton("下载并更新")
        self.go_btn.clicked.connect(self._start)
        self.close_btn = QPushButton("稍后")
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.notes_btn)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

        if rel.notes:
            self.log.setPlainText(rel.notes.strip())

    def _load_notes(self):
        self.notes_btn.setEnabled(False)
        if self.rel.notes:
            self.log.setPlainText(self.rel.notes.strip())
        else:
            self.log.setPlainText("该版本没有附带更新说明。")
        self._append_meta()

    def _append_meta(self):
        self.log.appendPlainText(
            "\n开通时间　%s\n项目地址　%s"
            % (self.rel.published_at or "（未知）", self.rel.html_url))

    def _start(self):
        self.go_btn.setEnabled(False)
        self.notes_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.log.clear()
        self._worker = HostUpdateWorker(self.rel)
        self._worker.line.connect(self.log.appendPlainText)
        self._worker.line.connect(host_log)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, msg: str):
        if not ok:
            self.log.appendPlainText("\n更新失败：\n" + msg)
            self.go_btn.setEnabled(True)
            self.notes_btn.setEnabled(True)
            self.close_btn.setEnabled(True)
            return

        self._prepared = True
        self.log.appendPlainText("\n" + msg)
        # 下载成功但还没替换——替换要靠我们退出后由脚本完成。
        # 所以这里把按钮换成明确的"立即重启"，而不是自动退出：
        # 静默退出会让用户以为程序崩了。
        self.go_btn.setText("立即重启完成更新")
        self.go_btn.setEnabled(True)
        try:
            self.go_btn.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.go_btn.clicked.connect(self._restart)
        self.close_btn.setEnabled(True)
        self.close_btn.setText("稍后手动重启")

    def _restart(self):
        self.done(self.RESTART_NOW)


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


class MainWindow(QMainWindow):
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
        self.host_release = None          # 有值表示桌面壳有新版本可用
        self.view = None
        self.profile = None
        self._theme_timer = None          # 主题跟踪定时器（低频）
        self._last_titlebar_rgb = None    # 上次上色用的界面色，用于变更比对

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        # 初始图标跟随**系统**主题——此刻还不知道界面是什么色。
        # 取色成功后会被 _set_theme_icon 换成匹配界面底色的那套。
        self._icon_dark_bg = None
        self._set_theme_icon(dwm.system_uses_dark())
        self._build_loading_page()
        self._build_tray()

        self.starter = None
        self._launch_startup()

    def _launch_startup(self):
        self.starter = StartupWorker(int(config.get("port") or 0),
                                     bool(config.get("port_auto_fallback")),
                                     config.service_log())
        self.starter.progress.connect(self._set_tip)
        self.starter.ready.connect(self._on_ready)
        self.starter.failed.connect(self._on_failed)
        self.starter.needs_setup.connect(self._on_needs_setup)
        self.starter.start()

    # ----------------------------------------------------------- 界面

    def _build_loading_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        bar.setFixedHeight(3)
        self.tip = QLabel("正在启动 DSH 服务…")
        self.tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tip.setWordWrap(True)
        lay.addWidget(bar)
        lay.addStretch(1)
        lay.addWidget(self.tip)
        lay.addStretch(1)
        self.stack.addWidget(page)

    def _set_tip(self, text: str):
        self.tip.setText(text)

    def _ensure_webview(self):
        if self.view is not None:
            return
        self.profile = QWebEngineProfile("dsh-profile", self)
        self.profile.setPersistentStoragePath(config.profile_dir())
        self.view = QWebEngineView(self)
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.stack.addWidget(self.view)

    # ----------------------------------------------------------- 标题栏自适应

    def _apply_titlebar(self, rgb):
        """把取到的主色上到标题栏。

        三个必须一起设的属性，少一个就会出现违和的细节：
          CAPTION_COLOR  底色
          TEXT_COLOR     标题文字色——不设的话浅底上会白字（读不了）
          DARK_MODE      影响系统绘制的关闭按钮/边框高光
        """
        if not dwm.available() or rgb is None:
            return
        mute = float(config.get("titlebar_mute") or 0.0)
        # 用降饱和（往**自身灰度**拉）而不是往固定深灰 blend：
        # 后者会把纯白拉成灰色，浅色主题下观感明显违和。
        # 详见 dwm.desaturate 的 docstring。
        bg = dwm.desaturate(rgb, mute) if mute else rgb
        fg = dwm.contrast_text(bg)
        dark = dwm.is_dark(bg)
        self._last_titlebar_rgb = rgb

        ok = dwm.set_caption_color(self, bg)
        dwm.set_text_color(self, fg)
        dwm.set_border_color(self, bg)
        dwm.set_dark_mode(self, dark)
        # 图标要跟着底色走：深色标题栏配白图标，否则黑图标看不见。
        # 用 try 包住——图标设置失败绝不能影响上色这个主功能。
        try:
            self._set_theme_icon(dark)
        except Exception as e:                                   # noqa: BLE001
            host_log("切换主题图标失败（不影响上色）：%s: %s"
                     % (type(e).__name__, e))
        if ok:
            host_log("标题栏上色：底色 %s（取自界面 %s）文字 %s"
                     % (dwm.to_hex(bg), dwm.to_hex(rgb), dwm.to_hex(fg)))

    def _set_theme_icon(self, dark_bg: bool) -> None:
        """按标题栏深浅切换窗口/托盘图标。

        用实例属性记住上次的状态，避免每次重设——重设窗口图标会让
        任务栏图标闪一下，而且托盘图标重建代价更高。
        """
        if getattr(self, "_icon_dark_bg", "unset") == dark_bg:
            return
        ic = icon_for(dark_bg)
        if ic.isNull():
            return
        self._icon_dark_bg = dark_bg
        self.setWindowIcon(ic)
        app = QApplication.instance()
        if app is not None:
            try:
                app.setWindowIcon(ic)
            except Exception:                                      # noqa: BLE001
                pass
        tray = getattr(self, "tray", None)
        if tray is not None:
            try:
                tray.setIcon(ic)
            except Exception:                                      # noqa: BLE001
                pass

    def _sample_titlebar_color(self, attempt: int = 0):
        """取界面主题色，给标题栏上色。**三层降级**。

        为什么必须降级（2026-09-21 实机教训）
        -------------------------------------
        原实现只有一层：`self.view.grab()`。但这是**不可靠**的来源 ——
        QWebEngineView 的网页在独立渲染进程/合成器里画，
        `QWidget.grab()` 不保证能拿到那些像素。实测在某些环境下它返回
        的是 widget 自身的空白背景（整片纯色），取出来的"主题色"是
        空白色，标题栏反而被染成一片灰白。

        所以按可靠性排序依次尝试，任一层成功即止：

          L1  JS 读计算样式    最准——直接拿到界面真正的背景色，
                               与合成方式无关。用的都是标准 DOM API
                               （elementFromPoint + getComputedStyle
                               + 向上找非透明祖先），**不依赖 DSH 的
                               私有类名或层级结构**，所以不违反分层铁律。
          L2  GDI PrintWindow  向系统要窗口真实像素（已验证可靠）。
                               截客户区顶部，兼做纯色校验。
          L3  view.grab()      兜底。同样做纯色校验，纯色则判失败。

        三层都失败就保持系统默认标题栏——**不要拿一个不可信的颜色上色**。

        为什么只取一次
        --------------
        定时器触发一次，不做持续监听：界面主题在使用中基本不变，
        而持续截图会拖慢界面。

        为什么要延迟
        ------------
        `loadFinished` 只代表 DOM 就绪，样式与首绘可能还没完成，
        这时取到的是空白页颜色。所以等一段固定时间再取。
        """
        if not config.get("adaptive_titlebar") or not dwm.available():
            return
        if self.view is None:
            return
        self._tb_attempt = attempt
        self._probe_trace = []
        try:
            self.view.page().runJavaScript(_JS_THEME_PROBE,
                                           self._on_js_theme_color)
        except Exception as e:                                   # noqa: BLE001
            host_log("标题栏取色异常（JS）：%s: %s" % (type(e).__name__, e))
            self._sample_by_pixels()

    #: 各层取色失败的记录，用一次日志把"为什么没上色"讲清楚
    _probe_trace: list = []
    #: 当前是第几次尝试（用于退避重试）
    _tb_attempt: int = 0

    def _on_js_theme_color(self, value):
        """L1 回调。value 形如 "30,32,36"，取不到是 None。"""
        rgb = _parse_probe_color(value)
        if rgb:
            host_log("标题栏取色：来源=JS 计算样式 %s" % dwm.to_hex(rgb))
            self._apply_titlebar(rgb)
            return
        self._probe_trace.append("JS 未取到")
        self._sample_by_pixels()

    def _sample_by_pixels(self):
        """L2 GDI PrintWindow -> L3 view.grab()。"""
        # --- L2：窗口真实像素 ---
        try:
            rgb = dwm.capture_window_top(int(self.winId()))
            if rgb:
                host_log("标题栏取色：来源=窗口像素 %s" % dwm.to_hex(rgb))
                self._apply_titlebar(rgb)
                return
            self._probe_trace.append("窗口像素未取到（纯色或截取失败）")
        except Exception as e:                                   # noqa: BLE001
            self._probe_trace.append("窗口像素异常 %s" % type(e).__name__)

        # --- L3：控件截图兜底 ---
        try:
            rgb = self._grab_view_color()
            if rgb:
                host_log("标题栏取色：来源=控件截图 %s" % dwm.to_hex(rgb))
                self._apply_titlebar(rgb)
                return
            self._probe_trace.append("控件截图未取到（纯色）")
        except Exception as e:                                   # noqa: BLE001
            self._probe_trace.append("控件截图异常 %s" % type(e).__name__)

        host_log("标题栏取色三层均失败，保持系统默认标题栏：%s"
                 % "；".join(self._probe_trace))
        # 多半是页面还没画出来（loadFinished 早于首绘）。退避后重试，
        # 而不是立刻放弃或死等。
        self._schedule_titlebar_probe(self._tb_attempt + 1)

    def _grab_view_color(self):
        """L3：截 QWebEngineView 取色。截不到有效内容返回 None。

        两段式判断：**空白判定看整幅**，取色只看顶部一条。
        不能只拿顶部一条判空白——真实界面的顶部可能就是一条纯色工具栏，
        那完全正常，据此判"没渲染"会误杀。

        性能：只拷顶部 6%，不整图 toBytes()（整图 1200x800 是 5.7MB、
        4K 是 33MB）。constBits 的 memoryview 需先 cast("B")——默认
        视图元素是 4 字节结构体，按字节切会算错元素数。
        """
        img = self.view.grab().toImage().convertToFormat(
            QImage.Format.Format_RGBA8888)
        w, h = img.width(), img.height()
        if w <= 0 or h <= 0:
            return None
        mv = img.constBits()
        if isinstance(mv, (bytes, bytearray)):
            mv = memoryview(mv)
        elif getattr(mv, "itemsize", 1) != 1:
            mv = mv.cast("B")
        stride = img.bytesPerLine()      # 不一定等于 w*4（行可能对齐填充）
        pad = max(0, int(w * 0.02))
        x0, span = pad, max(1, w - pad * 2)

        def _rows(y0, n, step=1):
            """取第 y0 行起的 n 行（左右各让开 pad），紧排成 RGB 缓冲。"""
            return b"".join(
                bytes(mv[y * stride + x0 * 4: y * stride + (x0 + span) * 4])
                for y in range(y0, min(y0 + n, h), step))

        # 先判整幅是否一片纯色（没渲染出来的控件背景就是整片同色）。
        # 按行抽样——逐行全转在 4K 下要搬 33MB，而"是否空白"只需
        # 有代表性的采样。上限约 96 行，代价与分辨率解耦。
        step_y = max(1, h // 96)
        full = _rows(0, h, step=step_y)
        sampled = len(range(0, h, step_y))
        if not full or dwm.flat_ratio(full, span, sampled) >= dwm.FLAT_RATIO:
            return None

        rows = max(1, int(h * 0.06))
        top = _rows(0, rows)          # 取色这一条要逐行，保证准确
        if not top:
            return None
        return dwm.dominant_color(top, span, len(top) // (span * 4))

    # ----------------------------------------------------------- 托盘

    def _build_tray(self):
        """托盘菜单。

        分组原则（2026-09-20 重构）
        --------------------------
        按"用户想做什么"分组，而不是按"我们的模块怎么划分"分组：

          1. 显示主窗口
          2. 环境/运行     准备运行环境
          3. DSH 本身      检查 DSH 更新 / 预览计划 / 回归稳定版 / 回滚
          4. 桌面壳        检查桌面壳更新 / 关于
          5. 系统          打开日志目录 / 退出

        DSH 更新与桌面壳更新**必须分开**：用户看到"有更新"时得知道
        换的是 DSH 还是这个窗口程序，否则会以为更新完界面就该变。

        关于「停止后台服务」被移除
        --------------------------
        它和「退出」语义不同（一个停服务留窗口，一个关窗口），但用户点它的
        意图几乎都是"我要结束这一切"。两个入口殊途同归，只会让人犹豫。
        现在只保留「退出」，且退出时**默认连带停止后台服务**——
        想单独留服务的人可以用「仅退出窗口」的替代操作。
        """
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()

        act_show = QAction("显示主窗口", self)
        act_show.triggered.connect(self._show_main)

        # 只在环境确实缺东西时才可用——平时不给一个点了没反应的菜单项
        self.act_setup = QAction("准备运行环境…", self)
        self.act_setup.setEnabled(False)
        self.act_setup.triggered.connect(self._prepare_env)

        # --- DSH 相关 ---
        self.act_check = QAction("检查 DSH 更新…", self)
        self.act_check.triggered.connect(lambda: self._check_update(manual=True))

        self.act_prev = QAction("加入预览计划", self)
        self.act_prev.setCheckable(True)
        self.act_prev.setChecked(bool(config.get("prerelease_opt_in")))
        self.act_prev.triggered.connect(self._toggle_prerelease)

        self.act_stable = QAction("回归稳定版", self)
        self.act_stable.triggered.connect(self._go_stable)

        self.act_rollback = QAction("回滚 DSH 到上一版本", self)
        self.act_rollback.triggered.connect(self._rollback)

        # --- 桌面壳自身 ---
        self.act_host_check = QAction("检查桌面壳更新…", self)
        self.act_host_check.triggered.connect(lambda: self._check_host_update(manual=True))
        # 开发运行时自更新不可用，直接不给点，避免"点了报错说不行"
        supported, _reason = selfupdate.self_update_supported()
        if not supported:
            self.act_host_check.setEnabled(False)
            self.act_host_check.setText("检查桌面壳更新…（仅打包版可用）")

        self.act_about = QAction("关于 DSH Launcher", self)
        self.act_about.triggered.connect(self._show_about)

        # 标题栏自适应：视觉偏好因人而异，给一个关掉的开关
        self.act_titlebar = QAction("标题栏跟随界面配色", self)
        self.act_titlebar.setCheckable(True)
        self.act_titlebar.setChecked(bool(config.get("adaptive_titlebar")))
        self.act_titlebar.setEnabled(dwm.available())
        if not dwm.available():
            self.act_titlebar.setText("标题栏跟随界面配色（系统不支持）")
        self.act_titlebar.triggered.connect(self._toggle_titlebar)

        # --- 系统 ---
        act_logs = QAction("打开日志目录", self)
        act_logs.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(config.log_dir())))

        self.act_quit = QAction("退出", self)
        self.act_quit.triggered.connect(self._quit)

        menu.addAction(act_show)
        menu.addAction(self.act_setup)
        menu.addSeparator()
        menu.addAction(self.act_check)
        menu.addAction(self.act_prev)
        menu.addAction(self.act_stable)
        menu.addAction(self.act_rollback)
        menu.addSeparator()
        menu.addAction(self.act_host_check)
        menu.addSeparator()
        menu.addAction(self.act_titlebar)
        menu.addAction(self.act_about)
        menu.addSeparator()
        menu.addAction(act_logs)
        menu.addAction(self.act_quit)

        self.tray.setContextMenu(menu)
        self.tray.setToolTip(f"{APP_TITLE}\n桌面壳 v{HOST_VERSION}")
        self.tray.activated.connect(
            lambda reason: self._show_main()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.messageClicked.connect(self._on_tray_message_clicked)
        self.tray.show()
        self._refresh_menu()

    def _refresh_menu(self):
        """按当前版本状态调整菜单可用性——不做无意义的可点项。"""
        info = self.update_info
        cur = self.handle.install.version if self.handle else None

        if info:
            self.act_stable.setEnabled(info.action == "downgrade")
            self.act_stable.setText(
                f"回归稳定版 {info.stable}" if info.action == "downgrade"
                else "已是稳定版")
        else:
            # 还没拿到通道信息时不知道该回到哪个版本，宁可不给点
            self.act_stable.setEnabled(False)
            self.act_stable.setText("回归稳定版")

        prev = config.get("previous_version")
        rollback_ok = bool(prev and cur and prev != cur)
        self.act_rollback.setEnabled(rollback_ok)
        self.act_rollback.setText(
            f"回滚 DSH 到 {prev}" if rollback_ok else "回滚 DSH 到上一版本")

        if self.host_release:
            self.act_host_check.setText(f"更新桌面壳到 {self.host_release.version}…")
        else:
            self.act_host_check.setText(
                "检查桌面壳更新…" if selfupdate.self_update_supported()[0]
                else "检查桌面壳更新…（仅打包版可用）")

    # ----------------------------------------------------------- 启动回调

    def _on_ready(self, handle):
        self.handle = handle
        host_log("服务就绪：%s | 端口 %s | 来源 %s"
                 % (handle.install.describe(), handle.port, handle.install.source))
        self._ensure_webview()
        self.view.loadFinished.connect(self._on_view_loaded)
        self.view.load(QUrl(handle.url))
        QTimer.singleShot(15000, lambda: self.stack.setCurrentIndex(1))
        self._refresh_menu()
        if config.get("check_update_on_start"):
            QTimer.singleShot(3000, lambda: self._check_update(manual=False))

    def _on_view_loaded(self, ok: bool):
        """页面加载完成回调。

        标题栏取色在这里触发，而**不是**等一个固定时长。
        早期实现是 `singleShot(4000, ...)` 死等 4 秒，老大反馈
        "加载完网页后有明显延迟"——正是这 4 秒。

        现在改成"立刻试一次 + 失败退避重试"：既然有了空白检测
        （见 dwm.FLAT_RATIO），就能区分"页面还没画出来"和"页面本身是纯色"，
        于是在页面真正渲染完成的那一刻就上色，通常 1 秒内完成。
        """
        if ok:
            self.stack.setCurrentIndex(1)
        if ok and config.get("adaptive_titlebar") and dwm.available():
            self._schedule_titlebar_probe(0)
            self._start_theme_watch()

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

    def _start_theme_watch(self):
        """按配置启动/停止主题跟踪。"""
        ms = int(config.get("theme_watch_ms") or 0)
        if ms > 0:
            ms = max(ms, _THEME_WATCH_MIN_MS)
        if not config.get("adaptive_titlebar") or ms <= 0:
            timer = getattr(self, "_theme_timer", None)
            if timer is not None:
                timer.stop()
            return
        timer = getattr(self, "_theme_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.timeout.connect(self._theme_watch_tick)
            self._theme_timer = timer
        # 已在跑就只保证节拍正确，不重启倒计时——避免频繁 start()
        # 反复重置计时器。
        if not timer.isActive() or timer.interval() != ms:
            timer.start(ms)
            host_log("主题跟踪：每 %d ms 检查一次（仅读计算样式，无截图）" % ms)

    def _theme_watch_tick(self):
        """跟踪 tick：只看 L1，且没变就什么都不做。"""
        if not config.get("adaptive_titlebar") or self.view is None:
            timer = getattr(self, "_theme_timer", None)
            if timer is not None:
                timer.stop()
            return
        # 最小化/隐藏时不看——省掉无谓的渲染进程往返
        if self.isMinimized() or not self.isVisible():
            return
        try:
            self.view.page().runJavaScript(_JS_THEME_PROBE,
                                           self._on_watch_color)
        except Exception:                                       # noqa: BLE001
            pass

    def _on_watch_color(self, value):
        """跟踪回调：与上次上色用的颜色比对，变了才动。"""
        rgb = _parse_probe_color(value)
        if rgb is None:
            return
        last = getattr(self, "_last_titlebar_rgb", None)
        if last is not None and all(
                abs(rgb[i] - last[i]) <= THEME_CHANGE_TOL for i in range(3)):
            return                       # 没变——大多数 tick 走这里
        host_log("标题栏：检测到界面配色变化 %s → %s"
                 % (dwm.to_hex(last) if last else "(无)", dwm.to_hex(rgb)))
        self._apply_titlebar(rgb)

    #: 标题栏取色的重试节奏（毫秒）。首次很快，之后逐步退避，
    #  累计约 7.7 秒 —— 足够覆盖冷启动的样式计算与首绘，
    #  而正常情况第一次就成功，用户感知不到等待。
    _TB_RETRY_MS = (250, 400, 600, 900, 1300, 1800, 2400)

    def _schedule_titlebar_probe(self, attempt: int):
        """安排第 attempt 次取色。"""
        if not config.get("adaptive_titlebar") or not dwm.available():
            return
        if attempt >= len(self._TB_RETRY_MS):
            host_log("标题栏取色：重试 %d 次仍未取到，放弃（保持系统默认）"
                     % attempt)
            return
        QTimer.singleShot(self._TB_RETRY_MS[attempt],
                          lambda a=attempt: self._sample_titlebar_color(a))

    def _on_failed(self, msg: str):
        host_log("启动失败：%s" % msg.replace("\n", " | ")[:600])
        # 启动失败是硬失败：托盘气泡容易错过，改走应用内提示
        self._set_tip("启动失败")
        self._warn("DSH 启动失败。\n\n" + msg)

    def _on_needs_setup(self, rep):
        """缺 Node / 缺 DSH：这不是"启动失败"，而是"还没准备好"，走引导。"""
        host_log("环境自检未通过：%s | broken=%s"
                 % (rep.detail or "(无说明)", rep.broken))
        self._set_tip("需要先准备运行环境")
        self.act_setup.setEnabled(True)
        self._show_main()
        dlg = OnboardingDialog(self, rep)
        dlg.exec()
        if dlg.succeeded:
            host_log("环境准备完成，重新启动服务")
            self.act_setup.setEnabled(False)
            self._set_tip("环境已就绪，正在启动…")
            self.stack.setCurrentIndex(0)
            self._launch_startup()
        else:
            self._set_tip("运行环境尚未就绪。可从托盘菜单「准备运行环境」重试。")
            self.tray.showMessage(
                "DSH", "运行环境尚未就绪。可从托盘菜单「准备运行环境」重试。",
                QSystemTrayIcon.MessageIcon.Information, 6000)

    def _prepare_env(self):
        self.stack.setCurrentIndex(0)
        self._set_tip("正在检查运行环境…")
        self._launch_startup()

    # ----------------------------------------------------------- 服务控制

    def _stop_service(self) -> int:
        """停止后台服务，返回终止的进程数。

        不再是菜单项（见 _quit 的说明），改为退出流程内部调用。
        保留成独立方法是为了让"停服务"和"退窗口"两件事在代码里仍然分开——
        将来若要恢复成两个入口，直接挂回菜单即可。
        """
        n = contract.stop(self.handle,
                          port=(self.handle.port if self.handle else
                                int(config.get("port") or 0)))
        if n:
            contract.save_state(None)
        return n

    # ----------------------------------------------------------- 用户可见反馈

    def _info(self, text: str):
        """应用内提示。

        手动操作的结果**必须**落在应用内。托盘气泡有两个致命问题：
        用户视线在鼠标位置而气泡在屏幕角落，容易错过；系统「通知」设置
        还可能直接把它拦掉。两条叠加，用户看到的就是"点了没反应"。
        """
        self._show_main()
        QMessageBox.information(self, "DSH", text)

    def _warn(self, text: str):
        self._show_main()
        QMessageBox.warning(self, "DSH", text)

    def _check_update(self, manual: bool):
        if getattr(self, "_checking", False):
            if manual:
                # 静默吞掉点击是最糟的反馈——明确告诉用户正在查
                self._info("正在检查更新，请稍候。")
            return
        self._checking = True
        if manual:
            self.act_check.setEnabled(False)
            self.act_check.setText("检查中…")
            host_log("手动检查更新")
        self._checker = CheckWorker()
        self._checker.found.connect(lambda info: self._on_check(info, manual))
        self._checker.failed.connect(lambda msg: self._on_check_failed(msg, manual))
        self._checker.done.connect(self._on_check_done)
        self._checker.start()

    def _on_check(self, info, manual: bool):
        self.update_info = info
        self._refresh_menu()
        if manual:
            # 手动检查：无论有没有更新，结果都要明确给出来
            if info.has_action:
                self._show_update_dialog(info.target)
            else:
                self._info("当前已是最新版本。\n\n"
                           "已安装　　　DSH %s\n"
                           "检查通道　　%s\n"
                           "预览通道最新　%s"
                           % (info.current,
                              updater.channel_label(info.channel),
                              info.alpha or "（未知）"))
            return
        if info.has_action:
            self.tray.showMessage("DSH 更新", info.summary() + "（点击查看）",
                                  QSystemTrayIcon.MessageIcon.Information, 10000)

    def _on_check_failed(self, msg: str, manual: bool):
        if manual:
            self._warn("检查更新失败。\n\n" + msg + "\n\n"
                       "常见原因：网络不可达、npm 源配置异常。\n"
                       "详细记录见日志目录下的 host.log。")

    def _on_check_done(self):
        self._checking = False
        self.act_check.setEnabled(True)
        self.act_check.setText("检查更新…")

    def _on_tray_message_clicked(self):
        if self.update_info and self.update_info.has_action:
            self._show_update_dialog(self.update_info.target)

    def _toggle_prerelease(self, checked: bool):
        config.set(prerelease_opt_in=bool(checked))
        self.act_prev.setChecked(bool(checked))
        host_log("预览计划 → %s" % ("开" if checked else "关"))
        # 切换通道后立刻给结果：走手动检查路径，所以会有应用内反馈
        self._check_update(manual=True)

    def _go_stable(self):
        target = self.update_info.stable if self.update_info else None
        if not target:
            self._info("还没有取到稳定版版本号。\n\n请先点「检查更新」，"
                       "拿到结果后再试「回归稳定版」。")
            return
        config.set(prerelease_opt_in=False)
        self.act_prev.setChecked(False)
        host_log("回归稳定版 %s" % target)
        self._show_update_dialog(target)

    def _rollback(self):
        prev = config.get("previous_version")
        if prev:
            self._show_update_dialog(prev)

    def _show_update_dialog(self, target: str):
        if not self.handle:
            return
        info = self.update_info
        if not info or info.target != target:
            info = updater.UpdateInfo(
                current=self.handle.install.version,
                stable=self.update_info.stable if self.update_info else target,
                alpha=self.update_info.alpha if self.update_info else None,
                target=target,
                channel="alpha" if "alpha" in target else "latest",
                action="upgrade" if target != self.handle.install.version else "none",
            )
        dlg = UpdateDialog(self, info, self.handle)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.new_handle:
            self.handle = dlg.new_handle
            self._ensure_webview()
            self.stack.setCurrentIndex(1)
            self.view.load(QUrl(self.handle.url))
            self.update_info = None
            self._refresh_menu()
            QTimer.singleShot(2000, lambda: self._check_update(manual=False))

    # ----------------------------------------------------------- 桌面壳更新

    def _check_host_update(self, manual: bool):
        """检查桌面壳自身更新。与 DSH 检查完全独立。"""
        if getattr(self, "_host_checking", False):
            if manual:
                self._info("正在检查桌面壳更新，请稍候。")
            return
        self._host_checking = True
        if manual:
            self.act_host_check.setEnabled(False)
            self.act_host_check.setText("检查中…")
            host_log("手动检查桌面壳更新")

        self._host_checker = HostCheckWorker()
        self._host_checker.found.connect(
            lambda rel, note: self._on_host_check(rel, note, manual))
        self._host_checker.failed.connect(
            lambda msg: self._on_host_check_failed(msg, manual))
        self._host_checker.done.connect(self._on_host_check_done)
        self._host_checker.start()

    def _on_host_check(self, rel, note: str, manual: bool):
        self.host_release = rel
        self._refresh_menu()
        if not manual:
            if rel:
                self.tray.showMessage(
                    "DSH Launcher 更新", f"桌面壳有新版本 v{rel.version}（点击查看）",
                    QSystemTrayIcon.MessageIcon.Information, 10000)
            return
        if not rel:
            self._info("桌面壳" + note)
            return
        self._show_host_update_dialog(rel)

    def _on_host_check_failed(self, msg: str, manual: bool):
        if getattr(self, "_host_checking", False) and not manual:
            return
        if manual:
            self._warn("检查桌面壳更新失败。\n\n" + msg + "\n\n"
                       "桌面壳更新从 GitHub 获取，网络不可达时属正常现象。\n"
                       "不影响 DSH 本身的更新（那条走 npm 通道）。")

    def _on_host_check_done(self):
        self._host_checking = False
        self.act_host_check.setEnabled(True)
        self._refresh_menu()

    def _show_host_update_dialog(self, rel):
        dlg = HostUpdateDialog(self, rel)
        result = dlg.exec()
        if result == HostUpdateDialog.RESTART_NOW:
            host_log("桌面壳更新：用户选择立即重启，退出进程交由替换脚本接管")
            # 托盘必须显式隐藏，否则图标会留到进程真正结束
            self.tray.hide()
            QApplication.quit()

    def _show_about(self):
        supported, reason = selfupdate.self_update_supported()
        text = (
            f"DSH Launcher  v{HOST_VERSION}\n\n"
            f"给 DeepSeek Harness 套的 Windows 桌面壳。\n"
            f"项目地址：https://github.com/{selfupdate.HOST_REPO}\n\n"
            f"桌面壳更新　{'可用' if supported else '不可用'}\n"
            f"　{('从 GitHub Release 拉取新版本并自动替换。' if supported else reason)}\n\n"
            f"DSH 更新　走官方 npm 通道，与桌面壳更新互不影响。\n"
        )
        QMessageBox.about(self, "关于", text)

    def _toggle_titlebar(self, checked: bool):
        """开关标题栏自适应配色。"""
        config.set(adaptive_titlebar=bool(checked))
        self.act_titlebar.setChecked(bool(checked))
        host_log("标题栏跟随界面配色 → %s" % ("开" if checked else "关"))
        if checked:
            if not dwm.available():
                self._info("当前系统不支持（需要 Windows 11）。")
                return
            # 立刻取一次，不然要等到下次启动才看到效果
            self._sample_titlebar_color()
        else:
            dwm.reset(self)
            # 标题栏交还系统了，图标也要跟着回到系统主题——
            # 否则深色界面配白图标、浅色系统标题栏配白图标，又是一片糊。
            self._set_theme_icon(dwm.system_uses_dark())
        # 开关关闭时 _start_theme_watch 内部会停掉定时器
        self._start_theme_watch()

    # ----------------------------------------------------------- 窗口行为

    def _show_main(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        """退出应用。

        退出时**连带停止后台服务**——这是重构后的默认行为。

        理由：「停止后台服务」与「退出」原本是两个菜单项，语义不同
        （一个停服务留窗口、一个关窗口留服务），但用户点它们时的意图
        几乎都是"我要结束这一切"。留两个入口只会让人犹豫点哪个。
        现在只保留「退出」，并且它做的是"把这件事完整结束掉"。

        代价：想在关掉窗口后继续跑 DSH 后台任务的人失去了入口。
        对桌面壳的目标用户（用图形界面干活的人）这个代价可以接受——
        真需要常驻服务的人本来就会用命令行。
        """
        host_log("用户退出，连带停止后台服务")
        try:
            n = self._stop_service()
            host_log("已停止后台服务（%d 个进程）" % n)
        except Exception as e:                                   # noqa: BLE001
            host_log("退出时停止服务失败：%s: %s" % (type(e).__name__, e))
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self.tray.showMessage("DSH", "应用已最小化到托盘，后台服务仍在运行",
                                  QSystemTrayIcon.MessageIcon.Information, 5000)
            self._tray_hint_shown = True


# ------------------------------------------------------------------ 单实例

def activate_existing() -> None:
    for w in QApplication.topLevelWidgets():
        if isinstance(w, MainWindow):
            w._show_main()


def main() -> int:
    sock = QLocalSocket()
    sock.connectToServer(SINGLETON_ID)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.close()
        return

    QLocalServer.removeServer(SINGLETON_ID)
    QApplication.setApplicationName("DSH-Web")
    QApplication.setOrganizationName("DSH")
    app = QApplication(sys.argv)

    # 打包产物资源自检：设了环境变量就只检查、写报告、退出，不进 GUI。
    # CI 构建完 exe 后立刻跑这个，用来兜住"包里没带 assets"这类问题。
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

    # 全局图标先跟系统主题；MainWindow 取到界面色后会再切一次
    _ic = icon_for(dwm.system_uses_dark())
    if not _ic.isNull():
        app.setWindowIcon(_ic)

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
    holder.append(win)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    # 必须 sys.exit(main())：main() 在资源自检失败时返回非 0，
    # 而裸调 main() 会把返回值丢掉 → 退出码恒为 0 → CI 拦不住坏产物。
    sys.exit(main())
