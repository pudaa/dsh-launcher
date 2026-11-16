# -*- coding: utf-8 -*-
"""版本库：让私有安装可以同时存在多个版本，切换只改一个指针。

为什么要有这个
--------------
在此之前私有安装只有**一份**，"更新"和"回退"都是对这同一份做覆盖式安装。
后果是：回退其实是重装（实测 6 分钟 656 个包），而且预发布版本一旦被 npm
unpublish 就再也回不去；用户也没法"装了但不用"——点一次更新，旧版当场消失。

2026-11-16 老大提的场景正是后者：新版本会打断插件，用户希望停在旧版，
但启动器不给他这个机会。

结构
----
    versions/
      0.2.2-rc.1/            ← 每个版本一个完整的 npm prefix
        node_modules/@deepseek-ai/dsh/...
      0.2.2-rc.3/
      index.json             ← 清单 + 当前指向

**不用软链**。Windows 上创建符号链接要么要管理员权限、要么要开发者模式，
这两条都不能指望用户满足。改成让 `DshInstall.prefix` 指向选中的目录——
安装机制本身一行不用改，那些踩过坑的修复（子依赖漂移、半升级治理）原样继承。

与旧机制的关系
--------------
`dsh_source` 的 auto/private/system 全部保持不变。版本库是通过
`config.dsh_version_prefix` 接入的：有值且目录有效时，`candidate_bin_js()`
会优先返回它。这样**不启用版本库的机器行为完全不变**。
"""
from __future__ import annotations

import json
import os
import shutil
import time

from . import config, contract

#: index.json 的结构版本，将来改格式时用来判断要不要迁移
INDEX_VERSION = 1


def versions_dir() -> str:
    return os.path.join(config.data_root(), "versions")


def index_path() -> str:
    return os.path.join(versions_dir(), "index.json")


def bin_js_in(prefix: str) -> str:
    """在这个 prefix 下找 bin.js。

    两种布局都可能遇到：Windows 的 npm 把包放在 `<prefix>/node_modules`，
    Linux 放在 `<prefix>/lib/node_modules`。都试一遍比按平台猜可靠。
    """
    for rel in (("node_modules",), ("lib", "node_modules")):
        p = os.path.join(prefix, *rel, "@deepseek-ai", "dsh", "lib", "bin.js")
        if os.path.isfile(p):
            return p
    return ""


def version_of(prefix: str) -> str:
    """读这个 prefix 下 DSH 的版本号；读不出来返回空串。"""
    for rel in (("node_modules",), ("lib", "node_modules")):
        p = os.path.join(prefix, *rel, "@deepseek-ai", "dsh", "package.json")
        try:
            with open(p, encoding="utf-8") as f:
                return str(json.load(f).get("version") or "")
        except (OSError, ValueError):
            continue
    return ""


def dir_size_mb(path: str) -> int:
    """目录体积（MB，向上取整）。算不出来返回 0，不要让它挡住列表渲染。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        return 0
    return int((total + 1048575) // 1048576)


# ------------------------------------------------------------------ index.json

def load_index() -> dict:
    try:
        with open(index_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("versions", [])
            data.setdefault("active", "")
            return data
    except (OSError, ValueError):
        pass
    return {"version": INDEX_VERSION, "active": "", "versions": []}


def save_index(data: dict) -> None:
    os.makedirs(versions_dir(), exist_ok=True)
    data["version"] = INDEX_VERSION
    tmp = index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, index_path())


def list_installed() -> list[dict]:
    """已安装的版本，按安装时间倒序。

    以**磁盘目录**为准，不信 index.json 里记的——用户可能在资源管理器里
    手删过某个版本目录，那时清单里还留着一条，点"切换"才发现是空的。
    所以这里扫目录，再用 index.json 补充元数据（安装时间、健康标记）。
    """
    meta = {v.get("version"): v for v in load_index().get("versions", [])}
    out = []
    root = versions_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        ver = version_of(path)
        if not ver:
            continue                     # 不完整的目录（装了一半、或不是 DSH）
        m = meta.get(name, {})
        out.append({
            "version": ver,
            "dir": name,
            "path": path,
            "size_mb": m.get("size_mb") or dir_size_mb(path),
            "installed_at": m.get("installed_at", ""),
            "health": m.get("health", ""),     # "" | "ok" | "broken"
            "note": m.get("note", ""),
        })
    out.sort(key=lambda x: x.get("installed_at") or "", reverse=True)
    return out


def active_version() -> str:
    return str(load_index().get("active") or "")


def prefix_for(version: str) -> str:
    return os.path.join(versions_dir(), version)


def install(version: str, node: str, npm_cli: str | None = None,
            on_line=None, timeout: int = 900) -> tuple[bool, str]:
    """把指定版本装进版本库（**不影响当前在用的那份**）。

    关键点：装到 `versions/<版本>` 这个独立 prefix，用的是现有的
    `updater.install_version()`——半升级治理、卸载残留清理那些修复
    原样继承，这里只是把"装到哪"参数化。
    """
    from . import updater
    prefix = prefix_for(version)
    os.makedirs(prefix, exist_ok=True)
    staging = contract.DshInstall(
        node=node, bin_js="", pkg_root="", prefix=prefix,
        npm_cli=npm_cli, version=version, source="版本库")
    ok, text = updater.install_version(staging, version, on_line=on_line,
                                       timeout=timeout, fresh=True)
    if not ok:
        return False, text
    got = version_of(prefix)
    if not got:
        return False, "装完了但读不到版本号，目录可能不完整：%s" % prefix
    data = load_index()
    rows = [v for v in data.get("versions", []) if v.get("version") != got]
    rows.append({"version": got,
                 "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "size_mb": dir_size_mb(prefix)})
    data["versions"] = rows
    save_index(data)
    return True, text


def remove(version: str) -> tuple[bool, str]:
    """删掉一个版本目录。当前在用的那个不允许删。"""
    if version == active_version():
        return False, "这是当前使用的版本，先切到别的版本再删。"
    path = prefix_for(version)
    if not os.path.isdir(path):
        return False, "目录不存在：%s" % path
    try:
        shutil.rmtree(path)
    except OSError as e:
        return False, "删除失败（可能有进程在占用）：%s" % e
    data = load_index()
    data["versions"] = [v for v in data.get("versions", [])
                        if v.get("version") != version]
    save_index(data)
    return True, ""


def activate(version: str) -> tuple[bool, str]:
    """把当前指向切到某个已安装版本。

    **只改指针，不动文件。** 调用方负责先停服务、切换后重启——
    Windows 上 node_modules 被占用时切换会失败，所以顺序不能反。

    写入 `config.dsh_version_prefix` 是关键：`candidate_bin_js()` 看到它就
    用这个 prefix 解析安装，于是 `resolve_install()` 之后的整条链路
    （启动、版本复验、更新）全都自动指向新版本，不需要各处判断。
    """
    path = prefix_for(version)
    if not bin_js_in(path):
        return False, "这个版本的目录不完整，找不到 bin.js：%s" % path
    data = load_index()
    data["active"] = version
    for v in data.get("versions", []):
        if v.get("version") == version:
            v["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_index(data)
    config.set(dsh_version_prefix=path)
    # 必须清掉安装位置缓存，否则解析链路会继续用上一个版本的 bin_js，
    # 表现是"切了但没生效"，而且不报错。
    contract.invalidate_install_cache()
    return True, ""


def _copy_tree(src: str, dst: str) -> None:
    """整树复制。Windows 上优先用 robocopy。

    为什么不用 shutil.copytree：一份安装有 656 个包、655 MB，copytree 单线程
    复制实测超过两分钟；robocopy 多线程快好几倍。这不只是"慢"的问题——
    中途被打断会留下半成品目录，而半成的 node_modules 比没有更难查。
    """
    if os.name == "nt":
        import subprocess
        rc = subprocess.run(
            ["robocopy", src, dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS",
             "/MT:8", "/R:1", "/W:1"],
            capture_output=True, text=True).returncode
        # robocopy 的返回码：0-7 都算成功（1=复制了文件，3=复制+额外）
        if rc <= 7:
            return
        raise OSError("robocopy 退出码 %d" % rc)
    shutil.copytree(src, dst)


def adopt_current(prefix: str) -> tuple[bool, str]:
    """把"当前正在用的那份私有安装"纳入版本库。

    用户已经装好了一个版本、现在想开始用版本库时走这条。**用复制而非移动**：
    移动会破坏 `dsh_source=private` 的既有行为，而且中途失败会让用户连
    原本能用的那份都没了。复制多占 250 MB，但可回退。
    """
    ver = version_of(prefix)
    if not ver:
        return False, "这个目录里读不到 DSH 版本号：%s" % prefix
    dst = prefix_for(ver)
    if os.path.isdir(dst):
        return activate(ver)
    try:
        os.makedirs(versions_dir(), exist_ok=True)
        _copy_tree(prefix, dst)
    except OSError as e:
        shutil.rmtree(dst, ignore_errors=True)
        return False, "复制失败：%s" % e
    if not version_of(dst):
        shutil.rmtree(dst, ignore_errors=True)
        return False, "复制不完整（读不到版本号），已清理"
    data = load_index()
    if not any(v.get("version") == ver for v in data.get("versions", [])):
        data.setdefault("versions", []).append({
            "version": ver,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "size_mb": dir_size_mb(dst),
            "note": "从现有私有安装导入",
        })
    save_index(data)
    return activate(ver)


def is_enabled() -> bool:
    """版本库当前是否在用（配置里有指向且目录有效）。"""
    p = str(config.get("dsh_version_prefix") or "")
    return bool(p) and bool(bin_js_in(p))


def disable() -> None:
    """退出"用版本库"，回到 dsh_source 的常规解析。"""
    config.set(dsh_version_prefix="")


# ------------------------------------------------------------------ 自测

def selftest_checks() -> list[str]:
    """不装任何东西，只验路径与清单逻辑。"""
    failures: list[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                               ("  " + extra) if extra else ""))
        if not ok:
            failures.append(name)

    import tempfile
    check("版本库目录在数据根下",
          os.path.dirname(versions_dir()) == config.data_root())
    check("index 路径正确", index_path().endswith("index.json"))

    # 在临时目录里演一遍清单读写，不碰用户数据
    old_root = config.data_root
    tmp = tempfile.mkdtemp(prefix="vtest-")
    try:
        config.data_root = lambda: tmp          # type: ignore[assignment]
        check("空库读出默认结构", load_index() == {"version": INDEX_VERSION,
                                                   "active": "", "versions": []})
        save_index({"active": "1.2.3", "versions": [{"version": "1.2.3"}]})
        got = load_index()
        check("写入后可读回", got.get("active") == "1.2.3")
        check("清单文件真的落盘", os.path.isfile(index_path()))
        # 目录里有东西但读不出版本号 -> 不应出现在列表里
        os.makedirs(os.path.join(versions_dir(), "bogus"), exist_ok=True)
        check("跳过不完整目录", list_installed() == [])
    finally:
        config.data_root = old_root             # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)
    check("不启用时不谎报", is_enabled() in (True, False))
    return failures
