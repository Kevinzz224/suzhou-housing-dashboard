#!/usr/bin/env python3
"""免费代理池：拉取 -> 并发验证 -> 冷却 -> 取用。

用法:
    python proxy_pool.py refresh                 # 拉取+验证，更新池
    python proxy_pool.py get [--target host] [N] # 取 N 个可用代理
    python proxy_pool.py status                  # 池状态

设计要点:
- 验证只测"能不能连上目标域名"，免费代理对中国政务站基本无用，
  所以正式爬取应优先直连，代理只作兜底（见 SKILL.md 的坑一节）。
"""
import argparse
import concurrent.futures as cf
import json
import os
import time
import urllib.request
import urllib.error

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy_state.json")
COOLDOWN_SECONDS = 1800  # 失败代理冷却 30 分钟

SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://api.openproxylist.xyz/http.txt",
    "https://proxylist.geonode.com/api/proxy-list?limit=100&page=1&sort_by=lastChecked&sort_type=desc",
]


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def harvest():
    """从所有源抓代理，返回去重后的 ip:port 列表。"""
    found = set()
    for url in SOURCES:
        try:
            text = _fetch(url)
        except Exception as e:
            print(f"[harvest] {url[:60]} -> {e}")
            continue
        if url.endswith(".txt") or "displayproxies" in url:
            for line in text.splitlines():
                line = line.strip()
                if line and ":" in line and line[0].isdigit():
                    found.add(line)
        else:  # geonode JSON
            try:
                data = json.loads(text)
                for item in data.get("data", []):
                    found.add(f"{item['ip']}:{item['port']}")
            except Exception:
                pass
    print(f"[harvest] 共收集 {len(found)} 个候选")
    return sorted(found)


def check(proxy, target_host=None, timeout=5):
    """验证单个代理。默认连 example.com；传 target_host 时测目标站连通性。"""
    url = f"http://{target_host}/" if target_host else "http://example.com/"
    handler = urllib.request.ProxyHandler({"http": f"http://{proxy}", "https": f"http://{proxy}"})
    opener = urllib.request.build_opener(handler)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500  # 有响应就算通
    except Exception:
        return False


def verify(proxies, target_host=None, workers=40, timeout=5, limit=30):
    """并发验证，返回前 limit 个可用的。"""
    alive = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check, p, target_host, timeout): p for p in proxies}
        for fut in cf.as_completed(futs):
            if fut.result():
                alive.append(futs[fut])
    alive.sort()
    print(f"[verify] 可用 {len(alive)}/{len(proxies)}" + (f" (target={target_host})" if target_host else ""))
    return alive[:limit]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"good": [], "bad": {}, "updated": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def cmd_refresh(args):
    state = load_state()
    now = time.time()
    # 冷却中的不再试
    cooled = {p: t for p, t in state["bad"].items() if now - t < COOLDOWN_SECONDS}
    candidates = [p for p in harvest() if p not in cooled and p not in state["good"]]
    if not candidates:
        print("[refresh] 没有新候选（可能都在冷却），复用旧池")
        return
    alive = verify(candidates, target_host=args.target, workers=args.workers,
                   timeout=args.timeout, limit=args.limit)
    state["good"] = sorted(set(state["good"]) | set(alive))
    for p in candidates:
        if p not in alive:
            state["bad"][p] = now
    state["updated"] = now
    save_state(state)
    print(f"[refresh] 池内可用 {len(state['good'])} 个")


def cmd_get(args):
    state = load_state()
    good = state.get("good", [])
    if not good:
        print("[]")
        return
    for p in good[: args.n]:
        print(p)


def cmd_status(args):
    state = load_state()
    age = (time.time() - state.get("updated", 0)) / 60
    print(f"可用 {len(state.get('good', []))} 个，冷却中 {len(state.get('bad', {}))} 个，更新于 {age:.0f} 分钟前")


def main():
    ap = argparse.ArgumentParser(description="免费代理池")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("refresh", help="拉取并验证新代理")
    p.add_argument("--target", default=None, help="目标主机名（验证时测它而不是 example.com）")
    p.add_argument("--workers", type=int, default=40)
    p.add_argument("--timeout", type=float, default=5)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("get", help="输出可用代理")
    p.add_argument("n", type=int, nargs="?", default=1)
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("status", help="池状态")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
