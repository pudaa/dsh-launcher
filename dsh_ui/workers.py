# -*- coding: utf-8 -*-
"""后台任务：凡是"会阻塞界面"的活都放在这里，跑在 QThread 上。

为什么单独成文件（2026-11-12 拆分）
----------------------------------
原先是 `dsh_gui_qt.py` 里的十几个类，把主窗口挤到了 2000 行以下的位置。
这些东西的共同点是"只做一件事然后发信号"，跟窗口本身没有耦合——
留在同一个文件里只会让人误以为它们和 MainWindow 有依赖。

约定
----
- 每个 worker 只发信号，不碰界面对象（跨线程改 widget 是崩溃的经典来源）；
- 错误一律**分类往上抛**（`RemoteError.kind` 那样），不要压成一句"失败了"——
  "认证没过"和"远端没装 dsh"对用户是两回事。
"""

from __future__ import annotations
import time

from PySide6.QtCore import QThread, Signal

from dsh_host import (config, contract, lifecycle, provision, remote, selfupdate,
                      storage, updater)
from dsh_host.contract import DshError
from dsh_host.logutil import host_log
from dsh_host.version import HOST_NAME


class RemoteConnectWorker(QThread):
    """在后台建立一条远程会话（SSH + 可能要在远端起 dsh web + 建隧道）。

    必须后台：远端冷启动实测十几秒，上游再慢一点就是半分钟。界面线程里做，
    窗口会白纱、面板按钮点不动。

    失败的分类原样往上抛（`remote.RemoteError.kind`）——"认证没过"和
    "远端没装 dsh"对用户是完全不同的两件事，不能压成一句"连接失败"。
    """
    stage = Signal(str)
    ready = Signal(object, str)      # (RemoteSession, 带令牌的本地地址)
    failed = Signal(str, str)        # (kind, 可读消息)

    def __init__(self, host_obj, parent=None):
        super().__init__(parent)
        self.host_obj = host_obj
        self.session = None

    def run(self):
        try:
            self.session = remote.RemoteSession(self.host_obj)
            url = self.session.connect(on_stage=self.stage.emit)
            self.ready.emit(self.session, url)
        except remote.RemoteError as e:
            self.failed.emit(e.kind, str(e))
        except Exception as e:                                   # noqa: BLE001
            self.failed.emit("other", "%s: %s" % (type(e).__name__, e))

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
        handle = None
        try:
            # 先做环境自检：缺 Node / 缺 DSH 时不是"启动失败"，而是"还没准备好"，
            # 这两件事对用户的意义完全不同，走不同的界面
            # verify=False：不在这里等 `dsh --version`，采信缓存里的版本号。
            # 启动完成后由 VersionReverifyWorker 在后台补一次真实核对。
            self.progress.emit("正在检查运行环境…")
            rep = contract.diagnose(verify=False)
            if not rep.ready:
                self.needs_setup.emit(rep)
                return

            inst = rep.install
            self.progress.emit(inst.describe())

            mode, port = lifecycle.plan_service(inst, self.requested_port,
                                      self.allow_fallback, self.logf)
            if mode == "attach":
                self.ready.emit(lifecycle.attach_handle(inst, port, self.logf))
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
            self._abort(handle)
            self.failed.emit(str(e))
        except Exception as e:                                   # noqa: BLE001
            self._abort(handle)
            self.failed.emit(f"{type(e).__name__}: {e}")

    @staticmethod
    def _abort(handle) -> None:
        """启动链路失败时，把**已经拉起来的**进程收掉。

        没有这一步的话，"就绪超时"/"端口回退后起不来"会留下一个没人管的
        node.exe：占着端口、吃 300+ MB，而界面已经显示失败。用户看到的是
        "服务明明在跑，可就是连不上"——比直接失败更难查（2026-10-20 体检）。
        """
        if handle is None:
            return
        try:
            contract.stop(handle, port=handle.port)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            contract.save_state(None)
        except Exception:                                        # noqa: BLE001
            pass

class VersionReverifyWorker(QThread):
    """启动完成后，在后台复验 DSH 版本。

    为什么挪到后台
    --------------
    `resolve_install()` 命中缓存时默认仍会 spawn 一次 `dsh --version` 复验，
    实测 375 ms（占我方可控启动开销的 91%，见 tools/startup_profile.py），
    而且它挡在"启动服务"之前——启动路径为此白等一个进程。

    但复验结果**不是第一时间必须的**：用户完全可以先进界面，之后再看到提示。
    所以启动路径走 `verify=False`（采信缓存里的版本号），复验交给这里。

    这里用 `force=True` 做一次全量重探：既复验版本，也顺手刷新缓存，
    代价约 0.5 s，全部发生在后台线程里。
    """
    changed = Signal(object)               # 新的 DshInstall（版本与界面显示不一致时）

    def __init__(self, shown_version: str):
        super().__init__()
        self.shown_version = shown_version

    def run(self):
        try:
            fresh = contract.resolve_install(force=True)
        except Exception as e:                                   # noqa: BLE001
            host_log("版本复验失败（不影响使用）：%s: %s" % (type(e).__name__, e))
            return
        # 把结果对象整个带回去。**不要在界面线程里再 resolve 一次**——
        # 那等于把刚挪走的阻塞又搬回来，等于白改。
        if fresh.version and fresh.version != self.shown_version:
            self.changed.emit(fresh)

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
            host_log(f"检查{HOST_NAME}更新：{note}")
            self.found.emit(rel, note)
        except Exception as e:                                   # noqa: BLE001
            host_log(f"检查{HOST_NAME}更新失败：{type(e).__name__}: {e}")
            self.failed.emit("%s: %s" % (type(e).__name__, e))
        finally:
            self.done.emit()

class HostUpdateWorker(QThread):
    """下载桌面壳新版本，并**在本进程内**完成替换。

    为什么不再交给 bat（2026-10-01 改）
    ----------------------------------
    老大的反馈："点了重启之后到新窗口出现之间是个黑箱，失败也看不见。"
    bat 那条路必须先让应用退出，于是替换期间屏幕上什么都没有。

    而 Windows **允许重命名正在运行的 exe**（只改目录项，不动文件内容），
    也允许在原路径新建文件——实测确认。所以替换可以当场做完，
    全程界面还在，进度和失败都看得见。download 阶段本来就是网络耗时，
    进度主要花在那里。

    替换完成后**不重启**：重启由用户在对话框里点，见 `_relaunch_after_update`。
    """
    line = Signal(str)
    done = Signal(bool, str)

    def __init__(self, rel):
        super().__init__()
        self.rel = rel

    def run(self):
        try:
            # 进度里带速度与剩余时间：单连接时那个速度会低到让人以为死机，
            # 给出"还要多久"比只报百分比有用得多。
            window = [time.monotonic(), 0, 0.0]     # 起点时刻 / 起点字节 / 速度

            def on_progress(got: int, total: int):
                now = time.monotonic()
                dt = now - window[0]
                if dt >= 0.9:                       # ≥0.9 秒才重算，免得数字抖
                    window[2] = (got - window[1]) / dt
                    window[0], window[1] = now, got
                speed = window[2]
                rate = ("  %.1f MB/s" % (speed / 1048576)) if speed > 0 else ""
                if total <= 0:
                    self.line.emit("已下载 %.1f MB%s" % (got / 1048576, rate))
                    return
                line = ("已下载 %.1f / %.1f MB  (%d%%)%s"
                        % (got / 1048576, total / 1048576,
                           got * 100 // total, rate))
                if speed > 4096 and got < total:
                    # 低于 4 KB/s 时算出来的剩余时间没有意义，不如不显示
                    line += "  剩余约 %s" % _human_left((total - got) / speed)
                self.line.emit(line)

            info = selfupdate.install_inplace(self.rel, on_progress=on_progress)
            self.done.emit(True,
                           "v%s 已安装到位。\n"
                           "重启后会运行新版本——重启本身是瞬时的，"
                           "不需要等待文件替换。" % info.get("version", ""))
        except selfupdate.SelfUpdateError as e:
            host_log(f"{HOST_NAME}更新失败：{e}")
            self.done.emit(False, str(e))
        except Exception as e:                                   # noqa: BLE001
            host_log(f"{HOST_NAME}更新异常：{type(e).__name__}: {e}")
            self.done.emit(False, "%s: %s" % (type(e).__name__, e))

class UpdateWorker(QThread):
    line = Signal(str)
    done = Signal(bool, str, object)

    def __init__(self, target: str, handle, restart_port: int, logf: str,
                 fresh: bool = True):
        super().__init__()
        self.target = target
        self.handle = handle
        self.restart_port = restart_port
        self.logf = logf
        #: 是否先卸载旧包。更新与重装都该为 True——半升级就是"没卸就装"留下的。
        self.fresh = fresh

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
                                           start_service, on_line=self.line.emit,
                                           fresh=self.fresh)
            self.done.emit(ok, detail, box.get("handle"))
        except Exception as e:                                   # noqa: BLE001
            self.done.emit(False, f"{type(e).__name__}: {e}", box.get("handle"))

class DiagnoseWorker(QThread):
    done = Signal(object)

    def run(self):
        try:
            self.done.emit(contract.diagnose())
        except Exception:                                        # noqa: BLE001
            self.done.emit(contract.EnvironmentReport(detail="环境检测失败。"))

class ServiceSwapWorker(QThread):
    """换完配置（来源 / 数据目录）后，按新配置把后台服务拉起来。

    为什么单独一个 worker：切换来源要重新 `resolve_install`（偏好变了），
    迁移数据目录之后连日志路径都变了——旧 handle 整体失效，只能停掉重来。
    """
    done = Signal(bool, str, object)

    def __init__(self, old_handle, logf):
        super().__init__()
        self.old_handle = old_handle
        self.logf = logf

    def run(self):
        try:
            if self.old_handle is not None:
                contract.stop(self.old_handle,
                              port=(self.old_handle.port or None))
                contract.save_state(None)
            inst = contract.resolve_install(force=True)
            h = contract.launch(inst, int(config.get("port") or 0),
                                self.logf, cwd=config.log_dir())
            contract.wait_ready(h)
            contract.save_state(h)
            self.done.emit(True, h.url, h)
        except Exception as e:                                 # noqa: BLE001
            self.done.emit(False, "%s: %s" % (type(e).__name__, e), None)

class DataTaskWorker(QThread):
    """数据目录的重活：迁移 / 清缓存 / 清空 / 复制私有安装。

    为什么要单独一个线程（2026-10-21，老大第二次提"同类卡顿"）
    ----------------------------------------------------------
    `copytree` / `rmtree` 在 727 MB、6.6 万个文件上是十几秒到几十秒的纯 IO。
    以前它们直接跑在 GUI 线程里：窗口不重绘、Windows 还会盖一层"未响应"白纱，
    用户以为程序死了——而它只是在数盘。

    这里只做 IO，**不碰任何 QWidget**：所有界面更新都靠信号回到主线程
    （Qt 的 AutoConnection 会自动排队）。

    `done(ok, detail, payload)`：payload 是 dict，避免为了四种任务各定一套签名。
    """

    line = Signal(str)                   # 一行进展文案
    progress = Signal(int, int, str)     # (已完成, 总数, 当前文件名)；总数=0 → 不确定
    done = Signal(bool, str, object)

    def __init__(self, task: str, parent=None, **kw):
        super().__init__(parent)
        self.task = task
        self.kw = kw

    def _prog(self, done, total, name: str = ""):
        self.progress.emit(int(done), int(total), str(name or ""))

    def run(self):
        try:
            if self.task == "migrate":
                old = self.kw["old"]
                new_root = storage.migrate(self.kw["target"],
                                           on_line=self.line.emit,
                                           on_progress=self._prog)
                # 删旧目录同样是几十秒的活，一并留在这个线程里——否则"迁移完了
                # 但窗口又卡住十秒"，用户会以为迁移失败。
                self.line.emit("清理旧目录（%s）…" % old)
                cleaned = storage.remove_old(old, on_progress=self._prog)
                if not cleaned:
                    self.line.emit("旧目录没能删干净，可手动清理：%s" % old)
                self.done.emit(True, new_root, {"root": new_root,
                                                "cleaned": cleaned})
            elif self.task == "clear_cache":
                n, b = storage.clear_cache(on_progress=self._prog,
                                           on_line=self.line.emit)
                self.done.emit(True, "", {"files": n, "bytes": b})
            elif self.task == "clear_all":
                n, b = storage.clear_all(on_progress=self._prog)
                self.done.emit(True, "", {"files": n, "bytes": b})
            elif self.task == "private":
                ok, detail = provision.import_private(
                    self.kw["install"], on_line=self.line.emit,
                    on_progress=self._prog)
                self.done.emit(bool(ok), detail,
                               {"version": detail if ok else "",
                                "error": "" if ok else detail})
            else:
                self.done.emit(False, "未知的数据任务：%s" % self.task,
                               {"error": "未知的数据任务"})
        except storage.StorageError as e:
            self.done.emit(False, str(e), {"error": str(e)})
        except Exception as e:                                 # noqa: BLE001
            msg = "%s: %s" % (type(e).__name__, e)
            self.done.emit(False, msg, {"error": msg})


def _human_left(seconds: float) -> str:
    """把"还要多久"写成人话。

    显示剩余时间不是装饰：实测从 GitHub 单线程下一个 100 MB 的包只有
    40 KB/s，不给时间预期的话，用户只会以为卡死了。
    """
    seconds = max(0.0, seconds)
    if seconds < 90:
        return "%d 秒" % max(1, int(round(seconds)))
    minutes = seconds / 60.0
    if minutes < 60:
        return "%d 分钟" % int(round(minutes))
    hours = int(minutes // 60)
    return "%d 小时 %d 分" % (hours, int(round(minutes - hours * 60)))
