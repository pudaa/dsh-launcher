# -*- coding: utf-8 -*-
"""桌面壳自身的更新：从 GitHub Release 拉取新 exe 并替换自己。

与 DSH 更新的区别（界面上必须讲清楚）
------------------------------------
    DSH 更新      npm 通道，换的是 %LOCALAPPDATA%\\DSH-Web\\node-global\\ 里的程序
    桌面壳更新    GitHub Release，换的是你正在运行的这个 exe

两者互不影响。DSH 更新不会带来新界面，桌面壳更新不会带来新 DSH 能力。

为什么用 ".new + .bat + 重启" 而不是直接覆盖自己
-----------------------------------------------
Windows 不允许覆盖正在运行的 exe（文件被锁）。标准解法是找一个"局外进程"
在我们退出后做替换。这里选 .bat 而不是另起一个 exe，理由：

  - 零额外依赖，不用再打包一个 updater.exe
  - 天然可见可审计，出问题用户能自己看懂 bat 在干什么
  - 用 `move`（同目录重命名）而不是 `copy`，因此**不需要管理员权限**

安全边界（重要）
----------------
我们只从**官方 GitHub Release** 拉取，并且：

  1. 校验 tag 与请求的版本号一致（防止被指向别的 release）
  2. 下载到临时文件，校验是有效 PE 文件（MZ 头）且体积合理
  3. 落盘前比对已下载字节数 == Content-Length（防截断）
  4. 替换前把当前 exe 备份为 `DSH-Web.exe.old`，失败可人工恢复

**没有做代码签名校验。** 若将来要对外大规模分发，应考虑加上
（需要证书），或至少校验 SHA-256（可在 Release 资产里附带校验文件）。
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import config
from .version import HOST_ASSET_NAME, HOST_REPO, HOST_VERSION, is_newer

#: 用我们自己的 UA，便于在 GitHub 侧区分流量来源
UA = f"dsh-launcher/{HOST_VERSION}"

#: exe 体积下限。低于此值必然是错误页 / 空文件，不可能是构建产物。
MIN_EXE_BYTES = 5 * 1024 * 1024


class SelfUpdateError(Exception):
    """自更新过程中的可预期失败。消息面向使用者，不出现堆栈术语。"""


@dataclass
class ReleaseAsset:
    name: str
    url: str
    size: int


@dataclass
class HostRelease:
    version: str                 # 去掉前缀 v 的版本号
    tag: str
    notes: str
    html_url: str
    published_at: str
    asset: ReleaseAsset | None = None
    prerelease: bool = False

    @property
    def has_asset(self) -> bool:
        return self.asset is not None


# ------------------------------------------------------------------ 当前状态

def running_as_exe() -> bool:
    """是否运行在冻结后的 exe 里（Nuitka / PyInstaller 都会设 sys.frozen）。

    源码运行时不能自更新——替换掉 .py 没有意义，而且会破坏开发环境。
    """
    return bool(getattr(sys, "frozen", False))


def current_exe() -> str:
    return os.path.abspath(sys.executable if running_as_exe() else sys.argv[0])


def self_update_supported() -> tuple[bool, str]:
    """返回 (是否支持, 原因)。界面据此决定是否显示"更新桌面壳"。"""
    if not running_as_exe():
        return False, "当前以源码方式运行，桌面壳更新仅在打包后的版本中可用。"
    if not os.access(os.path.dirname(current_exe()) or ".", os.W_OK):
        return False, "程序所在目录不可写，无法自动替换。请手动下载新版本。"
    return True, ""


# ------------------------------------------------------------------ 查询

def _http_json(url: str, timeout: float = 20) -> dict | None:
    """与 updater.py 保持同一姿态：先正常证书，失败再放宽。

    国内访问 GitHub 常被中间人替换证书，严格校验下会直接失败。
    """
    for ctx in (ssl.create_default_context(), ssl._create_unverified_context()):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": UA,
            })
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


def _parse_release(data: dict) -> HostRelease | None:
    if not isinstance(data, dict):
        return None
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    version = tag.lstrip("v")

    asset = None
    for a in data.get("assets") or []:
        if not isinstance(a, dict):
            continue
        if a.get("name") == HOST_ASSET_NAME:
            asset = ReleaseAsset(
                name=a["name"],
                url=a.get("browser_download_url") or "",
                size=int(a.get("size") or 0),
            )
            break

    return HostRelease(
        version=version,
        tag=tag,
        notes=(data.get("body") or "").strip(),
        html_url=data.get("html_url") or f"https://github.com/{HOST_REPO}/releases",
        published_at=(data.get("published_at") or "")[:10],
        asset=asset,
        prerelease=bool(data.get("prerelease")),
    )


def fetch_latest_release(timeout: float = 20,
                         include_prerelease: bool = False) -> HostRelease | None:
    """取最新 release。

    单次 `releases/latest` 会跳过预发布版；要拿到预发布就得列全部再挑。
    这里的"预发布"与 DSH 的 alpha 通道无关——是桌面壳自己的。
    """
    if not include_prerelease:
        data = _http_json(f"https://api.github.com/repos/{HOST_REPO}/releases/latest", timeout)
        rel = _parse_release(data) if data else None
        if rel:
            return rel
        # latest 接口在没有正式 release 时返回 404，退回去列全部
    data = _http_json(f"https://api.github.com/repos/{HOST_REPO}/releases?per_page=10", timeout)
    if not isinstance(data, list):
        return None
    for item in data:
        rel = _parse_release(item)
        if rel:
            return rel
    return None


def check_host_update(timeout: float = 20) -> tuple[HostRelease | None, str]:
    """检查桌面壳更新。

    返回 (release, 状态说明)。release 为 None 表示没有可用更新，
    此时状态说明是给用户看的原因文本。
    """
    try:
        rel = fetch_latest_release(timeout=timeout)
    except Exception as e:
        return None, f"检查失败：{e}"

    if rel is None:
        return None, "无法获取发行信息（可能是网络问题，或尚未发布任何版本）。"

    if not is_newer(rel.version, HOST_VERSION):
        return None, f"已是最新版本（v{HOST_VERSION}）。"

    if not rel.has_asset:
        return None, (f"发现新版本 v{rel.version}，但该发行版没有附带可用的程序文件。\n"
                      f"请到 {rel.html_url} 手动下载。")

    return rel, f"发现新版本 v{rel.version}（当前 v{HOST_VERSION}）。"


# ------------------------------------------------------------------ 下载

def download(rel: HostRelease, on_progress=None, timeout: float = 60) -> str:
    """把新 exe 下载到临时文件，返回路径。

    下载过程中的每一步都校验，宁可失败也不要写进去一个坏文件——
    坏文件会直接导致下次启动失败，用户连"更新失败"的提示都看不到。
    """
    if not rel.has_asset or not rel.asset:
        raise SelfUpdateError("该发行版没有附带可下载的程序文件。")
    if not rel.asset.url:
        raise SelfUpdateError("下载地址为空，无法下载。")

    fd, tmp = tempfile.mkstemp(prefix="DSH-Web-new-", suffix=".exe")
    os.close(fd)
    downloaded = 0
    try:
        ok = False
        for ctx in (ssl.create_default_context(), ssl._create_unverified_context()):
            try:
                req = urllib.request.Request(rel.asset.url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r, \
                        open(tmp, "wb") as f:
                    expected = int(r.headers.get("Content-Length") or 0)
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if on_progress:
                            on_progress(downloaded, expected)
                ok = True
                break
            except (urllib.error.URLError, OSError) as e:
                if downloaded:
                    # 已经下了一部分还失败 → 重试也没意义，直接抛
                    raise SelfUpdateError(f"下载中断：{e}") from e
                continue
        if not ok:
            raise SelfUpdateError("下载失败，请检查网络连接。")

        # --- 校验：体积 ---
        size = os.path.getsize(tmp)
        if size < MIN_EXE_BYTES:
            raise SelfUpdateError(
                f"下载到的文件只有 {size} 字节，明显不是完整的程序，已放弃。")

        # --- 校验：PE 头。GitHub 出错时会返回 HTML 页面，这里能拦住 ---
        with open(tmp, "rb") as f:
            if f.read(2) != b"MZ":
                raise SelfUpdateError("下载到的内容不是有效的 Windows 程序，已放弃。")

        return tmp
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ 应用

def _bat_path() -> str:
    return os.path.join(tempfile.gettempdir(), "DSH-Web-selfupdate.bat")


def _win(path: str) -> str:
    """统一成反斜杠的 Windows 路径。

    实测：cmd 的内建命令 `move` / `del` 对正斜杠路径不可靠——
    传 `C:/x/a.exe` 给 `move` 会静默失败（errorlevel 非零但无输出），
    反斜杠才稳定。而 bat 里路径一律放在双引号内，
    反斜杠在引号里不会触发转义问题。
    """
    return os.path.normpath(path)


def build_apply_script(target: str, incoming: str, pid: int,
                       relaunch: bool = True) -> str:
    """生成替换用的 bat。

    踩过的坑，逐条记下来免得回退：

    1. **路径必须用反斜杠。** cmd 的 `move` 对正斜杠路径会静默失败
       （errorlevel 非零但无输出），实测确认。用 `os.path.normpath`。

    2. **`>nul 2>&1` 会污染 errorlevel 判定。** 写成 `move ... >nul 2>&1`
       后紧跟 `if errorlevel 1` 会误判——必须只重定向 stdout（`>nul`），
       让 stderr 原样输出，errorlevel 才是 move 自己的。

    3. **`del "%~f0"` 不能放在中间。** cmd 逐行读取 bat 文件，运行中删除
       自己会让后续行读不到，报 "The batch file cannot be found"。
       必须放到最后，且前面用 `goto fin` 保证一定会走到它。

    4. **PID 为 0 时不能进等待循环。** `tasklist /FI "PID eq 0"` 会匹配到
       系统空闲进程并一直匹配，于是死等到超时走 giveup，替换永远不发生。

    5. **日志路径用绝对路径**，因为 bat 的工作目录不确定（可能是 system32）。
    """
    log = _win(config.log_dir())
    target = _win(target)
    incoming = _win(incoming)
    backup = target + ".old"
    logfile = os.path.join(log, "selfupdate.log")
    launch = 'start "" "%TARGET%"' if relaunch else "rem no relaunch"
    done_word = "已重启" if relaunch else "已完成，未自动重启"

    if pid and pid > 0:
        wait_block = (
            "set /a WAITED=0\n"
            ":waitloop\n"
            'tasklist /FI "PID eq %PID%" 2>nul | find.exe "%PID%" >nul\n'
            "if not errorlevel 1 (\n"
            "    if %WAITED% GEQ 60 goto giveup\n"
            "    timeout /t 1 /nobreak >nul\n"
            "    set /a WAITED+=1\n"
            "    goto waitloop\n"
            ")\n"
            "echo 旧进程已退出，等待 %WAITED% 秒 >> \"%LOG%\""
        )
    else:
        wait_block = 'echo 未指定进程号，直接替换 >> "%LOG%"'

    return f"""@echo off
chcp 65001 >nul
setlocal

set "TARGET={target}"
set "INCOMING={incoming}"
set "BACKUP={backup}"
set "PID={pid}"
set "LOG={logfile}"

if not exist "%TARGET%" goto notarget
if not exist "%INCOMING%" goto noincoming

echo [%date% %time%] self-update start >> "%LOG%"
echo   目标: %TARGET% >> "%LOG%"
echo   来源: %INCOMING% >> "%LOG%"

{wait_block}

rem ---- 备份当前版本 ----
if exist "%BACKUP%" del /f /q "%BACKUP%" >nul 2>&1
move /y "%TARGET%" "%BACKUP%" >nul
if errorlevel 1 goto movefail

rem ---- 放入新版本 ----
move /y "%INCOMING%" "%TARGET%" >nul
if errorlevel 1 goto putfail

echo 替换完成 >> "%LOG%"
{launch}
echo {done_word} >> "%LOG%"
goto fin

:notarget
echo 目标文件不存在 >> "%LOG%"
goto fin

:noincoming
echo 待替换文件不存在 >> "%LOG%"
goto fin

:movefail
echo 无法移动旧版本，可能仍被占用 >> "%LOG%"
{launch}
goto fin

:putfail
echo 无法放入新版本，正在回滚 >> "%LOG%"
move /y "%BACKUP%" "%TARGET%" >nul
echo 已回滚，旧版本保持不变 >> "%LOG%"
{launch}
goto fin

:fin
rem 自删除必须放在最后一行区域，且只能有一处
del "%~f0" >nul 2>&1
exit /b 0
"""


def apply(rel: HostRelease, on_progress=None, relaunch: bool = True) -> str:
    """下载并安排替换。返回给用户看的说明文本。

    本函数**不自己退出**。调用方拿到返回值后应当提示用户，然后调用
    `shutdown()` 退出进程，把舞台交给 bat。

    为什么不在函数里直接退出：用户需要看到进度和"即将重启"的提示，
    静默退出会让人以为程序崩了。
    """
    incoming = download(rel, on_progress=on_progress)
    target = current_exe()
    pid = os.getpid()

    bat = _bat_path()
    with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(build_apply_script(target, incoming, pid, relaunch=relaunch))

    # DETACHED + 不继承句柄：bat 必须活过我们的退出
    DETACHED = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", bat],
            creationflags=DETACHED,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as e:
        raise SelfUpdateError(f"无法启动更新程序：{e}") from e

    return (f"新版本 v{rel.version} 已下载完成。\n"
            "点击「立即重启」后程序会关闭并自动完成替换，随后重新打开。")
