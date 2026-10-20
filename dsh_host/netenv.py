# -*- coding: utf-8 -*-
"""网络环境检查：代理是否会挡住 DSH 的账号功能。

为什么需要它（2026-10-20 实测）
------------------------------
官方文档 `docs/user/guide/network-proxy.zh.md` 写明：

    DSH 会把自身的出站请求——模型调用、web 搜索、页面抓取、走 HTTP 的
    MCP 服务器——都经由标准代理环境变量所指定的代理发出。
    **DSH 不读取操作系统的代理设置。**

而本机实测（无认证，只看状态码）：

    chat.deepseek.com/auth-api/v0/users/current
    platform.deepseek.com/dsh/authorize（登录授权页，实测同样 403）
        直连 429（通，只是限流）    走代理 403（被拒）
    api.deepseek.com/models
        直连 401（通，只是缺 key）

也就是说：家里开着代理、`NO_PROXY` 又是空的，DSH 的账号请求就会撞墙。
界面上的表现极具误导性——**登录状态和余额正常显示，只有「账号资料」显示
不可用**，控制台报一条 `[deepseek-account] account read failed`。
用户完全看不出这和代理有关，所以我们主动提示，而不是等他自己猜。
"""
from __future__ import annotations

import os

#: 这些域名走代理会被拒（实测），必须直连
_DIRECT_HOSTS = ("deepseek.com",)


def _env(*names: str) -> str:
    """按顺序取第一个非空环境变量（大小写两种写法都要看）。"""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return ""


def proxy_hint() -> str:
    """代理可能挡住账号接口时返回一句提示；没有风险时返回空串。

    判据刻意保守：只有"确实设了代理"且"NO_PROXY 没覆盖 deepseek 域名"
    才提示。没设代理，或已经排除的，一个字都不多说。
    """
    proxy = _env("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    if not proxy:
        return ""
    no_proxy = _env("NO_PROXY", "no_proxy").lower()
    if any(h in no_proxy for h in _DIRECT_HOSTS):
        return ""
    return ("检测到本机设置了网络代理，但 DeepSeek 的域名没有排除在外。\n\n"
            "DSH 只会走环境变量里配的代理（它不读系统的代理设置），"
            "而账号相关的请求经代理会被拒绝——表现是「账号资料暂不可用」，"
            "登录状态和余额却显示正常，很容易误判成登录过期。\n\n"
            "要排除的话，两个域名都得写：\n"
            "NO_PROXY=platform.deepseek.com,chat.deepseek.com\n"
            "然后重启桌面壳。"
            "（本项目不会替你改环境变量，这一步由你决定。）")
