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


def tree_usage(path: str) -> tuple[int, int]:
    """返回 (文件数, 总字节)。路径不存在时返回 (0, 0)。"""
    files = 0
    total = 0
    if not os.path.isdir(path):
        return 0, 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
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


def _scan(root: str) -> tuple[int, int, dict]:
    """**一次**遍历数据根，同时算出总量与每个分组的占用。

    为什么要一次遍历（2026-10-20）
    -----------------------------
    以前 `breakdown()` 对数据根和 4 个分组各跑一遍 `tree_usage()`，等于把整棵树
    走两遍。私有部署下这棵树是 **6.6 万个文件 / 727 MB**，实测 10.2 秒
    （Windows Defender 逐文件扫描是主要成本）。一次遍历后 **5.1 秒**（2.0x），
    剩下的交给后台线程（`dsh_panel.UsageWorker`）——它以前就在界面线程里同步跑，
    于是"每次切到「常规」页都卡十几秒"。

    返回 (文件数, 总字节, {分组key: [文件数, 字节]})。只统计根下第一层目录
    对应的分组；直接躺在根上的文件只计入总量（由调用方算进 "other"）。
    """
    per = {key: [0, 0] for key, _label, _hint in GROUPS}
    files = 0
    total = 0
    if not os.path.isdir(root):
        return 0, 0, per
    base = os.path.abspath(root)
    for cur, _dirs, names in os.walk(base):
        rel = os.path.relpath(cur, base)
        bucket = None if rel == os.curdir else per.get(rel.split(os.sep, 1)[0])
        for name in names:
            files += 1
            try:
                size = os.path.getsize(os.path.join(cur, name))
            except OSError:
                size = 0
            total += size
            if bucket is not None:
                bucket[0] += 1
                bucket[1] += size
    return files, total, per


def breakdown() -> dict:
    """当前数据根的占用明细。

    返回 {"root", "files", "bytes", "groups": [{key,label,hint,files,bytes}], "other"}。

    ⚠️ 这个调用**很贵**（秒级，见 `_scan` 的说明）。界面线程里不要直接调它，
    用 `dsh_panel.UsageWorker` 挪到后台。
    """
    root = config.data_root()
    files, total, per = _scan(root)
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
    for name in ("profile", "logs", "cache"):
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
