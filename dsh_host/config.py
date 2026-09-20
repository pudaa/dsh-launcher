# -*- coding: utf-8 -*-
"""桌面壳自有配置与路径规划。

目录规划（**唯一权威定义处**）
------------------------------
    %LOCALAPPDATA%\\DSH-Web\\             桌面壳数据根——只归我们，DSH 与打包工具都不碰
        settings.json                     宿主配置
        compat.json                       适配表外部覆盖（可手改，改完即生效）
        install.json                      安装定位缓存
        service.json                      服务运行时状态
        logs\\                             日志
            dsh-web.log                   DSH 服务 stdout/stderr
            host.log                      桌面壳自身日志
        profile\\                          QtWebEngine 持久 profile（登录态）
        node-global\\                     首次运行引导安装的 DSH（私有 npm prefix）

    %LOCALAPPDATA%\\DSH-Web-runtime\\      Nuitka onefile 解包目录——纯缓存，可随时删

两者必须严格分家：解包目录按设计就是"排障时可随手清掉"的缓存，
一旦和登录态、配置混放，清缓存就会连带丢数据。
打包参数里的 `--onefile-tempdir-spec` 必须指向 RUNTIME_DIR_NAME。

其余边界
--------
- 桌面壳数据**永不写进 DSH_HOME**（那是 DSH 的地盘）
- 所有路径只在本模块定义，其余模块一律 import，不得自行拼 LOCALAPPDATA
"""
from __future__ import annotations

import json
import os
import threading

APP_NAME = "DSH-Web"
RUNTIME_DIR_NAME = "DSH-Web-runtime"


# ------------------------------------------------------------------ 路径定义

def _local_appdata() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")


def data_root() -> str:
    """桌面壳数据根。目录不存在则创建。"""
    d = os.path.join(_local_appdata(), APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def runtime_dir() -> str:
    """Nuitka onefile 解包目录。不创建——由打包产物在运行时自行建立。

    这里定义出来是为了让诊断信息有据可依，也让"清缓存"有明确边界：
    删这个目录永远安全；删 data_root() 会丢配置和登录态。
    """
    return os.path.join(_local_appdata(), RUNTIME_DIR_NAME)


def settings_path() -> str:
    return os.path.join(data_root(), "settings.json")


def compat_path() -> str:
    return os.path.join(data_root(), "compat.json")


def install_cache_path() -> str:
    return os.path.join(data_root(), "install.json")


def service_state_path() -> str:
    return os.path.join(data_root(), "service.json")


def log_dir() -> str:
    d = os.path.join(data_root(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def service_log() -> str:
    """DSH 服务的 stdout/stderr 落这里——是我们自己的文件，不是 DSH 的私有日志。"""
    return os.path.join(log_dir(), "dsh-web.log")


def host_log() -> str:
    """桌面壳自身的运行日志（启动、探测命中路径、更新事务）。"""
    return os.path.join(log_dir(), "host.log")


def profile_dir() -> str:
    """QtWebEngine 持久 profile。登录态靠它跨次保留。"""
    d = os.path.join(data_root(), "profile")
    os.makedirs(d, exist_ok=True)
    return d


def node_global_dir() -> str:
    """我们私有管理的 DSH 安装位置（首次运行引导装到这里）。

    为什么是私有 prefix 而不是 npm 默认全局位置：
      Windows 上 npm 的 globalPrefix = dirname(node.exe)（见 @npmcli/config 源码），
      而官方 MSI 装的 Node 位于 C:\\Program Files\\nodejs —— 往那里装需要管理员权限。
      引导流程必须免管理员才能对普通用户可用，所以由我们自己指定一个用户可写的 prefix。

    副作用：这里的 dsh 不会出现在用户 PATH 上（终端里直接敲 dsh 用不了）。
    这是刻意的取舍——桌面壳不需要 PATH，而免权限才是分发的前提。
    """
    d = os.path.join(data_root(), "node-global")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------------ 配置读写

# 默认值即"出厂设置"。用户改动写入 settings.json，缺省键在这里兜底。
DEFAULTS: dict = {
    # 服务端口：3080 优先，被占用且允许降级时改用 --port 0 由系统分配
    "port": 3080,
    "port_auto_fallback": True,
    # 「加入预览计划」：开启后才跟 alpha 通道
    "prerelease_opt_in": False,
    # 启动时静默检查更新（只检查与提示，安装永远需要手动确认）
    "check_update_on_start": True,
    # 手工覆盖项（留空表示自动探测）
    "dsh_node": None,
    "dsh_bin": None,
    "dsh_home": None,
    # 更新器写入的状态：用于一键回滚
    "last_good_version": None,
    "previous_version": None,
    # 标题栏：读界面主色调后自动上色，让标题栏与界面衔接自然
    "adaptive_titlebar": True,
    # 取到的主色往中性灰拉多少（0=原色，1=全灰）。太饱和会抢视线
    "titlebar_mute": 0.35,
}

_lock = threading.RLock()
_cache: dict | None = None


def load(force: bool = False) -> dict:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        cfg = dict(DEFAULTS)
        try:
            with open(settings_path(), "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k, v in disk.items():
                    cfg[k] = v
        except (OSError, ValueError):
            # 配置损坏时退回默认值，不阻断启动
            pass
        _cache = cfg
        return _cache


def save() -> None:
    with _lock:
        cfg = load()
        tmp = settings_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, settings_path())
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass


def get(key: str, default=None):
    return load().get(key, DEFAULTS.get(key, default))


def set(**kw) -> None:
    """批量写入并落盘。set(prerelease_opt_in=True)"""
    with _lock:
        cfg = load()
        cfg.update(kw)
        save()
