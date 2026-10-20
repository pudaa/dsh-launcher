# -*- coding: utf-8 -*-
"""数据目录的观测与搬迁。

为什么需要这一层（2026-10-20 老大提的问题）
------------------------------------------
桌面壳是**单 exe**：删掉 exe 就结束了，但数据目录留在 `%LOCALAPPDATA%` 里不会跟着消失。
用户换机、重装、或者单纯不想要了，那几百 MB 就成了 C 盘上的无声残留。
所以要把三件事做成用户能自己操作的动作：

    看   —— 现在占多大、都在哪
    搬   —— 换到别的磁盘（指针文件固定在默认根，见 config.pointer_path）
    清   —— 清缓存（安全）或清空全部（回到首次运行）

边界
----
- 只处理**桌面壳自己的数据根**。DSH_HOME（会话、模型凭据）是 DSH 的地盘，
  这里一概不碰——它有自己的重建与迁移路径。
- 迁移前必须先停服务、关掉 WebEngine：Windows 上被占用的 profile 目录删不掉，
  复制到一半失败会留下"半个新目录 + 完整旧目录"，那是最难收拾的状态。
  这个协调由界面层负责，本模块只假设"当前没有进程占着这些文件"。
"""
from __future__ import annotations

import os
import shutil
import time

from . import config


class StorageError(Exception):
    """数据目录操作失败（可直接展示给用户）。"""


def str_of_bytes(n: float) -> str:
    """给界面用的体积文案。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit in ("B", "KB") else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def str_of_count(n: int) -> str:
    """给界面用的文件数文案（粗粒度）。

    用户明确说过"不需要那么精细"，而且 66447 这种精确数字对"占多大"的判断
    毫无帮助——反而暗示"数得越准越可信"。所以上万以后按"万"报：
        9999 → "9999"      66447 → "6.6 万"      1200000 → "120 万"
    """
    n = int(n)
    if n < 10000:
        return str(n)
    if n < 100000:
        return "%.1f 万" % (n / 10000.0)
    return "%.0f 万" % (n / 10000.0)


#: 单次统计的时间预算（秒）。超了就停，宁可给一个"偏小"的近似值。
SCAN_BUDGET_S = 3.0

#: 单次统计的条目上限（文件 + 目录）。防止极端目录（几十万小文件）把
#: 后台线程拖成几十秒——那时用户早就切走了，结果没人看。
SCAN_MAX_ENTRIES = 400_000


def tree_usage(path: str) -> tuple[int, int]:
    """返回 (文件数, 总字节)。路径不存在时返回 (0, 0)。

    用 `_scan()` 的同一套 scandir 逻辑，**无时间预算**（这里的调用方是
    迁移前/后的完整性核对，宁可慢也要数准）。旧实现是 `os.walk` +
    `os.path.getsize`：每个文件额外 stat 一次，在 Windows 上等于再打开一次
    文件，实测比这里慢 8 倍（66443 文件的真实数据根：5.8s → 0.7s）。
    """
    if not os.path.isdir(path):
        return 0, 0
    files, total, _per, _partial = _scan(path, budget_s=0, max_entries=0)
    return files, total


#: 界面上分组显示。顺序即展示顺序；"其他"兜底（放最后，不参与求和展示）。
# 标签刻意短：KeyValueRow 的 key 列宽固定 128px，"浏览器数据与登录态"这种
# 九个字会把它撑开，结果整列值都跟着右移，和上面的行对不齐（2026-10-20 实测）。
GROUPS = (
    ("profile", "浏览器数据", "登录态与缓存，删掉需要重新登录 DSH Web"),
    ("logs", "日志", "排查问题用的文本，删掉不影响使用"),
    ("node-global", "私有 DSH", "独立模式的程序本体，删掉需要重新安装"),
    ("cache", "临时缓存", "可以随时清"),
)


def _scan(root: str, budget_s: float = SCAN_BUDGET_S,
          max_entries: int = SCAN_MAX_ENTRIES) -> tuple[int, int, dict, bool]:
    """一次遍历算出总量与各分组占用；超预算就带着"不完整"标记返回。

    为什么这么写（2026-10-20 用户的第二版反馈："文件数量大的话耗时长又占内存，
    不需要那么精细"）
    ---------------------------------------------------------------------------
    1. **用 `os.scandir` 而不是 `os.walk` + `os.path.getsize`**：后者每读一个
       文件都要再 `stat()` 一次，在 Windows 上等于再打开一次文件——Defender
       逐个扫，这就是 5 秒里的大头。`scandir` 的目录枚举结果里已经带 size，
       `entry.stat()` 直接命中缓存，**同一次枚举就能拿到**。
    2. **时间/条目预算**：真遇到几十万小文件的目录，宁可给个近似值也不让
       后台线程跑几十秒。超预算时 `partial=True`，界面会标"≥"并说明原因。
    3. 只统计根下第一层目录对应的分组；直接躺在根上的文件只计入总量。

    `budget_s=0` / `max_entries=0` 表示**不设该上限**（`tree_usage()` 走这条，
    它要的是准确值，不是快）。

    返回 (文件数, 总字节, {分组key: [文件数, 字节]}, 是否不完整)。
    """
    per = {key: [0, 0] for key, _label, _hint in GROUPS}
    files = 0
    total = 0
    seen = 0
    if not os.path.isdir(root):
        return 0, 0, per, False
    deadline = time.monotonic() + budget_s if budget_s else None

    # 显式栈代替递归：目录层级不受 Python 递归上限影响。
    # 每项是 (目录绝对路径, 该目录所属的分组 key 或 None)。
    stack = [(os.path.abspath(root), None)]
    while stack:
        cur, group = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue                      # 权限不足 / 半路被删，跳过这一层
        with it:
            for entry in it:
                seen += 1
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child = group
                        # 只有根下的第一层目录才认分组名；更深的继承父级
                        if child is None and entry.name in per:
                            child = entry.name
                        stack.append((entry.path, child))
                        continue
                    # 目录枚举里已经带 stat 信息，这一次调用不额外打开文件
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue                  # 读不到就跳过，别让整次统计失败
                files += 1
                total += size
                if group is not None:
                    per[group][0] += 1
                    per[group][1] += size
                if max_entries and seen >= max_entries:
                    return files, total, per, True
                # 每 512 项看一次表（每项都调 monotonic 也要钱）；第 1 项时
                # 也查一次，这样"预算已经用完"（budget_s<=0）能立刻生效，
                # 小目录不会因为凑不满一个批次就绕过预算。
                if deadline is not None and (seen <= 1 or (seen & 511) == 0) \
                        and time.monotonic() > deadline:
                    return files, total, per, True
    return files, total, per, False


def breakdown() -> dict:
    """当前数据根的占用明细。

    返回 {"root", "files", "bytes", "groups": [{key,label,hint,files,bytes}],
          "other", "is_default", "partial"}。

    ⚠️ 这个调用**很贵**（秒级，见 `_scan` 的说明）。界面线程里不要直接调它，
    用 `dsh_panel.UsageWorker` 挪到后台。
    """
    root = config.data_root()
    files, total, per, partial = _scan(root)
    groups = []
    counted = 0
    for key, label, hint in GROUPS:
        f, b = per.get(key, (0, 0))
        if b or f:
            groups.append({"key": key, "label": label, "hint": hint,
                           "files": f, "bytes": b})
            counted += b
    return {
        "root": root,
        "files": files,
        "bytes": total,
        "groups": groups,
        "other": max(0, total - counted),
        "partial": partial,
        "is_default": os.path.normcase(os.path.abspath(root))
        == os.path.normcase(os.path.abspath(config.default_root())),
    }


def migrate(target: str, on_line=None) -> str:
    """把数据根整体搬到 `target`，返回新根。

    只做三件事：检查目标 → 复制 → 切指针。**旧数据不在这里删**——
    由调用方在确认新位置可用之后再广播 `remove_old()`，避免"复制成功但删旧失败"
    被当成整体失败（那时数据其实已经安全在新位置了）。
    """
    emit = on_line or (lambda s: None)
    src = config.data_root()
    tgt = os.path.abspath((target or "").strip())
    if not tgt:
        raise StorageError("没有选择目标目录。")
    if os.path.normcase(src) == os.path.normcase(tgt):
        raise StorageError("目标目录就是当前位置。")
    if os.path.exists(tgt) and os.listdir(tgt):
        raise StorageError("目标目录不是空的。请选一个空目录。")
    if tgt.startswith(src + os.sep):
        raise StorageError("不能把数据目录搬到它自己的子目录里。")

    emit("复制数据：%s → %s" % (src, tgt))
    try:
        shutil.copytree(src, tgt, dirs_exist_ok=True)
    except OSError as e:
        raise StorageError("复制失败：%s\n\n原目录未被改动，可以稍后重试。" % e) from e

    new_files, new_bytes = tree_usage(tgt)
    old_files, old_bytes = tree_usage(src)
    if new_files < old_files or new_bytes < old_bytes:
        raise StorageError(
            "复制后的文件比原来少（%d → %d 个），已中止。\n原目录未被改动。"
            % (old_files, new_files))

    config.set_data_root(tgt)
    emit("已切换：%s（%s，%d 个文件）"
         % (tgt, str_of_bytes(new_bytes), new_files))
    return tgt


def remove_old(old_root: str, keep_pointer: bool = True) -> bool:
    """删掉搬走之后的旧目录内容。失败返回 False（不影响新位置）。

    `keep_pointer=True` 时保留 `location.json`——它就在默认根里，
    删掉就找不到新位置了。
    """
    old_root = os.path.abspath(old_root)
    if not os.path.isdir(old_root):
        return True
    ptr = config.pointer_path()
    ok = True
    for name in os.listdir(old_root):
        p = os.path.join(old_root, name)
        if keep_pointer and os.path.normcase(p) == os.path.normcase(ptr):
            continue
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
        except OSError:
            ok = False
    return ok


def clear_cache() -> tuple[int, int]:
    """清缓存：只删 profile 与 logs，保留设置、安装与登录态之外的配置。

    返回 (删除文件数, 释放字节)。注意 profile 里存着 WebEngine 的登录态，
    删了要重新登录——所以这一步在界面上要写清楚。
    """
    freed_files = freed_bytes = 0
    # devtools-profile 也要清（2026-10-20 体检）：那是 Chromium 给开发者工具
    # 单独开的 profile，缓存在 `data_root()/devtools-profile`，以前不在清理范围内，
    # 越用越大，而用户在"清理缓存"里看不到它。
    for name in ("profile", "devtools-profile", "logs", "cache"):
        p = os.path.join(config.data_root(), name)
        f, b = tree_usage(p)
        if os.path.isdir(p):
            try:
                shutil.rmtree(p)
                freed_files += f
                freed_bytes += b
            except OSError as e:
                raise StorageError("清理 %s 失败：%s\n请先退出桌面壳再试。" % (name, e)) from e
    return freed_files, freed_bytes


def clear_all() -> tuple[int, int]:
    """清空全部数据：回到首次运行状态（会丢设置、登录态与私有 DSH）。

    保留位置指针本身，否则连"数据在哪"都记不住了。
    """
    root = config.data_root()
    files, total = tree_usage(root)
    remove_old(root, keep_pointer=True)
    return files, total
