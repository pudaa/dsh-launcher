# -*- coding: utf-8 -*-
"""L2 更新器：只走官方 npm 通道。

为什么不用"拉源码替换"或"复制文件备份"
--------------------------------------
DSH 的官方分发渠道就是 npm（README 写明 `npx @deepseek-ai/dsh web`），
CLI 自身不提供 update 子命令。所以：

    更新 = npm install -g @deepseek-ai/dsh@<版本>
    回滚 = npm install -g @deepseek-ai/dsh@<旧版本>

两条路都走 npm，不碰文件系统。这样每次操作后 npm 的依赖树、bin 包装、
package-lock 都是自洽的——手工替换目录会破坏这种自洽性，下一次更新必然出问题。

通道策略
--------
    latest  稳定通道，默认
    alpha   预览通道，只有开启「加入预览计划」后才会选中
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import config, contract
from .compat import compare, is_prerelease

PKG_NAME = "@deepseek-ai/dsh"
REPO = "deepseek-ai/deepseek-harness"


@dataclass
class UpdateInfo:
    current: str
    stable: str
    alpha: str | None
    target: str
    channel: str
    action: str                    # upgrade | downgrade | none
    tags: dict = field(default_factory=dict)

    @property
    def has_action(self) -> bool:
        return self.action != "none"

    def summary(self) -> str:
        if self.action == "upgrade":
            label = "预览通道新版" if self.channel == "alpha" else "正式通道新版"
            return f"发现{label} {self.target}（当前 {self.current}）"
        if self.action == "downgrade":
            return f"可回归稳定版 {self.target}（当前 {self.current}）"
        return f"已是最新（{self.current}，{self.channel} 通道）"


# ------------------------------------------------------------------ 通道查询

def _npm_name(tags: dict) -> str:
    return tags.get("latest", "?")


def fetch_dist_tags(install: contract.DshInstall, timeout: float = 90) -> dict:
    """查询官方通道版本。

    主路：npm view（自动遵循 .npmrc 里的 registry / 镜像配置，与 npm 官方通道一致）
    备路：直接查 registry（npm 不可用或超时时的降级路径）
    """
    errors: list[str] = []

    if install.npm_cli:
        rc, out, err = contract.run_capture(
            [install.node, install.npm_cli, "view", PKG_NAME, "dist-tags", "--json"],
            timeout=timeout,
        )
        if rc == 0:
            tags = _parse_json_object(out)
            if tags:
                return tags
            errors.append("npm view 返回内容无法解析")
        else:
            errors.append(f"npm view 退出码 {rc}: {(err or '').strip()[:200]}")
    else:
        errors.append("未找到 npm 入口")

    tags = _fetch_tags_via_registry(install)
    if tags:
        return tags

    raise contract.DshError("无法查询官方版本通道。\n" + "\n".join(errors))


def _parse_json_object(text: str) -> dict | None:
    """npm 输出可能夹杂警告行，从第一个 { 起截取。"""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    for end in range(len(text), start, -1):
        try:
            data = json.loads(text[start:end])
            if isinstance(data, dict):
                return data
            return None
        except ValueError:
            continue
    return None


def get_registry(install: contract.DshInstall) -> str:
    if install.npm_cli:
        rc, out, _ = contract.run_capture(
            [install.node, install.npm_cli, "config", "get", "registry"], timeout=30)
        if rc == 0 and out.strip():
            return out.strip().splitlines()[-1].strip()
    return "https://registry.npmjs.org/"


def _http_json(url: str, timeout: float = 20) -> dict | None:
    """先按正常证书校验取，失败再放宽——与本机 npm 的 TLS 姿态保持一致。"""
    for ctx in (ssl.create_default_context(), ssl._create_unverified_context()):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "dsh-desktop"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


def _fetch_tags_via_registry(install: contract.DshInstall) -> dict | None:
    registry = get_registry(install).rstrip("/")
    for url in (f"{registry}/{PKG_NAME.replace('/', '%2f')}",
                f"https://registry.npmjs.org/{PKG_NAME.replace('/', '%2f')}"):
        data = _http_json(url)
        if isinstance(data, dict) and isinstance(data.get("dist-tags"), dict):
            return data["dist-tags"]
    return None


def decide(install: contract.DshInstall, tags: dict, prerelease_opt_in: bool) -> UpdateInfo:
    """根据通道策略决定目标版本。"""
    stable = tags.get("latest") or install.version
    alpha = tags.get("alpha")

    if prerelease_opt_in and alpha and compare(alpha, stable) > 0:
        target, channel = alpha, "alpha"
    else:
        target, channel = stable, "latest"

    c = compare(target, install.version)
    action = "upgrade" if c > 0 else ("downgrade" if c < 0 else "none")
    return UpdateInfo(current=install.version, stable=stable, alpha=alpha,
                      target=target, channel=channel, action=action, tags=tags)


def check(install: contract.DshInstall, prerelease_opt_in: bool | None = None) -> UpdateInfo:
    if prerelease_opt_in is None:
        prerelease_opt_in = bool(config.get("prerelease_opt_in"))
    return decide(install, fetch_dist_tags(install), prerelease_opt_in)


# ------------------------------------------------------------------ 执行更新

def _npm_prefix_args(install: contract.DshInstall) -> list[str]:
    """确认 npm 的全局 prefix 与 DSH 实际所在位置一致，不一致就显式指定。"""
    if not install.npm_cli:
        return []
    rc, out, _ = contract.run_capture(
        [install.node, install.npm_cli, "prefix", "-g"], timeout=45)
    if rc != 0 or not out.strip():
        return []
    prefix = out.strip().splitlines()[-1].strip()
    try:
        same = os.path.normcase(os.path.realpath(prefix)) == \
            os.path.normcase(os.path.realpath(install.prefix))
    except OSError:
        same = False
    return [] if same else ["--prefix", install.prefix]


def install_version(install: contract.DshInstall, version: str,
                    on_line=None, timeout: float = 600):
    """通过 npm 安装指定版本。返回 (是否成功, 输出文本)。

    调用方必须先把服务停掉——Windows 上 node_modules 可能被占用。
    """
    if not install.npm_cli:
        return False, "未找到 npm 入口，无法执行更新。请确认 node/npm 安装完整。"

    args = [install.node, install.npm_cli, "install", "-g", f"{PKG_NAME}@{version}",
            "--no-fund", "--no-audit", "--loglevel=http"]
    args += _npm_prefix_args(install)

    if on_line:
        on_line("$ npm install -g %s@%s" % (PKG_NAME, version))
    rc, out, err = contract.run_capture(args, timeout=timeout)
    text = ((out or "") + (err or "")).strip()
    if on_line and text:
        for line in text.splitlines()[-40:]:
            on_line(line)
    return rc == 0, text


def verify(install: contract.DshInstall, expected: str) -> tuple[bool, str]:
    """安装后复验：重新定位并读取版本，确认真的换过去了。"""
    try:
        fresh = contract.resolve_install(force=True)
    except contract.DshError as e:
        return False, f"更新后无法重新定位 DSH：{e}"
    if compare(fresh.version, expected) != 0:
        return False, f"版本校验失败：期望 {expected}，实际 {fresh.version}"
    return True, fresh.version


def update_to(current_install: contract.DshInstall, version: str,
              stop_service, start_service, on_line=None):
    """完整更新事务。

    停服 → 记录用户数据指纹 → npm 安装 → 校验 → 比对指纹 → 重启。
    任何一步失败自动把版本装回去。

    关于"会不会丢聊天记录"
    ----------------------
    用户数据全在 DSH_HOME 下（聊天记录在 sessions\\，见官方 dump-config），
    而 npm 只替换 `<prefix>\\node_modules\\`，两边不相交。所以更新本身不动数据。

    但这是"设计上不该动"，不是"我们查过了"。所以每步都用 `home_fingerprint()`
    前后各数一遍文件数与字节数——**如果更新后数据反而变少了，明确报出来**。
    只统计不复制：快照会带来不可预估的存储压力，而这个问题一对数字就能回答。
    """
    original = current_install.version
    emit = on_line or (lambda s: None)

    def check_data(before: contract.HomeFingerprint) -> None:
        after = contract.home_fingerprint(before.path)
        if before.truncated or after.truncated:
            # 数据量超出统计上限时不当成"没问题"——那会把"没查"伪装成"查过了"
            emit("用户数据量较大，本次未做增减比对（统计已截断）：%s" % after.describe())
            return
        if after.lost_against(before):
            emit("警告：更新后用户数据比更新前少了（%s → %s）。"
                 % (before.describe(), after.describe()))
            emit("更新只应替换程序文件，不应改动 %s，请检查该目录。"
                 % (before.path or "DSH_HOME"))
        else:
            emit("用户数据未减少：%s → %s" % (before.describe(), after.describe()))

    emit("准备更新：%s → %s" % (original, version))
    emit("停止后台服务…")
    stop_service()

    # 基线必须在服务停止之后取——否则运行中的 DSH 还在写，数字会晃
    before = contract.home_fingerprint()
    emit("更新前用户数据：%s" % before.describe())

    emit("执行官方 npm 安装…")
    ok, text = install_version(current_install, version, on_line=emit)
    if not ok:
        emit("安装失败，回滚到 %s" % original)
        install_version(current_install, original, on_line=emit)
        check_data(before)
        start_service()
        return False, "npm 安装失败。\n" + text[-1500:]

    emit("校验版本…")
    ok, detail = verify(current_install, version)
    if not ok:
        emit(detail)
        emit("校验未通过，回滚到 %s" % original)
        install_version(current_install, original, on_line=emit)
        try:
            contract.resolve_install(force=True)
        except contract.DshError:
            pass
        check_data(before)
        start_service()
        return False, detail

    check_data(before)

    # 记录回滚锚点
    config.set(previous_version=original, last_good_version=detail)
    emit("版本校验通过：%s" % detail)
    emit("重启后台服务…")
    start_service()
    return True, detail


# ------------------------------------------------------------------ 更新说明

def fetch_release_notes(version: str, timeout: float = 12) -> str | None:
    """尽力而为地取 GitHub Release 说明。取不到不算错误——国内网络访问 GitHub 本就不稳。"""
    tag = version if version.startswith("dsh-") else f"dsh-v{version}"
    data = _http_json(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}", timeout)
    if not isinstance(data, dict):
        return None
    body = data.get("body")
    return body if isinstance(body, str) and body.strip() else None


def channel_label(channel: str) -> str:
    """给界面用的通道名字。面向使用者，不出现 dist-tag 这类词。"""
    return {
        "alpha": "预览通道（alpha）",
        "latest": "稳定通道（latest）",
        "next": "预发布通道（next）",
    }.get(channel, channel or "未知")


def channel_note(version: str) -> str:
    return "预览通道（预发布版本，可能包含破坏性变更）" if is_prerelease(version) \
        else "稳定通道"
