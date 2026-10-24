# -*- coding: utf-8 -*-
"""桌面壳自身的版本与发行信息。

为什么单独一个模块
------------------
版本号需要三个地方读：界面（"关于"里显示）、更新器（与 release 比对）、
发布脚本（打 tag）。散落各处迟早会对不上，所以只在这里定义一次。

    HOST_VERSION    桌面壳版本，对应 git tag `v*`
    DSH_PKG         我们所依附的 DSH npm 包名（下游用，便于将来换包）

与 DSH 版本的关系
-----------------
两个版本**完全独立**。桌面壳更新走 GitHub Release，DSH 更新走 npm，
互不干扰。界面上必须分开呈现，否则用户看到"有更新"会以为是同一件事。

发布流程
--------
改这里的 HOST_VERSION → 提交 → 打 tag `v{HOST_VERSION}` → 推 tag。
GitHub Actions 会自动构建 exe 并创建 Release。见 .github/workflows/release.yml。
"""
from __future__ import annotations

HOST_VERSION = "2.4.2"

#: 桌面壳自身的发布仓库（owner/repo）。自动更新从这里拉 Release。
HOST_REPO = "pudaa/dsh-launcher"

#: release 资产文件名。必须与打包产物一致。
HOST_ASSET_NAME = "DSH-Web.exe"

#: 我们所依附的 DSH npm 包（仅用于展示与诊断，更新走 updater.py）
DSH_PKG = "@deepseek-ai/dsh"


def _prerelease_key(pre: str) -> tuple:
    """把预发布段拆成可比较的键（semver 规则）。

    为什么不能直接拿字符串比
    ------------------------
    `beta.10` 在字典序下**小于** `beta.9`（'1' < '9'），语义上却更新。
    发到第四五个预发布版就会漏掉更新提示，而且现象是"明明有新 beta 却
    告诉我已是最新"——与 0.1.7 漏读 next 通道那次是同一类静默失败。

    规则（semver 2.0.0 §11）：
      - 按 `.` 分段；纯数字段按**数值**比，其余按 ASCII 比
      - 数字段的优先级**低于**非数字段（`-1` < `-alpha`）
      - 段数少的更小（`-beta` < `-beta.1`）
    """
    key = []
    for seg in pre.split("."):
        if seg.isdigit():
            # 三元组的第 1 位是"数字 vs 非数字"的优先级标记
            key.append((0, int(seg), ""))
        else:
            key.append((1, 0, seg))
    return tuple(key)


def version_tuple(v: str) -> tuple:
    """把版本串拆成可比较的元组。

    只用于桌面壳自己的版本比较（我们发的是规范的 semver），
    所以这里可以放心地用严格规则，不需要 compat.py 里那套代数比较。

    `X.Y.Z-beta.N` 这类预发布版**排在同一个 X.Y.Z 正式版之前**。
    """
    core = v.strip().lstrip("v")
    core = core.partition("+")[0]          # 构建元数据不参与比较
    main, _, pre = core.partition("-")
    nums = []
    for part in main.split("."):
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    # 第 4 位是发布阶段标记：0=预发布，1=正式版。
    # 同阶段之间比后面的键，预发布那边是拆好的段元组。
    return tuple(nums[:3]) + ((0, _prerelease_key(pre)) if pre else (1, ()),)



def is_prerelease(v: str) -> bool:
    """版本串是否带预发布段（如 `2.4.0-beta.1`）。

    只做文本判断，用途是"这条 release 能不能推给稳定用户"。
    判据必须与 `version_tuple()` 一致：都是"第一个 `-` 之后还有内容"。
    """
    core = v.strip().lstrip("v").partition("+")[0]
    return bool(core.partition("-")[2])


def is_newer(candidate: str, current: str) -> bool:
    """candidate 是否比 current 新。解析失败时保守返回 False（不提示更新）。"""
    try:
        return version_tuple(candidate) > version_tuple(current)
    except Exception:
        return False
