# -*- coding: utf-8 -*-
"""OpenCode Zen 免费池体检。

用途：查清"此刻我这个出口还能不能用 Zen 免费模型"。
OpenCode 免费层的配额维度是**出口公网 IP × UTC 自然日**（2026-10-21 实测：
同一请求仅换出口 IP，代理侧 429、直连侧 200），与账号、DSH 会话都无关。
所以换节点有没有用，取决于出口 IP 换没换成"干净的"——这个脚本就是用来验的。

    python tools/zen_pool_probe.py                  # 只测直连
    python tools/zen_pool_probe.py --env-proxy      # 加上 HTTP_PROXY/HTTPS_PROXY
    python tools/zen_pool_probe.py --proxy http://127.0.0.1:7897 --proxy socks5h://...
    python tools/zen_pool_probe.py --all            # 直连 + 环境代理 + --proxy

只支持 HTTP(S) 代理；SOCKS 需要第三方库，本脚本不引入依赖，遇到会提示跳过。

注意：
- 每次探测会真发一次 `max_tokens=4` 的最小请求，消耗一点点额度；默认每个出口只探一次。
- 这是**诊断**工具，不重试、不轮换、不并发刷额度。
- Zen 对 429 返回 `Retry-After`（秒），脚本换算成本地时间直接给人看。
"""
import argparse
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ZEN_BASE = "https://opencode.ai/inference/v1"
ZEN_CHAT = ZEN_BASE + "/chat/completions"
ZEN_MODELS = ZEN_BASE + "/models"
VERSION_URL = "https://unpkg.com/opencode-ai@latest/package.json"
IP_URL = "https://www.cloudflare.com/cdn-cgi/trace"
UA_FALLBACK = "opencode/1.19.3"
DEFAULT_MODEL = "deepseek-v4.1-flash-free"
TIMEOUT = 45

# 探测用模型：优先这几款（零保留、地域限制少）；beam-free 明确不向中国大陆提供，
# 拿它当探针会把「地域被拒」误读成「额度用尽」。
PREFERRED_MODELS = ("deepseek-v4.1-flash-free", "glm-5.3-flash-free", "mimo-v2.6-flash-free")
REGION_LOCKED = {"beam-free"}

ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_CTX = ssl.create_default_context()


def _opener(proxy):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 显式清空，避免环境变量悄悄把 direct 变成代理
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _request(url, proxy=None, data=None, headers=None, timeout=TIMEOUT):
    """返回 (status, headers, body_text)。非 2xx 不抛，交给调用方判读。"""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    opener = _opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return exc.code, dict(exc.headers or {}), body
    except Exception as exc:  # 连接/SSL/超时
        return None, {}, f"{type(exc).__name__}: {exc}"


def egress_ip(proxy=None):
    status, _, body = _request(IP_URL, proxy=proxy, headers={"user-agent": "curl/8"})
    if status == 200:
        for line in body.splitlines():
            if line.startswith("ip="):
                return line[3:].strip()
    return None


def opencode_version(proxy=None):
    status, _, body = _request(VERSION_URL, proxy=proxy, headers={"accept": "application/json"})
    if status == 200:
        try:
            version = json.loads(body).get("version")
            if isinstance(version, str) and version:
                return version
        except ValueError:
            pass
    return UA_FALLBACK.split("/")[-1]


def _session_id():
    """opencode 的 id：12 位 hex 是反转毫秒时间戳，后接 14 位 base62。"""
    value = ~(int(time.time() * 1000) * 0x1000 + 1)
    stamp = "".join(f"{(value >> (40 - 8 * i)) & 0xFF:02x}" for i in range(6))
    return "ses_" + stamp + "".join(random.choice(ID_ALPHABET) for _ in range(14))


def _tool(name, prop):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "probe",
            "parameters": {"type": "object", "properties": {prop: {"type": "string"}}, "required": [prop]},
        },
    }


def zen_headers(version):
    headers = {
        "user-agent": f"opencode/{version}",
        "content-type": "application/json",
        "accept": "text/event-stream",
        "x-opencode-project": "global",
        "x-opencode-client": "cli",
    }
    session = _session_id()
    for name in ("x-opencode-session", "x-opencode-session-id", "x-session-affinity", "x-session-id"):
        headers[name] = session
    return headers


def pick_model(proxy=None, version=UA_FALLBACK.split("/")[-1]):
    """挑一个当前在架的 free 模型；目录接口本身不计额度。"""
    status, _, body = _request(ZEN_MODELS, proxy=proxy, headers=zen_headers(version))
    if status == 200:
        try:
            free = [str(m["id"]) for m in json.loads(body).get("data", []) if str(m.get("id", "")).endswith("-free")]
            for wanted in PREFERRED_MODELS:
                if wanted in free:
                    return wanted, free
            for candidate in free:
                if candidate not in REGION_LOCKED:
                    return candidate, free
            if free:
                return free[0], free
        except (ValueError, KeyError, TypeError):
            pass
    return DEFAULT_MODEL, []


def zen_probe(proxy, model, version):
    """发一次最小请求，判读免费池状态。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 4,
        "stream": True,
        "tools": [
            _tool("bash", "command"),
            _tool("read", "file_path"),
            _tool("edit", "file_path"),
            _tool("glob", "pattern"),
            _tool("grep", "pattern"),
        ],
    }).encode("utf-8")
    status, headers, text = _request(ZEN_CHAT, proxy=proxy, data=body, headers=zen_headers(version))
    result = {"status": status, "kind": "", "detail": "", "retry_after": None}
    if status is None:
        result["kind"] = "network"
        result["detail"] = text[:120]
        return result
    if status == 200:
        result["kind"] = "ok"
        return result
    if status == 429:
        result["kind"] = "exhausted"
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw and str(raw).strip().isdigit():
            result["retry_after"] = int(str(raw).strip())
        result["detail"] = _error_message(text)
        return result
    if status == 403:
        detail = _error_message(text)
        region = "RegionError" in detail or "not available in your country" in detail
        result["kind"] = "region" if region else "blocked"
        result["detail"] = detail
        return result
    result["kind"] = f"http{status}"
    result["detail"] = _error_message(text)
    return result


def _error_message(text):
    try:
        payload = json.loads(text)
    except ValueError:
        return text.strip().replace("\n", " ")[:120]
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        kind = err.get("type") or ""
        msg = err.get("message") or ""
        return f"{kind}: {msg}"[:160] if kind else msg[:160]
    return str(payload)[:160]


def describe(result, now=None):
    now = now or time.time()
    kind = result["kind"]
    if kind == "ok":
        return "可用"
    if kind == "exhausted":
        if result["retry_after"]:
            resume = time.localtime(now + result["retry_after"])
            return "已用尽，%s 恢复（剩 %s）" % (
                time.strftime("%m-%d %H:%M", resume),
                _human_delta(result["retry_after"]),
            )
        return "已用尽（上游未给恢复时间）"
    if kind == "region":
        return "地区受限（换个模型或节点再试，与额度无关）"
    if kind == "blocked":
        return "被拒：" + (result["detail"] or "403")
    if kind == "network":
        return "连不上：" + (result["detail"] or "")
    return kind + " " + (result["detail"] or "")


def _human_delta(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def collect_exits(args):
    exits = []
    seen = set()

    def push(label, proxy):
        key = proxy or "direct"
        if key in seen:
            return
        seen.add(key)
        if proxy and not proxy.lower().startswith(("http://", "https://")):
            print(f"  跳过 {proxy}：只支持 HTTP(S) 代理（SOCKS 需要额外依赖）", file=sys.stderr)
            return
        exits.append((label, proxy))

    if not args.no_direct:
        push("direct", None)
    if args.all or args.env_proxy:
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = os.environ.get(name)
            if value:
                push(f"env:{name}", value)
    for proxy in args.proxy or []:
        push(proxy, proxy)
    return exits


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="查 OpenCode Zen 免费池在各出口的可用状态")
    parser.add_argument("--proxy", action="append", help="要测的 HTTP(S) 代理，可重复")
    parser.add_argument("--env-proxy", action="store_true", help="额外测环境变量里的代理")
    parser.add_argument("--all", action="store_true", help="直连 + 环境代理 + --proxy")
    parser.add_argument("--no-direct", action="store_true", help="不测直连")
    parser.add_argument("--model", default="", help="指定模型 id，默认自动挑一个在架的 free 模型")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    exits = collect_exits(args)
    if not exits:
        parser.error("没有可测的出口")

    version = opencode_version(exits[0][1])
    model = args.model or pick_model(exits[0][1], version)[0]

    rows = []
    for label, proxy in exits:
        ip = egress_ip(proxy)
        result = zen_probe(proxy, model, version)
        rows.append({
            "exit": label,
            "proxy": proxy or "direct",
            "egress_ip": ip,
            "status": result["status"],
            "kind": result["kind"],
            "retry_after": result["retry_after"],
            "detail": result["detail"],
            "describe": describe(result),
        })

    if args.json:
        print(json.dumps({"model": model, "opencode_version": version, "exits": rows}, ensure_ascii=False, indent=2))
        return 0

    print(f"模型 {model}   opencode/{version}   配额维度：出口 IP × UTC 日（北京 08:00 重置）\n")
    width = max(len(r["exit"]) for r in rows) + 2
    for row in rows:
        ip = row["egress_ip"] or "-"
        print(f"{row['exit']:<{width}}{ip:<18}{row['describe']}")

    usable = [r for r in rows if r["kind"] == "ok"]
    exhausted = [r for r in rows if r["kind"] == "exhausted"]
    print()
    if usable:
        print("可用出口：" + "、".join(r["exit"] for r in usable))
    elif exhausted:
        print("这些出口的免费池都已用尽。等 Retry-After 指的时间，或换一个真正不同的出口 IP。")
    else:
        print("没有可用出口，但不是额度问题（原因见上一列）。")
    distinct = {r["egress_ip"] for r in rows if r["egress_ip"]}
    if len(rows) > 1 and len(distinct) <= 1:
        print("提示：多个出口探到同一个 IP —— 代理可能按域名分流，访问 opencode.ai 时其实没走它。")
    print("注：IP 取自 Cloudflare trace，仅供对照；判断能不能用请看状态列。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
