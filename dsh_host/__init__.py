# -*- coding: utf-8 -*-
"""dsh_host —— DSH 桌面壳的宿主适配层。

分层约定（重要，新增代码请遵守）：

    L2  updater.py   更新器      只走官方 npm 通道
        provision.py 环境供给    首次运行补齐 Node 与 DSH
    L1  compat.py    适配表      版本区间 → 探测顺序与降级策略（数据，不是代码）
    L0  contract.py  契约层      唯一允许知道 DSH 内部结构的地方

L0 之上的一切代码都不应该再出现 DSH 的私有路径、私有文件名、私有日志格式。
"""

HOST_VERSION = "2.0.0"
