# -*- coding: utf-8 -*-
"""L2 环境供给：首次运行时把缺的东西补齐。

只在"用户拿到 exe 但机器上什么都没有"时登场。三条原则：

1. **只走官方渠道**
   Node.js  → winget 的 OpenJS.NodeJS.LTS（微软官方包管理器 + Node 官方发布方）
   DSH      → npm install -g（与更新器同一条通道）

2. **不设版本下限**
   DSH 的 package.json 里没有 `engines` 字段，我们就不替它发明要求。
   装最新 LTS、然后实跑 `dsh --version` 验证——能跑就认。

3. **免管理员**
   npm 的 globalPrefix 在 Windows 上等于 `dirname(node.exe)`（见 @npmcli/config 源码），
   官方 MSI 装的 Node 位于 C:\\Program Files\\nodejs，往那儿装全局包需要管理员。
   所以我们用 `--prefix` 指定一个用户可写的私有目录（见 config.node_global_dir）。

已知代价（明说，不藏）
----------------------
- 装 Node 会弹一次 UAC（MSI 作用域是全机）——实测未验证，属推断
- 首次安装 DSH 实测：518 个包、约 13 分钟、259MB。界面必须给进度，
  否则用户会以为卡死而中途关掉，留下半个 node_modules
"""
from __future__ import annotations

import os
import shutil

from . import config, contract

# winget 里 Node.js 的官方标识（已实测：OpenJS.NodeJS.LTS → 24.19.0）
NODE_WINGET_ID = "OpenJS.NodeJS.LTS"
NODE_WEBSITE = "https://nodejs.org/zh-cn"

# 首次安装走稳定通道——引导流程不该默认把人带到预发布上
DEFAULT_TAG = "latest"


# ------------------------------------------------------------------ winget

def find_winget() -> str | None:
    p = shutil.which("winget")
    if p:
        return p
    for raw in (r"%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe",
                r"%ProgramFiles%\WindowsApps\winget.exe"):
        cand = os.path.expandvars(raw)
        if os.path.isfile(cand):
            return cand
    return None


def winget_available() -> bool:
    return find_winget() is not None


# ------------------------------------------------------------------ PATH 刷新

def refresh_path_from_registry() -> list[str]:
    """把注册表里的 PATH（用户级 + 机器级）补进当前进程。

    刚装完 Node 时，当前进程看不到新目录——环境变量变更不向已运行的进程传播。
    不这么做就得要求用户重启程序，体验上不可接受。
    读取注册表不需要管理员权限。
    """
    try:
        import winreg
    except ImportError:
        return []

    blocks: list[str] = []
    for root, sub in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(root, sub) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
                if isinstance(value, str):
                    blocks.append(os.path.expandvars(value))
        except OSError:
            continue

    current = os.environ.get("PATH", "")
    seen = {p.strip().strip('"').rstrip("\\").lower()
            for p in current.split(os.pathsep) if p.strip()}
    added: list[str] = []
    for block in blocks:
        for part in block.split(os.pathsep):
            part = part.strip().strip('"')
            if not part or part.rstrip("\\").lower() in seen:
                continue
            seen.add(part.rstrip("\\").lower())
            current = current + os.pathsep + part if current else part
            added.append(part)
    if added:
        os.environ["PATH"] = current
    return added


# ------------------------------------------------------------------ 安装动作

def install_node_via_winget(on_line=None) -> tuple[bool, str]:
    wg = find_winget()
    if not wg:
        return False, ("未找到 winget（Windows 包管理器），无法自动安装 Node.js。\n"
                       f"请手动前往 {NODE_WEBSITE} 下载安装后点「重新检测」。")

    args = [wg, "install", "--id", NODE_WINGET_ID, "--exact",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity", "--silent"]
    if on_line:
        on_line("$ winget install --id %s" % NODE_WINGET_ID)
        on_line("提示：Windows 可能弹出权限确认，请点「是」。")

    rc, out, err = contract.run_capture(args, timeout=1800)
    text = ((out or "") + "\n" + (err or "")).strip()
    if on_line and text:
        for line in text.splitlines()[-30:]:
            on_line(line)

    if rc != 0:
        return False, ("Node.js 安装未成功（winget 退出码 %s）。\n"
                       "常见原因：权限确认被取消、网络不可达。\n"
                       f"也可以手动前往 {NODE_WEBSITE} 安装后点「重新检测」。\n"
                       % rc) + text[-800:]
    return True, "Node.js 安装完成"


def install_dsh(on_line=None, tag: str = DEFAULT_TAG) -> tuple[bool, str]:
    """用官方 npm 通道把 DSH 装进我们的私有 prefix（免管理员）。"""
    node = contract.find_node()
    if not node:
        return False, "未找到 Node.js，无法安装 DeepSeek Harness。"

    npm_cli = contract.find_npm_cli(node)
    if not npm_cli:
        return False, "未找到 npm，Node.js 安装可能不完整，建议重新安装 Node.js。"

    prefix = config.node_global_dir()
    args = [node, npm_cli, "install", "-g", "--prefix", prefix,
            f"@deepseek-ai/dsh@{tag}", "--no-fund", "--no-audit", "--loglevel=http"]
    if on_line:
        on_line("$ npm install -g --prefix <私有目录> @deepseek-ai/dsh@%s" % tag)
        on_line("首次安装需要下载 500+ 个包，大约十几分钟，可以先去忙别的。")

    rc, out, err = contract.run_capture(args, timeout=3600)
    text = ((out or "") + "\n" + (err or "")).strip()
    if on_line and text:
        for line in text.splitlines()[-40:]:
            on_line(line)

    if rc != 0:
        return False, "DeepSeek Harness 安装失败（npm 退出码 %s）。\n%s" % (rc, text[-1200:])
    return True, "DeepSeek Harness 安装完成"


# ------------------------------------------------------------------ 编排

PHASE_NODE = "node"
PHASE_DSH = "dsh"
PHASE_DONE = "done"


def provision_all(on_line=None, on_phase=None) -> tuple[bool, str]:
    """按需补齐环境。幂等：已就绪的步骤直接跳过，可反复调用。

    每个阶段结束后都**重新诊断**而不是假定成功——winget 说装好了不代表
    当前进程能看见它，npm 说装好了不代表能跑起来。
    """
    def say(msg):
        if on_line:
            on_line(msg)

    def phase(key, text):
        if on_phase:
            on_phase(key, text)

    phase("check", "正在检查运行环境…")
    rep = contract.diagnose()
    if rep.ready:
        return True, f"环境已就绪：DSH {rep.install.version}"

    if rep.need_node:
        phase(PHASE_NODE, "正在安装 Node.js 运行环境（可能需要同意权限确认）…")
        say("未检测到 Node.js，开始安装。")
        ok, msg = install_node_via_winget(say)
        if not ok:
            return False, msg
        added = refresh_path_from_registry()
        if added:
            say("已刷新环境变量，新增 %d 个路径。" % len(added))
        rep = contract.diagnose()
        if rep.need_node:
            return False, ("Node.js 安装完成，但当前程序仍检测不到它。\n"
                           "请关闭本程序后重新打开；或手动安装后点「重新检测」。")
        say("Node.js 已可用：%s" % (rep.node_version or "版本未知"))

    if rep.need_dsh:
        phase(PHASE_DSH, "正在安装 DeepSeek Harness（约十几分钟，请勿关闭窗口）…")
        say("未检测到 DeepSeek Harness，开始安装。")
        ok, msg = install_dsh(say)
        if not ok:
            return False, msg
        rep = contract.diagnose()
        if not rep.ready:
            return False, ("安装已完成但校验未通过。\n" + (rep.detail or "未知原因"))

    phase(PHASE_DONE, "环境准备完成")
    return True, f"环境准备完成：DSH {rep.install.version}"


def uninstall_hint() -> str:
    """给用户看的卸载说明——我们装的东西要能干净卸掉。"""
    return ("如需卸载：删除目录 %s 即可，不会影响系统里其它 Node.js 安装。"
            % config.node_global_dir())
