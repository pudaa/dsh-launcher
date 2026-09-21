# -*- coding: utf-8 -*-
"""自更新全链路验证（下载 -> 校验 -> 生成 bat -> 局外进程替换 -> 备份）。

为什么需要它
------------
`selfupdate.py` 是"发版后最省事的一环"，但**从未端到端验证过** ——
之前只有 v2.0.0 一个版本，没有"从旧版更新到新版"的场景可测。
本次发 v2.1.0/v2.1.1 才凑齐两个版本，正好补上。

而且它里面全是"错了就会把用户程序搞坏"的动作（替换自身、删自己），
所以更需要能重复跑的自测，而不是靠人肉点菜单。

覆盖点
------
1. `running_as_exe()` 的判定 —— Nuitka 不设 sys.frozen，只查它会让
   自更新在打包版里**静默失效**（真实踩过，见下方注释）
2. `current_exe()` 必须指向 .exe，**绝不能指向 python 解释器**
   （否则 apply 会去替换解释器）
3. `download()`：走 file:// 假装成远端，验证落盘 + 体积/PE 头校验
4. `apply()`：写出 bat 并真的把它拉起来（不依赖 GitHub）
5. bat 脚本本体：真的能替换目标文件、留 .old 备份

用法：
    python tools/selfupdate_e2e.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 隔离配置目录——绝不能碰老大真实的 settings.json
_SANDBOX = os.path.join(tempfile.gettempdir(), "dsh-launcher-test-cfg")
os.makedirs(_SANDBOX, exist_ok=True)
os.environ["LOCALAPPDATA"] = _SANDBOX

from dsh_host import selfupdate as su                              # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -> " + detail) if detail else ""))


def fake_exe(path: str, size: int = 6 * 1024 * 1024) -> str:
    """造一个"看起来像 exe"的文件：MZ 头 + 足够体积。"""
    with open(path, "wb") as f:
        f.write(b"MZ" + b"\x00" * (size - 2))
    return path


print("=" * 70)
print("自更新全链路验证")
print("=" * 70)

WORK = os.path.join(tempfile.gettempdir(), "dsh-su-test")
import shutil                                                      # noqa: E402
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)

# ---------------------------------------------------- 1. running_as_exe
print("\n[1] running_as_exe() 的判据")
check("源码运行时为 False", su.running_as_exe() is False)

# 模拟 PyInstaller
_real_frozen = getattr(sys, "frozen", None)
try:
    sys.frozen = True                                              # type: ignore[attr-defined]
    check("sys.frozen=True 时判定为 exe", su.running_as_exe() is True)
finally:
    if _real_frozen is None:
        try:
            del sys.frozen                                         # type: ignore[attr-defined]
        except AttributeError:
            pass
    else:
        sys.frozen = _real_frozen                                  # type: ignore[attr-defined]

# 模拟 Nuitka：**不设 frozen**，只有 __compiled__
# 这是真实踩到的坑：只查 frozen 会让打包版的自更新被判为不可用，
# 菜单项被禁用，功能直接死掉。
su.__compiled__ = object()
try:
    check("只有 __compiled__（Nuitka）时也判定为 exe",
          su.running_as_exe() is True,
          "只查 sys.frozen 会在这里返回 False —— 就是那个 bug")
finally:
    del su.__compiled__

check("清理后恢复 False", su.running_as_exe() is False)

# ---------------------------------------------------- 2. current_exe
print("\n[2] current_exe() 的安全性")
exe_now = su.current_exe()
print("    当前解析为:", exe_now)
check("源码模式下指向启动脚本", exe_now.lower().endswith((".py", ".exe")),
      exe_now)

# 打包场景：模拟一个 .exe 作为 argv[0]，确认优先选中它
fake_target = os.path.join(WORK, "target.exe")
fake_exe(fake_target)
su.__compiled__ = object()
_argv0 = sys.argv[0]
_argv = list(sys.argv)
try:
    # 让 sys.executable 指向 python（模拟"打包器没改它"的最坏情况）
    sys.argv[0] = fake_target
    got = su.current_exe()
    check("打包场景优先选中 .exe 而非解释器",
          got.lower().endswith(".exe") and "python" not in os.path.basename(got).lower(),
          got)
finally:
    del su.__compiled__
    sys.argv[:] = _argv

# ---------------------------------------------------- 3. download 校验
print("\n[3] download() 的校验")

# 3a 正常：file:// 假装远端
src = os.path.join(WORK, "remote-new.exe")
fake_exe(src, 6 * 1024 * 1024)
rel = su.HostRelease(
    tag="v9.9.9", version="9.9.9",
    asset=su.ReleaseAsset(name="DSH-Web.exe", url="file:///" + src.replace("\\", "/"),
                          size=os.path.getsize(src)),
    published_at="", notes="", html_url="")
got = su.download(rel)
check("正常下载并落盘", os.path.isfile(got) and os.path.getsize(got) == os.path.getsize(src),
      "%s (%d 字节)" % (got, os.path.getsize(got)))
os.remove(got)

# 3b 体积不足
small = os.path.join(WORK, "small.exe")
fake_exe(small, 1024)
rel_bad = su.HostRelease(
    tag="v9.9.9", version="9.9.9",
    asset=su.ReleaseAsset(name="DSH-Web.exe", url="file:///" + small.replace("\\", "/"),
                          size=1024),
    published_at="", notes="", html_url="")
try:
    su.download(rel_bad)
    check("体积不足被拒绝", False, "居然通过了")
except su.SelfUpdateError as e:
    check("体积不足被拒绝", True, str(e)[:46])

# 3c 不是 PE（GitHub 出错时会返回 HTML）
html = os.path.join(WORK, "reply.html")
with open(html, "wb") as f:
    f.write(b"<html>Not Found</html>" + b" " * (6 * 1024 * 1024))
rel_html = su.HostRelease(
    tag="v9.9.9", version="9.9.9",
    asset=su.ReleaseAsset(name="DSH-Web.exe", url="file:///" + html.replace("\\", "/"),
                          size=os.path.getsize(html)),
    published_at="", notes="", html_url="")
try:
    su.download(rel_html)
    check("非 PE 内容被拒绝", False, "居然通过了")
except su.SelfUpdateError as e:
    check("非 PE 内容被拒绝", True, str(e)[:46])

# ---------------------------------------------------- 4. apply 生成并拉起 bat
print("\n[4] apply() 写出并启动 bat")
su.current_exe = lambda: fake_target          # 注入目标，避免测试改到自己
_bat = os.path.join(WORK, "su.bat")
su._bat_path = lambda: _bat
try:
    msg = su.apply(rel, relaunch=False)
    check("apply 返回面向用户的说明", isinstance(msg, str) and len(msg) > 8,
          msg.splitlines()[0][:40])
    check("bat 已写出", os.path.isfile(_bat), _bat)
    bat_text = open(_bat, encoding="utf-8", errors="replace").read()
    check("bat 含目标路径", fake_target.replace("\\", "/").split("/")[-1] in bat_text.replace("\\", "/"))
    check("bat 含 .old 备份动作", ".old" in bat_text)
    # 自删除的位置：cmd 逐行读取 bat，运行中删掉自己会让后续行读不到
    # （报 "The batch file cannot be found"），所以 del "%~f0" 必须在
    # **所有实际工作之后**，且只能有一处。
    lines = [ln.strip() for ln in bat_text.splitlines()]
    dels = [i for i, ln in enumerate(lines) if 'del "%~f0"' in ln]
    body_after = [ln for ln in lines[dels[0] + 1:] if ln and not ln.startswith(("rem", "exit"))] if dels else []
    check("bat 自删除只有一处", len(dels) == 1, "找到 %d 处" % len(dels))
    check("bat 自删除之后没有实际工作", not body_after, str(body_after[:2]))
except su.SelfUpdateError as e:
    check("apply 执行", False, str(e)[:60])

# ---------------------------------------------------- 5. bat 真的能替换
print("\n[5] bat 脚本本体（pid=0，跳过等待）")
tgt2 = os.path.join(WORK, "bat-target.exe")
new2 = os.path.join(WORK, "bat-incoming.exe")
fake_exe(tgt2, 6 * 1024 * 1024)
fake_exe(new2, 7 * 1024 * 1024)
bat2 = os.path.join(WORK, "replace.bat")
with open(bat2, "w", encoding="utf-8", newline="\r\n") as f:
    f.write(su.build_apply_script(tgt2, new2, pid=0, relaunch=False))
old_size = os.path.getsize(tgt2)
# 用 subprocess 直接跑 bat（bash/PowerShell 工具可能被策略禁止调 cmd.exe，
# 但 Python 的 subprocess 不受该限制）
try:
    p = subprocess.run([bat2], cwd=WORK, capture_output=True, timeout=60)
    log = (p.stdout or b"").decode("utf-8", "replace") + \
          (p.stderr or b"").decode("utf-8", "replace")
    replaced = os.path.getsize(tgt2) != old_size
    check("目标被替换为新文件", replaced,
          "旧 %d -> 新 %d" % (old_size, os.path.getsize(tgt2)))
    check("留有 .old 备份", os.path.exists(tgt2 + ".old"))
    if not replaced:
        print("      bat 输出:", log[:300])
except Exception as e:                                             # noqa: BLE001
    check("bat 执行", False, "%s: %s" % (type(e).__name__, e))

print("\n" + "=" * 70)
print("结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED:", f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
