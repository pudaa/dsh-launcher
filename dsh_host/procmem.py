# -*- coding: utf-8 -*-
"""子进程内存采样（Windows / ctypes，不引入 psutil）。

为什么需要它（2026-10-21 老大问"WebEngine 占了 1.7 GB，是不是泄露了"）
--------------------------------------------------------------------
桌面壳的内存分成三块，任务管理器里只看到一个总数，无法判断该怀疑谁：

    DSH-Web.exe            我们的 Python + Qt 进程
    QtWebEngineProcess.exe Chromium：渲染进程（网页本身）、GPU、utility
    node.exe               DSH 服务（上游，不归我们管）

要用**数据**回答"有没有泄露"，必须能持续记录这三块各自的
WorkingSet 与 PrivateBytes。Windows 上这个信息只能靠 ctypes 走
`CreateToolhelp32Snapshot` + `GetProcessMemoryInfo`——加 psutil 依赖
会让 onefile 包再大几 MB，不值得。

判读方法（写给未来的自己）
--------------------------
- **WorkingSet 会被系统裁剪**：窗口最小化或系统内存紧张时，WS 会从 1.2 GB
  掉到十几 MB，而 PrivateBytes 几乎不动。看到 WS 骤降**不等于**内存被释放。
- 判断是否泄露看 **PrivateBytes 是否单调上涨**；判断"是不是真的在用"看 WS。
- `WS << Private` 说明这批内存是**冷页**（曾经分配过、现在没被访问），
  典型成因是 Chromium 的高水位不归还，而不是 JavaScript 里的活对象。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _processes() -> list[tuple[int, int, str]]:
    """(pid, ppid, exe 名) 列表。失败返回空表——采样失败绝不能影响主流程。"""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID),
                        int(entry.th32ParentProcessID),
                        entry.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def mem_of_pid(pid: int) -> tuple[float, float] | None:
    """(WorkingSet MB, PrivateBytes MB)；打不开就返回 None。"""
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return None
        return (pmc.WorkingSetSize / 1048576.0,
                pmc.PagefileUsage / 1048576.0)   # ≈ Private Bytes
    finally:
        k32.CloseHandle(h)


def _descendants(root_pid: int) -> dict[int, int]:
    """root_pid 的所有后代 → {pid: 父 pid}。"""
    procs = _processes()
    children: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for pid, ppid, name in procs:
        children.setdefault(ppid, []).append(pid)
        names[pid] = name
    out: dict[int, int] = {}
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        for c in children.get(cur, []):
            out[c] = cur
            stack.append(c)
    return out


def snapshot(root_pid: int | None = None) -> dict:
    """桌面壳整棵树的内存快照。

    返回 {
      "total_ws": MB, "total_private": MB,
      "groups": {"host": {...}, "webengine": {...}, "node": {...}, "other": {...}},
      "rows": [{pid,name,ws,private,group}...]
    }

    `root_pid` 缺省为当前进程。
    """
    root_pid = root_pid or os.getpid()
    kids = _descendants(root_pid)
    # 名字只取一次：`_processes()` 是一次快照遍历，别在每行里重复调它
    names = {p: n for p, _pp, n in _processes()}
    rows = []
    groups = {}
    for pid in kids:
        info = mem_of_pid(pid)
        if info is None:
            continue
        name = names.get(pid, "?")
        low = name.lower()
        if low.startswith("qtwebengine"):
            group = "webengine"
        elif low.startswith("node"):
            group = "node"
        else:
            group = "other"
        rows.append({"pid": pid, "name": name, "ws": info[0],
                     "private": info[1], "group": group})
        g = groups.setdefault(group, {"ws": 0.0, "private": 0.0, "count": 0})
        g["ws"] += info[0]
        g["private"] += info[1]
        g["count"] += 1

    host = mem_of_pid(root_pid) or (0.0, 0.0)
    groups["host"] = {"ws": host[0], "private": host[1], "count": 1}
    total_ws = host[0] + sum(r["ws"] for r in rows)
    total_private = host[1] + sum(r["private"] for r in rows)
    return {"total_ws": total_ws, "total_private": total_private,
            "groups": groups, "rows": rows}


def summary_line_from(snap: dict) -> str:
    """把一次 `snapshot()` 的结果压成一行（界面层用它，避免重复取样）。"""
    g = snap.get("groups") or {}

    def _g(key):
        v = g.get(key) or {"ws": 0.0, "private": 0.0, "count": 0}
        return "%.0f/%.0f(%d)" % (v["ws"], v["private"], v["count"])

    return ("内存 WS/Private MB — 合计 %.0f/%.0f ｜ 壳 %s ｜ 网页 %s ｜ node %s ｜ 其他 %s"
            % (snap.get("total_ws", 0), snap.get("total_private", 0),
               _g("host"), _g("webengine"), _g("node"), _g("other")))


def summary_line(root_pid: int | None = None) -> str:
    """一行摘要，直接进日志。"""
    return summary_line_from(snapshot(root_pid))
