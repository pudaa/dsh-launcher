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

HOST_VERSION = "2.1.1"

#: 桌面壳自身的发布仓库（owner/repo）。自动更新从这里拉 Release。
HOST_REPO = "pudaa/dsh-launcher"

#: release 资产文件名。必须与打包产物一致。
HOST_ASSET_NAME = "DSH-Web.exe"

#: 我们所依附的 DSH npm 包（仅用于展示与诊断，更新走 updater.py）
DSH_PKG = "@deepseek-ai/dsh"


def version_tuple(v: str) -> tuple:
    """把版本串拆成可比较的元组。

    只用于桌面壳自己的版本比较（我们发的是规范的 semver），
    所以这里可以放心地用严格规则，不需要 compat.py 里那套代数比较。
    """
    core = v.strip().lstrip("v")
    main, _, pre = core.partition("-")
    nums = []
    for part in main.split("."):
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    # 有预发布后缀的排在正式版之前（-1 < 0）
    return tuple(nums[:3]) + ((0, pre) if pre else (1, ""))


def is_newer(candidate: str, current: str) -> bool:
    """candidate 是否比 current 新。解析失败时保守返回 False（不提示更新）。"""
    try:
        return version_tuple(candidate) > version_tuple(current)
    except Exception:
        return False
