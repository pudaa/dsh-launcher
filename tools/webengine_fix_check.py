"""判断某个 QtWebEngine 构建是否已含 QTBUG-142165 的修复。

用途：每次 PySide6 / QtWebEngine 出新版本时，别凭版本号猜——跑这个脚本，
它读 DLL 的 PE 构建时间和崩溃点机器码，直接给结论。

判据（两条，任一不满足都别升）：
  1. DLL 的 PE 构建时间晚于修复 backport 合入 140-based 分支的时刻；
  2. 崩溃点那 96 字节机器码与 6.11.2 基线**不再逐字节相同**。

用法：
  python tools/webengine_fix_check.py                      # 检查当前装的 PySide6
  python tools/webengine_fix_check.py --wheel x.whl        # 检查一个 wheel
  python tools/webengine_fix_check.py --dll Qt6WebEngineCore.dll
  python tools/webengine_fix_check.py --json               # 给 agent 解析用

退出码：0 = 已含修复；1 = 不含；2 = 需要人工复核（指纹消失但时间对不上）；
        3 = 找不到 DLL / 文件损坏。

背景与完整取证过程见 docs/webengine-angle-crash.md。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import struct
import sys
import zipfile

# 三个 backport（6f1bf838 / a740890d / 6d308e34）合入 qtwebengine-chromium
# 的 140-based 分支的时刻（qt.gerrit.bot 留言，2026-10-08 17:56 UTC）。
# 早于这一刻构建的 DLL 在数学上不可能包含修复。
FIX_MERGED_UTC = _dt.datetime(2026, 10, 8, 17, 56, tzinfo=_dt.timezone.utc)

# 「未修复」指纹：崩溃点附近 96 字节机器码。
# 在 6.11.2 与 6.140.0 中逐字节相同（函数入口位置不同，只因链接顺序平移），
# 所以只要还能原样找到这段，就说明 ANGLE 的 InputLayoutCache 代码没被动过。
BASELINE_96 = bytes.fromhex(
    "7c2450488b88882f00004885ff751d4839b378050000743b488b0133d2ff9088000000"
    "4889b378050000eb27488b4708488b10483b93780500007417488b01ff90880000004c"
    "8b4f084d8b114c8993780500004c8b8c2408010000450fb6c648"
)

# 指纹在 6.11.2 位于文件偏移 0xd21aa3（约 13.8 MB）、在 6.140.0 位于 0xd23253，
# 都在 .text 段内。这里直接全文件扫：194 MB 走 bytes.find 也是毫秒级，而
# "96 字节完全匹配"这个条件已经足够严，不会在资源区误命中。
# （第一版把上限设成 8 MB 导致漏检，已修正——这类"范围设小"的错只会表现为
#  假阴性，不会报错，所以宁可扫全文件。）

VERDICT_FIXED = "FIXED"
VERDICT_NOT_FIXED = "NOT_FIXED"
VERDICT_SUSPICIOUS = "SUSPICIOUS"
VERDICT_ERROR = "ERROR"


def _pe_build_time(data: bytes) -> _dt.datetime | None:
    """读 PE 头里的 TimeDateStamp（链接时刻，UTC）。"""
    try:
        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
            return None
        ts = struct.unpack_from("<I", data, e_lfanew + 8)[0]
        if not ts:
            return None
        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    except (struct.error, IndexError, ValueError, OSError):
        return None


def _chromium_version(data: bytes) -> str | None:
    """尽力读 Chromium 版本串（如 140.0.7339.225），读不到就返回 None。"""
    m = re.search(rb"1[0-9][0-9]\.0\.[0-9]{3,5}\.[0-9]{1,4}", data)
    return m.group(0).decode("ascii", "ignore") if m else None


def _find_baseline(data: bytes) -> int | None:
    """返回基线机器码的文件偏移；找不到返回 None（= 代码变了）。"""
    pat = BASELINE_96[:16]
    start = 0
    while True:
        i = data.find(pat, start)
        if i < 0:
            return None
        if data[i:i + len(BASELINE_96)] == BASELINE_96:
            return i
        start = i + 1


def analyse(data: bytes, label: str) -> dict:
    """对一份 DLL 字节给出结论。纯函数，便于测试。"""
    build = _pe_build_time(data)
    off = _find_baseline(data)
    info = {
        "label": label,
        "size_mb": round(len(data) / 1048576, 1),
        "build_utc": build.strftime("%Y-%m-%d %H:%M:%S UTC") if build else None,
        "chromium": _chromium_version(data),
        "baseline_offset": ("0x%x" % off) if off is not None else None,
        "baseline_found": off is not None,
        "fix_merged_utc": FIX_MERGED_UTC.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    if off is not None:
        # 机器码一字未变 → 无论时间戳如何，都不含修复。
        info["verdict"] = VERDICT_NOT_FIXED
        info["reason"] = "崩溃点 96 字节机器码与 6.11.2 基线逐字节相同（代码未改）"
    elif build is not None and build < FIX_MERGED_UTC:
        # 指纹消失但构建时间在修复之前：可能是编译器/构建差异，不能当成已修。
        info["verdict"] = VERDICT_SUSPICIOUS
        info["reason"] = ("基线指纹消失，但构建时间早于修复合入时刻，"
                          "不能判定为已修，需实机复测")
    else:
        info["verdict"] = VERDICT_FIXED
        info["reason"] = ("基线指纹消失且构建时间晚于修复合入时刻，"
                          "很可能已含修复——请用 tools/probe_angle_backend.py "
                          "同款探针或长会话浸泡再确认")
    return info


def _read_dll_from_wheel(path: str) -> tuple[bytes, str]:
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith("qt6webenginecore.dll")]
        if not names:
            raise FileNotFoundError("%s 里没有 Qt6WebEngineCore.dll" % path)
        name = names[0]
        return z.read(name), "%s!%s" % (os.path.basename(path), name)


def _read_installed() -> tuple[bytes, str]:
    try:
        import PySide6  # noqa: PLC0415
    except ImportError:
        raise FileNotFoundError("当前解释器里没有 PySide6") from None
    dll = os.path.join(os.path.dirname(PySide6.__file__), "Qt6WebEngineCore.dll")
    if not os.path.isfile(dll):
        raise FileNotFoundError(
            "找不到 %s（6.12 起 WebEngine 拆成独立包，可能缺 PySide6_WebEngine）" % dll)
    with open(dll, "rb") as f:
        return f.read(), dll


_ICON = {VERDICT_FIXED: "[OK]   已含修复",
         VERDICT_NOT_FIXED: "[NO]   不含修复",
         VERDICT_SUSPICIOUS: "[??]   需人工复核",
         VERDICT_ERROR: "[ERR]  检查失败"}
_EXIT = {VERDICT_FIXED: 0, VERDICT_NOT_FIXED: 1,
         VERDICT_SUSPICIOUS: 2, VERDICT_ERROR: 3}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="检查 QtWebEngine 是否已含 QTBUG-142165（ANGLE InputLayoutCache）修复")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--wheel", help="一个 PySide6 的 wheel 文件")
    src.add_argument("--dll", help="直接指定 Qt6WebEngineCore.dll")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给 agent 解析）")
    args = ap.parse_args(argv)

    try:
        if args.wheel:
            data, label = _read_dll_from_wheel(args.wheel)
        elif args.dll:
            with open(args.dll, "rb") as f:
                data = f.read()
            label = args.dll
        else:
            data, label = _read_installed()
    except (OSError, FileNotFoundError, zipfile.BadZipFile) as e:
        info = {"label": args.wheel or args.dll or "（当前安装）",
                "verdict": VERDICT_ERROR, "reason": str(e)}
        if args.json:
            print(json.dumps(info, ensure_ascii=False))
        else:
            print(_ICON[VERDICT_ERROR], "|", info["reason"])
        return _EXIT[VERDICT_ERROR]

    info = analyse(data, label)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return _EXIT[info["verdict"]]

    print("DLL        : %s" % info["label"])
    print("大小       : %.1f MB" % info["size_mb"])
    print("PE 构建时间 : %s" % (info["build_utc"] or "读不到"))
    print("Chromium   : %s" % (info["chromium"] or "读不到"))
    print("修复合入时刻 : %s" % info["fix_merged_utc"])
    print("崩溃指纹   : %s" % (
        "找到（%s）" % info["baseline_offset"] if info["baseline_found"] else "未找到"))
    print("-" * 56)
    print(_ICON[info["verdict"]])
    print("理由       : %s" % info["reason"])
    if info["verdict"] == VERDICT_NOT_FIXED:
        print("建议       : 别升。等含修复的构建（判据见 docs/webengine-angle-crash.md）")
    return _EXIT[info["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
