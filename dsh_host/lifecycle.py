# -*- coding: utf-8 -*-
"""服务的启动编排：决定"接管已有实例"还是"新起一个"。

为什么不在 contract 里
--------------------
`contract` 是契约层——它知道**怎么**起一个 DSH、怎么等它就绪、怎么停。
这里知道的是**策略**：该不该接管、端口冲突时退不退让、句柄怎么补全。
策略会变，契约不该跟着变。

也不放在界面层：这些判断和 Qt 一点关系都没有，放进 UI 会让
"什么时候需要 Qt"变得模糊（打包体积、无头自测都受影响）。
"""
from __future__ import annotations

import time

from . import contract
from .contract import DshError, ServiceHandle


def plan_service(inst: contract.DshInstall, requested: int, allow_fallback: bool,
                 logf: str) -> tuple[str, int]:
    """决定"接管已有服务"还是"新起一个"，返回 (mode, port)。

    mode: 'attach' 端口上已是自家 DSH，直接接管
          'launch' 需要新起，port 是目标端口（0 表示交给系统分配）
    """
    host = inst.profile.get("service_host", "127.0.0.1")
    if not requested:
        return "launch", 0
    if not contract.tcp_open(host, requested):
        return "launch", requested

    # 端口被占：先用 HTTP 指纹分辨是不是自家服务
    probe_url = contract.recover_url(inst, requested, logf)
    if contract.looks_like_dsh(probe_url):
        return "attach", requested
    if allow_fallback:
        return "launch", 0
    raise DshError(
        f"端口 {requested} 已被其他程序占用（响应不像 DSH）。\n"
        "可以改端口，或在设置里允许占用时自动改用系统分配的端口。"
    )

def attach_handle(inst: contract.DshInstall, port: int, logf: str) -> ServiceHandle:
    """为已在运行的服务构造句柄。PID 取自我们自己的状态文件——不猜。"""
    host = inst.profile.get("service_host", "127.0.0.1")
    state = contract.load_state() or {}
    pid = int(state.get("pid") or 0) if int(state.get("port") or 0) == port else 0
    url = contract.recover_url(inst, port, logf)
    token = ""
    if "token=" in url:
        token = url.split("token=", 1)[1]
    return ServiceHandle(install=inst, pid=pid, host=host, requested_port=port,
                         port=port, url=url, token=token, log_path=logf,
                         started_at=time.time(), proc=None)
