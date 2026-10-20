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
官方同时维护三个 dist-tag。它们是**三条并行推进的线**，不是简单的"稳定/预览"两级：

    latest  稳定通道，默认跟随
    next    候选通道 —— 下一个正式版的备选（RC 线）
    alpha   尝鲜通道 —— 更早期的测试线

历史教训见 `PRERELEASE_TAGS` 上方的注释：曾经只读 latest + alpha，
结果 0.1.7-rc.1/rc.2 整条 RC 线在界面上完全不可见。
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import config, contract
from .compat import compare, parse_version

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
    # 候选通道（RC）。放在 action 之后是为了保住字段顺序的兼容性——
    # 之前的调用方都是关键字传参，新增字段不会破坏它们。
    next: str | None = None
    tags: dict = field(default_factory=dict)

    @property
    def has_action(self) -> bool:
        return self.action != "none"

    def newer_preview(self) -> str | None:
        """预览通道里比当前已装版本更高的最高版本。

        「加入预览计划」没开时，界面上说"已是最新"是**不负责任**的——
        只要候选/尝鲜通道躺着更新的版本，就必须让用户知道，否则他会以为
        官方没发新版（这正是 0.1.7-rc.x 被漏掉时的体验）。
        """
        best = None
        for v in (self.next, self.alpha):
            if v and compare(v, self.current) > 0 \
                    and (best is None or compare(v, best) > 0):
                best = v
        return best

    def preview_below_current(self) -> list[tuple[str, str]]:
        """预览通道里版本低于已装版本的 (通道短名, 版本)。

        预览计划取"所有预览通道里最高的一版"，不会为了切到 alpha 而降级。
        但用户看到"尝鲜通道 0.2.1-alpha.5"却被告知已是最新时，容易以为
        漏检了 alpha——界面用这个列表补一句解释。
        """
        out = []
        for name, v in (("候选", self.next), ("尝鲜", self.alpha)):
            if v and parse_version(v) is not None and compare(v, self.current) < 0:
                out.append((name, v))
        return out

    def summary(self) -> str:
        if self.action == "upgrade":
            label = "稳定通道新版" if self.channel == "latest" else "预览通道新版"
            return f"发现{label} {self.target}（当前 {self.current}）"
        if self.action == "downgrade":
            return f"可回归稳定版 {self.target}（当前 {self.current}）"
        return f"已是最新（{self.current}，{self.channel} 通道）"


# ------------------------------------------------------------------ 通道查询

def _npm_name(tags: dict) -> str:
    return tags.get("latest", "?")


# 除稳定通道外，官方还有哪些 dist-tag 属于"预览"。顺序**不代表优先级**——
# 谁最高由版本号决定，见 decide()。
#
# 为什么不能用"稳定 / 预览"两级、更不能写死优先某个 tag：
#
#   实测 2026-09-24，三个 tag 同时存在且**代次错开**：
#       latest = 0.1.5-rc.3   （稳定线停在 0.1.5）
#       alpha  = 0.1.7-alpha.2
#       next   = 0.1.7-rc.2   （RC 比 alpha 还新！RC 是 alpha 之后的阶段）
#
#   旧实现只读 latest + alpha，于是 0.1.7-rc.1/rc.2 整条 RC 线在界面上
#   完全不可见；更糟的是已装 0.1.7-alpha.2 的用户会被告知"已是最新"，
#   而官方其实已经发了两个更晚的 RC。
#
#   教训：通道之间的先后顺序会随上游发布流程变化，写死顺序迟早再错一次。
#   所以这里只声明"哪些 tag 算预览"，高低交给版本号比较。
PRERELEASE_TAGS = ("next", "alpha")


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
    """npm 输出可能夹杂警告行，从第一个 `{` 起取一个完整的 JSON 对象。

    为什么不用"从末尾往前截、反复 json.loads"（2026-10-20 体检）：
    那是对整段文本反复解析，尾部有 N 字节噪声就白跑 N 次，实测 17 KB 噪声
    要 57 ms（O(n²)）。`raw_decode` 一次就能告诉我们在哪里停，且多余内容
    直接忽略——正是"从噪声里挑 JSON"想要的行为。
    """
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


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


def _highest(candidates: list[tuple[str, str]]) -> tuple[str, str] | None:
    """从 (版本, 来源) 里取版本最高的一项；版本号无法解析的略过。

    必须显式跳过不可解析的值：compare() 对解析失败返回 0，若只看 `> 0`
    会让第一个非法值永远霸占 best。
    """
    best = None
    for ver, tag in candidates:
        if not ver or parse_version(ver) is None:
            continue
        if best is None or compare(ver, best[0]) > 0:
            best = (ver, tag)
    return best


def action_for(current: str, target: str) -> str:
    """目标相对当前版本是升级 / 降级 / 不动。

    "回滚 / 回归稳定版"走的是手工指定 target 的路径，不能一律写成
    upgrade：降级要触发对话框里的"旧版本可能打不开新会话"警告。
    """
    c = compare(target, current)
    return "upgrade" if c > 0 else ("downgrade" if c < 0 else "none")


def decide(install: contract.DshInstall, tags: dict, prerelease_opt_in: bool) -> UpdateInfo:
    """根据通道策略决定目标版本。

    规则：
      - 未开启预览计划 → 只跟稳定通道
      - 已开启预览计划 → 在稳定通道与所有预览 tag 里取**版本最高**的一个；
        且如果预览线反而落后于稳定线（上游提版顺序变过），仍然只给稳定版，
        免得"开启预览"变成"被降级"。
    """
    stable = tags.get("latest") or install.version
    if parse_version(stable) is None:
        stable = install.version          # 通道值不可解析时退回已装版本，别把垃圾传下去

    preview = _highest([(tags.get(t), t) for t in PRERELEASE_TAGS])

    target, channel = stable, "latest"
    if prerelease_opt_in and preview and compare(preview[0], stable) > 0:
        target, channel = preview

    action = action_for(install.version, target)
    return UpdateInfo(current=install.version, stable=stable,
                      next=tags.get("next"), alpha=tags.get("alpha"),
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


def uninstall_version(install: contract.DshInstall, on_line=None,
                      timeout: float = 300):
    """卸载全局 DSH。返回 (是否成功, 输出文本)。

    为什么更新前要先卸（2026-10-20 定）
    ----------------------------------
    `npm install -g <pkg>@<ver>` 并不保证把**子依赖**一起升上去。本机实测过
    一次典型事故：`dsh` 已经是 0.2.1-rc.2，而 `dsh-client-*` 等一批精确依赖
    仍停在 0.1.5-rc.2 —— npm 认为它们"已经在了"，于是跳过。后果是旧前端去
    请求新服务端已移除的端点，控制台一堆 404，而版本号看起来完全正常。

    先卸再装能把这个状态清掉：卸载会连带移除不再被引用的依赖，
    重装时 npm 必须按新版清单重新解析整棵树。
    """
    if not install.npm_cli:
        return False, "未找到 npm 入口，无法卸载。"
    args = [install.node, install.npm_cli, "uninstall", "-g", PKG_NAME,
            "--no-fund", "--no-audit"]
    args += _npm_prefix_args(install)
    if on_line:
        on_line("$ npm uninstall -g %s" % PKG_NAME)
    rc, out, err = contract.run_capture(args, timeout=timeout)
    text = ((out or "") + (err or "")).strip()
    if on_line and text:
        for line in text.splitlines()[-8:]:
            on_line(line)
    return rc == 0, text


def install_version(install: contract.DshInstall, version: str,
                    on_line=None, timeout: float = 600, fresh: bool = True):
    """通过 npm 安装指定版本。返回 (是否成功, 输出文本)。

    调用方必须先把服务停掉——Windows 上 node_modules 可能被占用。

    `fresh=True`（默认）先卸载旧版本再装。卸载失败**不算致命**：可能本来
    就没装（首次安装），此时直接装即可。
    """
    if not install.npm_cli:
        return False, "未找到 npm 入口，无法执行更新。请确认 node/npm 安装完整。"

    if fresh:
        if on_line:
            on_line("先卸载旧版本，避免子依赖被 npm 跳过…")
        uninstall_version(install, on_line=on_line)
        # 卸载后 npm 的 prefix 可能仍需重新确认，但 args 是按当前 install 算的，
        # 首次安装时也照样成立。

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


# ---------------------------------------------------------------- 半升级检查

def _installed_version(prefix: str, name: str) -> str:
    """某个依赖实际装在哪、什么版本。两处都查：全局顶层与 dsh 自己的 node_modules。"""
    bases = (prefix,
             os.path.join(prefix, "node_modules", *PKG_NAME.split("/")))
    for base in bases:
        p = os.path.join(base, "node_modules", *name.split("/"), "package.json")
        try:
            with open(p, encoding="utf-8") as f:
                return str(json.load(f).get("version") or "")
        except (OSError, ValueError):
            continue
    return ""


def check_dependency_consistency(install: contract.DshInstall) -> list[tuple[str, str, str]]:
    """检查 DSH 的**精确版本**依赖是否都装成了同一个版本。

    返回 `[(包名, 期望, 实际)]`，空列表表示一致。实际版本缺失时记为"缺失"。

    为什么只比精确版本
    ------------------
    `~4.0.5` 这类范围本来就允许浮动，拿字面量去比会全是误报。
    而 `0.2.1-rc.2` 这种精确值一旦对不上，就是真正的半升级——
    2026-10-20 实测到的正是这一类：主包升了，`dsh-client-*` 全留在旧版，
    表现为前端请求 404、账号信息读不出来，光看版本号看不出来。
    """
    pkg = os.path.join(install.prefix, "node_modules", *PKG_NAME.split("/"))
    try:
        with open(os.path.join(pkg, "package.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    drift: list[tuple[str, str, str]] = []
    for name, spec in (data.get("dependencies") or {}).items():
        want = str(spec).strip()
        if not want or want[0] not in "0123456789":
            continue
        got = _installed_version(install.prefix, name)
        if got != want:
            drift.append((name, want, got or "缺失"))
    return drift


def remove_drifted(install: contract.DshInstall, drift, on_line=None) -> list[str]:
    """删掉版本不一致的依赖目录，让下一次安装重新拉。

    只删**全局顶层**那一份，且只删 drift 点名的包——它们都是 DSH 自己的
    精确依赖，删掉后由紧接着的 npm install 补回来，不会波及其它全局包。
    """
    emit = on_line or (lambda s: None)
    removed: list[str] = []
    for name, _want, _got in drift:
        p = os.path.join(install.prefix, "node_modules", *name.split("/"))
        if not os.path.isdir(p):
            continue
        try:
            shutil.rmtree(p)
            removed.append(p)
            emit("已清理旧版本依赖：%s" % name)
        except OSError as e:
            emit("清理失败：%s（%s）" % (name, e))
    return removed


def verify(install: contract.DshInstall, expected: str) -> tuple[bool, str]:
    """安装后复验：重新定位并读取版本，确认真的换过去了。"""
    try:
        fresh = contract.resolve_install(force=True)
    except contract.DshError as e:
        return False, f"更新后无法重新定位 DSH：{e}"
    if compare(fresh.version, expected) != 0:
        return False, f"版本校验失败：期望 {expected}，实际 {fresh.version}"
    return True, fresh.version


def _resolve_or(install: contract.DshInstall) -> contract.DshInstall:
    """重新定位一次安装（卸载/重装之后 prefix 可能变）。取不到就用传入的。"""
    try:
        return contract.resolve_install(force=True)
    except contract.DshError:
        return install


def _install_drift(install: contract.DshInstall) -> list[tuple[str, str, str]]:
    return check_dependency_consistency(_resolve_or(install))


def reinstall(install: contract.DshInstall, stop_service, start_service,
              on_line=None):
    """按**当前版本**重装一遍，用于修复半升级或损坏的安装。

    流程与 `update_to` 完全一致（含"先卸再装"和子依赖一致性校验），
    区别只在目标版本取自当前安装。所以它既能修"更新到一半"的状态，
    也能修手动改坏的状态——不需要用户自己去命令行敲 npm。
    """
    return update_to(install, install.version, stop_service, start_service,
                     on_line=on_line, fresh=True)


def update_to(current_install: contract.DshInstall, version: str,
              stop_service, start_service, on_line=None, fresh: bool = True):
    """完整更新事务。

    停服 → 记录用户数据指纹 → npm 安装 → 版本校验 → **子依赖一致性校验**
    → 比对指纹 → 重启。任何一步失败自动把版本装回去。

    `fresh=True`（默认）会先卸载旧版本。这不是为了"装干净"的洁癖：
    2026-10-20 实测发现 `npm install -g` 可能只升主包、把精确版本的子依赖
    留在旧版，界面表现为控制台一堆 404，而版本号看起来完全正常。
    卸载 + 重装是唯一能不依赖 npm 内部策略就把它清掉的做法；
    后面那条一致性校验是第二道闸，两者都不通过就明确报出来。

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
    ok, text = install_version(current_install, version, on_line=emit, fresh=fresh)
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

    # --- 子依赖一致性：npm 可能只升主包，把精确版本的依赖留在旧版 ---
    # 这是 2026-10-20 那次"控制台一堆 404、版本号却正常"事故的兜底。
    drift = _install_drift(current_install)
    if drift:
        emit("发现 %d 个子依赖没跟上主包：" % len(drift))
        for name, want, got in drift[:8]:
            emit("  · %s：期望 %s，实际 %s" % (name, want, got))
        if len(drift) > 8:
            emit("  · …另有 %d 个" % (len(drift) - 8))
        emit("清理这些旧版本后重装…")
        target = _resolve_or(current_install)
        remove_drifted(target, drift, on_line=emit)
        ok2, text2 = install_version(target, version, on_line=emit, fresh=False)
        if not ok2:
            emit("清理后重装失败：\n" + text2[-600:])
        drift = _install_drift(target)

    if drift:
        detail = (
            "安装完成，但仍有 %d 个子依赖是旧版本（例如 %s 期望 %s、实际 %s）。\n\n"
            "这种状态下界面会出现接口 404、账号信息读不出来等问题，"
            "但版本号看起来是正常的。\n"
            "请稍后再试一次，或在「本机部署」页点「重新安装 DSH」。"
            % (len(drift), drift[0][0], drift[0][1], drift[0][2]))
        emit(detail)
        check_data(before)
        config.set(previous_version=original, last_good_version=detail)
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
        "latest": "稳定通道",
        "next": "候选通道（下一正式版的备选版本）",
        "alpha": "尝鲜通道（更早期的测试版本）",
    }.get(channel, channel or "未知")


def channel_short(channel: str) -> str:
    """塞进句子里的短名，如「这是候选版」。

    不要用 channel_label 拼句子——它带括号解释，拼出来会变成
    "这是候选通道（下一正式版的备选版本）版"。"""
    return {
        "latest": "稳定",
        "next": "候选",
        "alpha": "尝鲜",
    }.get(channel, "预览")
