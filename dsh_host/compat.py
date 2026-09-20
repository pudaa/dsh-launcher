# -*- coding: utf-8 -*-
"""L1 适配表：把"DSH 某个版本长什么样"变成数据，而不是散落在代码里的 if。

设计意图
--------
DSH 处于 developer preview，每一代都可能改日志格式、改启动参数、改私有文件名。
如果这些假设硬编码在 Python 里，一次破坏性更新就要重新编译打包 exe。

所以这里把假设收成一张表：

  1. BUILTIN_DEFAULTS —— 出厂默认，保证开箱可用
  2. BUILTIN_OVERRIDES —— 按版本区间覆盖，随 exe 一起发布
  3. %LOCALAPPDATA%\\DSH-Web\\compat.json —— 外部覆盖，**改完即生效，无需重新打包**

优先级（后者覆盖前者）：
    内置默认  <  内置区间覆盖  <  外部默认  <  外部区间覆盖

外部文件格式（只需要写要改的键）::

    {
      "defaults": { "ready_timeout": 180 },
      "overrides": [
        { "range": ">=0.1.7", "note": "假设 0.1.7 换了 URL 输出格式",
          "url_regexes": ["https?://127\\.0\\.0\\.1:\\d+/[^\\s]*"] }
      ]
    }
"""
from __future__ import annotations

import json
import os
import re

# ------------------------------------------------------------------ 版本号比较

_PRERELEASE_ORDER = {"alpha": 0, "beta": 1, "rc": 2}
_VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?")


def parse_version(text: str):
    """'0.1.6-alpha.2' → (0, 1, 6, 0, 0, 2)；正式版 prerelease 段排在预发布之后。"""
    m = _VER_RE.match((text or "").strip())
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre = m.group(4)
    if pre is None:
        return (major, minor, patch, 1, 0, 0)
    label, _, num = pre.partition(".")
    rank = _PRERELEASE_ORDER.get(label.lower(), -1)
    try:
        num = int(num)
    except ValueError:
        num = 0
    return (major, minor, patch, 0, rank, num)


def compare(a: str, b: str) -> int:
    """语义化比较：a>b 返回 1，a<b 返回 -1，不可解析返回 0。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return 0
    return (pa > pb) - (pa < pb)


def is_prerelease(version: str) -> bool:
    p = parse_version(version)
    return bool(p) and p[3] == 0


def satisfies(version: str, spec: str) -> bool:
    """适配表区间匹配：按 major.minor.patch 代数比较。

    与 semver 严格语义的差异是**有意为之**，理由很具体：

        DSH 至今所有 release 都是预发布（alpha/rc），严格语义下
        '0.1.6-alpha.2' 小于 '0.1.6'，于是范围 '>=0.1.6' 永远匹配不到
        任何版本——适配表就废了。而 0.1.6-alpha.2 显然属于 0.1.6 这一代。

    因此本函数只比较数字核心，忽略预发布后缀。若 spec 的右侧自带预发布
    限定（如 '>=0.1.6-alpha.1'），则退回严格序，把判断权交回给写表的人。

    spec 例：'*' / '>=0.1.6' / '>=0.1.5 <0.1.7' / '0.1.5-rc.2'
    """
    spec = (spec or "*").strip()
    if spec in ("*", ""):
        return True
    pv = parse_version(version)
    if pv is None:
        return False

    for part in spec.split():
        if not part:
            continue
        m = re.match(r"^(>=|<=|!=|==|=|>|<)\s*(.+)$", part)
        op, rhs = (m.group(1), m.group(2).strip()) if m else (None, part)
        pr = parse_version(rhs)
        if pr is None:
            return False

        strict = pr[3] == 0            # 右侧带预发布后缀 → 用严格序
        a = pv if strict else pv[:3]
        b = pr if strict else pr[:3]
        c = (a > b) - (a < b)

        if op is None and c != 0:
            return False
        if op == ">=" and c < 0:
            return False
        if op == "<=" and c > 0:
            return False
        if op == ">" and c <= 0:
            return False
        if op == "<" and c >= 0:
            return False
        if op in ("==", "=") and c != 0:
            return False
        if op == "!=" and c == 0:
            return False
    return True


# ------------------------------------------------------------------ 内置适配表

BUILTIN_DEFAULTS: dict = {
    # --- CLI 契约：官方 help 里公开暴露的参数，属于稳定接口 ---
    "cli": {
        "web": ["web"],
        "no_open": ["--no-open"],
        "port": ["--port"],
        "version": ["--version"],
    },

    # --- URL / 令牌发现 ---
    # 首选：我们 spawn 的子进程 stdout，实测输出形如
    #   dsh web: http://127.0.0.1:1744/?token=xxxx
    # 这是官方对用户可见的输出，比内部日志格式稳定
    "url_regexes": [
        r"https?://127\.0\.0\.1:\d+/?\?token=[A-Za-z0-9_\-.]+",
        r"https?://[\w.\-]+:\d+/[^\s\"']*token=[A-Za-z0-9_\-.]+",
    ],
    # 兜底：从诊断日志里捞令牌（0.1.6 起官方会把完整诊断写日志，格式可能变）
    "log_token_regexes": [r"token=([A-Za-z0-9_\-.]+)"],
    "log_url_regexes": [r"https?://127\.0\.0\.1:\d+/?\?token=[A-Za-z0-9_\-.]+"],

    # --- 就绪判定 ---
    "ready_probe": ["tcp"],          # tcp → 端口可连通即视为就绪
    "ready_timeout": 120,            # 秒，首次启动包含插件加载
    "poll_interval": 0.5,
    # TCP 判定就绪后，补读 URL 行的宽限期（DSH 写 URL 与开始监听几乎同时，
    # 若此刻还没读到就会拿到空 URL，首屏变 401 页）
    "url_grace": 3.0,

    # --- DSH_HOME 定位 ---
    "home_env": ["DSH_HOME"],
    "home_candidates": ["~/.dsh"],

    # --- 启动前需要清理的陈旧锁（私有文件名，属于"可选优化"而非硬依赖）---
    "stale_locks": [".credentials.yaml.lock"],

    "service_host": "127.0.0.1",
}

# 按版本区间的覆盖项。没有实际需要时保持为空——它存在的意义是"下次破坏性更新时
# 不用改 Python"，而不是现在就臆测一堆未来。
BUILTIN_OVERRIDES: list[dict] = [
    {
        "range": ">=0.1.6",
        "note": "0.1.6 起启动诊断会分类写入日志文件；stdout 的 'dsh web: <url>' 行保持可用",
    },
]


# ------------------------------------------------------------------ 组装 profile

_MERGE_KEYS = ("cli",)


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if k in _MERGE_KEYS and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def external_path() -> str:
    """外部覆盖文件位置——唯一权威定义在 config，这里只转一手。"""
    from . import config
    return config.compat_path()


def _load_external() -> tuple[dict, list]:
    try:
        with open(external_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, []
        d = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
        o = data.get("overrides") if isinstance(data.get("overrides"), list) else []
        return d, [x for x in o if isinstance(x, dict)]
    except (OSError, ValueError):
        return {}, []


def load_profile(version: str | None = None) -> dict:
    """按 DSH 版本组装生效的适配 profile。"""
    profile = dict(BUILTIN_DEFAULTS)

    for ov in BUILTIN_OVERRIDES:
        if version and satisfies(version, ov.get("range", "*")):
            profile = _merge(profile, {k: v for k, v in ov.items()
                                       if k not in ("range", "note")})

    ext_defaults, ext_overrides = _load_external()
    if ext_defaults:
        profile = _merge(profile, ext_defaults)
    for ov in ext_overrides:
        if version and satisfies(version, ov.get("range", "*")):
            profile = _merge(profile, {k: v for k, v in ov.items()
                                       if k not in ("range", "note")})
    return profile
