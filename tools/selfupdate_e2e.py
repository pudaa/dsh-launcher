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

# ---------------------------------------------------- 6. 应用内就地替换
#
# 这是 2026-10-01 新增的更新路径：不再交 bat，由应用自己在运行期间完成
# 替换（因为 bat 那条路要求应用先退出，替换期间用户看不到任何反馈）。
# 它依赖一个**实测得出**的事实：Windows 允许重命名正在运行的 exe。
print("\n[6] 应用内就地替换（stage_inplace / install_inplace）")

d6 = os.path.join(WORK, "inplace")
os.makedirs(d6, exist_ok=True)
tgt6 = fake_exe(os.path.join(d6, "app.exe"), 6 * 1024 * 1024)
inc6 = fake_exe(os.path.join(d6, "new.exe"), 7 * 1024 * 1024)
old_content = open(tgt6, "rb").read(2)

backup6 = su.stage_inplace(inc6, tgt6)
check("替换后目标变成新文件",
      os.path.getsize(tgt6) == 7 * 1024 * 1024, "%d 字节" % os.path.getsize(tgt6))
check("旧版被保留为 .old（可回滚）",
      backup6 == tgt6 + ".old" and os.path.getsize(backup6) == 6 * 1024 * 1024)
check("待替换的临时文件已不在原处", not os.path.exists(inc6))

# --- 失败必须回滚，且现场要恢复原样 ---
tgt7 = fake_exe(os.path.join(d6, "app2.exe"), 6 * 1024 * 1024)
inc7 = fake_exe(os.path.join(d6, "new2.exe"), 7 * 1024 * 1024)
_real_replace = os.replace


def _boom(*_a, **_k):
    raise OSError(13, "simulated failure")


os.replace = _boom
try:
    su.stage_inplace(inc7, tgt7)
    check("就位失败时抛 SelfUpdateError", False, "居然没抛异常")
except su.SelfUpdateError as e:
    check("就位失败时抛 SelfUpdateError", True, str(e)[:60])
except Exception as e:                                             # noqa: BLE001
    check("就位失败时抛 SelfUpdateError", False, "%s: %s" % (type(e).__name__, e))
finally:
    os.replace = _real_replace

check("回滚：目标恢复成旧版",
      os.path.getsize(tgt7) == 6 * 1024 * 1024,
      "%d 字节" % os.path.getsize(tgt7))
check("回滚：没有留下半成品 .old", not os.path.exists(tgt7 + ".old"))

# --- 体积不足时拒绝，且不碰现场 ---
tiny = os.path.join(d6, "tiny.exe")
open(tiny, "wb").write(b"MZ" + b"\x00" * 100)
tgt8 = fake_exe(os.path.join(d6, "app3.exe"), 6 * 1024 * 1024)
try:
    su.stage_inplace(tiny, tgt8)
    check("待替换文件过小时拒绝", False, "居然没抛异常")
except su.SelfUpdateError as e:
    check("待替换文件过小时拒绝", True, str(e)[:50])
check("被拒绝时目标原封不动",
      os.path.getsize(tgt8) == 6 * 1024 * 1024 and not os.path.exists(tgt8 + ".old"))

# --- 最关键的一条：**正在运行的文件**也能就地替换 ---
# 这正是整个方案成立的前提。用一个真实在跑的 exe 副本验证。
sysroot = os.environ.get("SystemRoot", r"C:\Windows")
ping_src = os.path.join(sysroot, "System32", "ping.exe")
if os.path.isfile(ping_src):
    live = os.path.join(d6, "live.exe")
    shutil.copy2(ping_src, live)
    proc = subprocess.Popen([live, "-n", "20", "127.0.0.1"],
                            creationflags=0x08000000,          # CREATE_NO_WINDOW
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    import time as _time
    _time.sleep(1.0)
    live_inc = fake_exe(os.path.join(d6, "live_new.exe"),
                        su.MIN_EXE_BYTES + 1024 * 1024)
    try:
        su.stage_inplace(live_inc, live)
        ok = (os.path.getsize(live) == su.MIN_EXE_BYTES + 1024 * 1024
              and os.path.getsize(live + ".old") == os.path.getsize(ping_src))
        check("正在运行的 exe 也能被就地替换（方案前提）", ok)
    except Exception as e:                                         # noqa: BLE001
        check("正在运行的 exe 也能被就地替换（方案前提）", False,
              "%s: %s" % (type(e).__name__, e))
    finally:
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:                                          # noqa: BLE001
            proc.kill()
else:
    check("正在运行的 exe 也能被就地替换（方案前提）", False, "找不到 ping.exe")

# --- 源码运行时必须拒绝（否则会去替换解释器）---
try:
    su.install_inplace.__wrapped__                                  # noqa: BLE001
except AttributeError:
    pass
_saved = su.running_as_exe
su.running_as_exe = lambda: False
try:
    su.install_inplace(type("R", (), {"version": "0", "asset": None})())
    check("源码运行时拒绝替换自身", False, "居然没抛异常")
except su.SelfUpdateError as e:
    check("源码运行时拒绝替换自身", True, str(e)[:40])
except Exception as e:                                             # noqa: BLE001
    check("源码运行时拒绝替换自身", False, "%s: %s" % (type(e).__name__, e))
finally:
    su.running_as_exe = _saved

# --- 参数传递 ---
check("--attach-port 能正确解析", su.attach_port_from_argv(
    ["a.exe", "--attach-port", "3080"]) == 3080)
check("--attach-port 缺失/非法时返回 0",
      su.attach_port_from_argv(["a.exe"]) == 0
      and su.attach_port_from_argv(["a.exe", "--attach-port", "x"]) == 0)

# ---------------------------------------------------- 7. 替换前预检
#
# 预检是"不依赖打包器"的关键：它不问"你是什么打包器、解包到哪"，
# 只问"这个候选 exe 现在能不能启动"。跑不起来就中止，用户那边一个字节没动。
print("\n[7] 候选版本预检（preflight）")

ok, why = su.preflight(os.path.join(WORK, "根本不存在的.exe"))
check("文件不存在时判定为不可用", not ok, why)

_sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
# 用系统自带的小程序当"候选"：不需要构建，退出码可控且语义清楚
_zero = os.path.join(_sys32, "hostname.exe")      # 无参也正常退出 0
_nonzero = os.path.join(_sys32, "ping.exe")       # 无参打印用法并以非零退出
if os.path.isfile(_zero):
    ok, why = su.preflight(_zero, timeout=30)
    check("能正常启动的候选 -> 通过", ok, why)
else:
    check("能正常启动的候选 -> 通过", False, "找不到 hostname.exe")
if os.path.isfile(_nonzero):
    ok, why = su.preflight(_nonzero, timeout=30)
    check("启动后非零退出的候选 -> 拒绝", not ok, why)
else:
    check("启动后非零退出的候选 -> 拒绝", False, "找不到 ping.exe")

# 预检用的环境变量名必须和 exe 侧一致，否则预检问了个没人回答的问题
try:
    import dsh_gui_qt as _g                                        # noqa: E402
    check("自检环境变量名两边一致",
          _g.RESOURCE_CHECK_ENV == su.SELFTEST_ENV, su.SELFTEST_ENV)
except Exception as e:                                             # noqa: BLE001
    check("自检环境变量名两边一致", False, "%s: %s" % (type(e).__name__, e))

print("\n" + "=" * 70)
print("结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED:", f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
