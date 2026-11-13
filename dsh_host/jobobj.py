# -*- coding: utf-8 -*-
"""把后台服务绑到一个「进程退出就回收」的作业对象里。

要解决什么问题
--------------
服务进程是用 `DETACHED_PROCESS` 起的（2026-10-20 的决定），为的是"关掉窗口
之后后台任务还能继续跑"。代价是：**鲸坞进程一死，没人通知服务退场**。

正常退出有 `contract.stop()` 兜着（`taskkill /F /T` 杀整棵树，2026-11-13 实测
能带走插件子进程）。但下面这两条路没有任何收尸机制：

  · 进程崩溃（Python 层未捕获的异常、Qt 层面的 abort）
  · 任务管理器里"结束任务"、或任何形式的强杀

结果是一整棵 node 进程树（实测常驻 600 MB 上下）留在后台，用户完全看不见；
下次启动会被 `plan_service` 按端口认出来并接管，所以不表现为故障，
只表现为"这机器怎么越用越占内存"。

怎么解决
--------
Windows 的 Job Object 有一个开关 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`：
**当持有 Job 句柄的最后一个进程消失时，Job 里的所有进程一并终止。**

这正好对上需求：

  · 鲸坞正常退出   → `contract.stop()` 先杀；Job 随后关闭，无副作用（双保险）
  · 鲸坞崩溃/被强杀 → 进程消逝 → 句柄由内核回收 → Job 关闭 → 服务树被系统杀掉
  · 用户关掉窗口   → 鲸坞进程还在，Job 句柄还在 → 服务照常继续跑

也就是说，"服务能常驻"和"崩溃不留尸"可以同时成立，前提是 Job 句柄的存活
周期与鲸坞进程严格一致。**所以句柄必须存在模块级变量里**——一旦被 GC 回收，
Job 立刻关闭，服务会在启动成功的瞬间被杀掉。

唯一的例外是自更新重启：那条路用 `os._exit(0)` 故意把服务留给新实例接管，
Job 关掉会把服务一起带走。`detach_kill_on_close()` 就是给它用的——退出前把
这个限制摘掉，服务就能活到新实例接手。

失败时一律降级
--------------
Job 创建不了（权限、极老的系统、或进程已在不允许嵌套的 Job 里）不算致命：
退回"没有收尸保障"的状态，行为与改造前一致。这类改动不该成为启动失败的理由。
"""
from __future__ import annotations

import ctypes
import os
import sys

#: `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: `JobObjectExtendedLimitInformation`
_INFO_CLASS_EXTENDED = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),          # ULONG_PTR
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


#: **必须放在模块级**：句柄一被回收，Job 就关闭，里面的服务会被立刻杀掉。
_JOB = None
#: 缓存的理由字符串，给日志和诊断用。
_STATE: dict = {"ok": False, "why": "尚未初始化"}

_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def _kernel32():
    if os.name != "nt":
        raise OSError("Job Object 只存在于 Windows")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _apply_limit(job, flags: int) -> bool:
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = flags
    k32 = _kernel32()
    ok = k32.SetInformationJobObject(
        job, _INFO_CLASS_EXTENDED, ctypes.byref(info), ctypes.sizeof(info))
    return bool(ok)


def ensure() -> tuple[bool, str]:
    """确保有一个带 `KILL_ON_JOB_CLOSE` 的 Job，返回 (是否可用, 原因)。

    幂等：第二次调用直接返回上次的结论，不会重复创建。
    """
    global _JOB, _STATE
    if _JOB is not None:
        return True, _STATE.get("why", "")
    if os.name != "nt":
        _STATE = {"ok": False, "why": "非 Windows 平台"}
        return False, _STATE["why"]
    try:
        k32 = _kernel32()
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            err = ctypes.get_last_error()
            _STATE = {"ok": False, "why": "CreateJobObject 失败（错误 %d）" % err}
            return False, _STATE["why"]
        if not _apply_limit(job, _LIMIT_KILL_ON_JOB_CLOSE):
            err = ctypes.get_last_error()
            k32.CloseHandle(job)
            _STATE = {"ok": False,
                      "why": "设置 KILL_ON_JOB_CLOSE 失败（错误 %d）" % err}
            return False, _STATE["why"]
        _JOB = job
        _STATE = {"ok": True, "why": "已启用退出即回收"}
        return True, _STATE["why"]
    except Exception as e:                                     # noqa: BLE001
        _STATE = {"ok": False, "why": "%s: %s" % (type(e).__name__, e)}
        return False, _STATE["why"]


def attach(pid: int) -> bool:
    """把一个已在运行的进程纳入 Job。

    失败不抛异常：调用方（`contract.start`）只记一条日志就继续。
    最常见的失败原因是进程已经在某个**不允许嵌套**的 Job 里（Win7 的限制；
    Win8 以后支持嵌套）。那种环境下我们退回到改造前的行为。
    """
    ok, _why = ensure()
    if not ok or not pid:
        return False
    try:
        k32 = _kernel32()
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ph = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not ph:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(_JOB, ph))
        finally:
            k32.CloseHandle(ph)
    except Exception:                                          # noqa: BLE001
        return False


def detach_kill_on_close() -> bool:
    """摘掉 `KILL_ON_JOB_CLOSE`，让服务在鲸坞退出后继续活着。

    只有**自更新重启**需要它：那条路故意不停服务，把端口交给新实例接管
    （省掉"停服务 → 重启 → 等就绪"的七八秒）。如果不摘，`os._exit(0)` 会让
    Job 关闭，服务被系统杀掉，新实例得重新拉起——功能不受影响，但慢。
    """
    if _JOB is None:
        return False
    try:
        return _apply_limit(_JOB, 0)
    except Exception:                                          # noqa: BLE001
        return False


def status() -> dict:
    """给诊断/自测看的当前状态。"""
    return dict(_STATE, handle=bool(_JOB))


def selftest_checks() -> list[str]:
    """纯逻辑断言（不真的起进程）。"""
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                               ("  " + extra) if extra else ""))
        if not ok:
            failures.append(name)

    ok, why = ensure()
    check("能创建带 KILL_ON_JOB_CLOSE 的 Job", ok, why)
    check("句柄保存在模块级（回收即杀服务）", _JOB is not None or os.name != "nt")
    check("重复调用幂等", ensure()[0] == ok)
    st = status()
    check("状态可读", "why" in st and "handle" in st, str(st))
    # 结构体尺寸：Windows x64 上 JOBOBJECT_EXTENDED_LIMIT_INFORMATION 是 144 字节，
    # x86 是 112。算错会让 SetInformationJobObject 静默失败，所以钉住。
    size = ctypes.sizeof(_JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
    expect = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
    check("结构体尺寸正确", size == expect, "%d（期望 %d）" % (size, expect))
    check("非 Windows 上不假装可用", os.name == "nt" or not ok)
    return failures
