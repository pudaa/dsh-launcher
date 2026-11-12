# -*- coding: utf-8 -*-
"""宿主自身的运行日志。

为什么单独一个模块
------------------
这个函数原来住在 `dsh_gui_qt.py` 里，而 `dsh_devtools.py` 又就地抄了一份——
因为 devtools 不能反向 import 入口（会成环）。两份实现迟早分叉，而且拆分
UI 层之后谁都想要它。所以收到 `dsh_host` 这一层，谁都能用，不成环。

命名注意：`config.host_log()` 返回的是**路径**，这里的 `host_log()` 是**写一行**。
两者同名不同物，import 时留意。
"""
from __future__ import annotations

import time

from . import config


def host_log(msg: str) -> None:
    """往宿主日志追加一行带时间戳的记录。

    为什么每条都要记：像"点了检查更新没反应"这类问题，事后唯一能查的就是
    这条日志里有没有对应的动作。凡是用户看得见的现象，背后都要留一条记录。

    写失败静默：日志是诊断手段，不该因为它自己出问题而影响主流程。
    """
    try:
        with open(config.host_log(), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass
