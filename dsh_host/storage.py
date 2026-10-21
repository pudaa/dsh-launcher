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
import stat
import time

from . import config

#: 进度回调的类型：`(已完成文件数, 总文件数, 当前文件名)`。
#: 界面把它接到进度条上；`total` 为 0 表示"数不出来"，界面应显示不确定进度。
ProgressFn = "callable"

#: 两次进度回调之间的最小间隔（秒）。复制 6.6 万个文件时，一次一回调
#: 等于往 GUI 线程灌 6.6 万个跨线程事件——**那本身就会把界面卡住**。
PROGRESS_MIN_INTERVAL_S = 0.08


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


def _reporter(on_progress, total: int):
    """把"逐文件回调"节流成"至多每 PROGRESS_MIN_INTERVAL_S 一次"。

    为什么必须节流（2026-10-21）：数据根有 6.6 万个文件，若每个文件都发一次
    跨线程信号，GUI 线程要先处理完 6.6 万个排队事件才轮得到重绘——看起来
    和"没挪到后台"一模一样。这是同一个病换了条路径复发。
    """
    if on_progress is None:
        return lambda done, name="": None
    last = [0.0]

    def report(done: int, name: str = "") -> None:
        now = time.monotonic()
        # 最后一个必定发，否则进度条永远差一点
        if done < total and now - last[0] < PROGRESS_MIN_INTERVAL_S:
            return
        last[0] = now
        on_progress(done, total, name)

    return report


def copy_tree(src: str, dst: str, on_progress=None) -> tuple[int, int]:
    """逐文件复制，带进度。返回 (文件数, 字节数)。

    不复用 `shutil.copytree`：它没有进度回调，6.6 万个文件复制几十秒期间
    界面完全黑箱。这里自己做遍历，语义与 `copytree(dirs_exist_ok=True)` 一致
    （`copy2` 保留时间戳，覆盖已存在的同名文件）。
    """
    files, _total_bytes = tree_usage(src)
    report = _reporter(on_progress, files)
    done = 0
    copied_bytes = 0
    for root, _dirs, names in os.walk(src):
        rel = os.path.relpath(root, src)
        out_dir = dst if rel == "." else os.path.join(dst, rel)
        try:
            os.makedirs(out_dir, exist_ok=True)
            for name in names:
                s = os.path.join(root, name)
                d = os.path.join(out_dir, name)
                shutil.copy2(s, d)
                done += 1
                try:
                    copied_bytes += os.path.getsize(d)
                except OSError:
                    pass
                report(done, name)
        except OSError as e:
            raise StorageError("复制 %s 失败：%s" % (root, e)) from e
    report(done, "")
    return done, copied_bytes


def remove_tree(path: str, on_progress=None) -> tuple[int, int]:
    """逐文件删除，带进度。返回 (删除文件数, 释放字节)。

    不复用 `shutil.rmtree`：同样没有进度。这里补上 `rmtree` 的两件事——
    ① Windows 上只读文件直接删会失败，要先去只读位再删；
    ② 自底向上删，目录空了才能 rmdir。
    """
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return 0, 0
    files, total_bytes = tree_usage(path)
    report = _reporter(on_progress, files)
    done = 0

    def _force(p: str, func) -> None:
        try:
            func(p)
        except OSError:
            # 只读位是 Windows 上最常见的"删不掉"，去掉再试一次
            os.chmod(p, stat.S_IWRITE)
            func(p)

    for root, dirs, names in os.walk(path, topdown=False):
        for name in names:
            p = os.path.join(root, name)
            try:
                _force(p, os.remove)
            except OSError as e:
                raise StorageError("删除 %s 失败：%s" % (p, e)) from e
            done += 1
            report(done, name)
        for name in dirs:
            p = os.path.join(root, name)
            try:
                _force(p, os.rmdir)
            except OSError as e:
                raise StorageError("删除目录 %s 失败：%s" % (p, e)) from e
    try:
        os.rmdir(path)
    except OSError as e:
        raise StorageError("删除 %s 失败：%s" % (path, e)) from e
    report(done, "")
    return done, total_bytes


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


def migrate(target: str, on_line=None, on_progress=None) -> str:
    """把数据根整体搬到 `target`，返回新根。

    只做三件事：检查目标 → 复制 → 切指针。**旧数据不在这里删**——
    由调用方在确认新位置可用之后再广播 `remove_old()`，避免"复制成功但删旧失败"
    被当成整体失败（那时数据其实已经安全在新位置了）。

    `on_progress(done, total, name)` 会被复制阶段调用；总数是复制前数出来的，
    复制完的核对阶段不再回调（那时数字已经没意义，只有一句"正在核对"）。
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
        copy_tree(src, tgt, on_progress=on_progress)
    except StorageError as e:
        raise StorageError("复制失败：%s\n\n原目录未被改动，可以稍后重试。" % e) from e

    emit("正在核对复制结果…")
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


def remove_old(old_root: str, keep_pointer: bool = True,
               on_progress=None) -> bool:
    """删掉搬走之后的旧目录内容。失败返回 False（不影响新位置）。

    `keep_pointer=True` 时保留 `location.json`——它就在默认根里，
    删掉就找不到新位置了。

    `on_progress` 的 `total` 是这条分支自己的文件数（旧根下各子项之和），
    不是"要删多少字节"；删旧根本身也是几十秒级的活，同样必须能报进度。
    """
    old_root = os.path.abspath(old_root)
    if not os.path.isdir(old_root):
        return True
    ptr = config.pointer_path()
    ok = True
    targets = []          # [(路径, 是否目录, 该子项文件数)]
    total = 0
    for name in os.listdir(old_root):
        p = os.path.join(old_root, name)
        if keep_pointer and os.path.normcase(p) == os.path.normcase(ptr):
            continue
        if os.path.isdir(p) and not os.path.islink(p):
            n = tree_usage(p)[0]     # 数一次，删的时候直接用
            targets.append((p, True, n))
        else:
            n = 1
            targets.append((p, False, 1))
        total += n
    done = 0

    for p, is_dir, n in targets:
        try:
            if is_dir:
                # 子项逐个报进度：把 done 累计成"全旧根的第几个文件"
                base = done

                def _sub(inner_done, _inner_total, _name, base=base):
                    if on_progress is not None:
                        on_progress(base + inner_done, total, "")

                remove_tree(p, on_progress=_sub if on_progress else None)
                done += n
            else:
                os.remove(p)
                done += 1
                if on_progress is not None:
                    on_progress(done, total, os.path.basename(p))
        except (OSError, StorageError):
            ok = False
    return ok


def clear_cache(on_progress=None, on_line=None) -> tuple[int, int]:
    """清缓存：只删 profile 与 logs，保留设置、安装与登录态之外的配置。

    返回 (删除文件数, 释放字节)。注意 profile 里存着 WebEngine 的登录态，
    删了要重新登录——所以这一步在界面上要写清楚。

    2026-10-21：`rmtree` 换成带进度的 `remove_tree`，且整个函数跑在后台线程
    （调用方 `DataTaskWorker`）。数据根 727 MB 时这里原本要冻结十几秒。
    """
    emit = on_line or (lambda s: None)
    freed_files = freed_bytes = 0
    # devtools-profile 也要清（2026-10-20 体检）：那是 Chromium 给开发者工具
    # 单独开的 profile，缓存在 `data_root()/devtools-profile`，以前不在清理范围内，
    # 越用越大，而用户在"清理缓存"里看不到它。
    names = ("profile", "devtools-profile", "logs", "cache")
    total = sum(tree_usage(os.path.join(config.data_root(), n))[0]
                for n in names)
    done = 0

    for name in names:
        p = os.path.join(config.data_root(), name)
        if not os.path.isdir(p):
            continue
        emit("清理 %s…" % name)
        try:
            # 每个分组的进度按"已清理文件数 / 总文件数"折算到全局
            base = done

            def _sub(inner_done, _t, _n, base=base):
                if on_progress is not None:
                    on_progress(base + inner_done, total, "")
            n, b = remove_tree(p, on_progress=_sub if on_progress else None)
            done += n
            freed_files += n
            freed_bytes += b
        except StorageError as e:
            raise StorageError("清理 %s 失败：%s\n请先退出桌面壳再试。"
                               % (name, e)) from e
    return freed_files, freed_bytes


def clear_all(on_progress=None) -> tuple[int, int]:
    """清空全部数据：回到首次运行状态（会丢设置、登录态与私有 DSH）。

    保留位置指针本身，否则连"数据在哪"都记不住了。
    """
    root = config.data_root()
    files, total = tree_usage(root)
    # done 由 remove_old 内部按子项累计，这里传出去即可
    remove_old(root, keep_pointer=True, on_progress=on_progress)
    return files, total
