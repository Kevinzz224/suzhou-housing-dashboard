#!/usr/bin/env python3
"""按 queries.json 依次跑多个查询（零第三方依赖，CI 直接用）。

为什么要有这层：
    「园区 140-180㎡」是一条查询，「珺华赋 190+ 大户型」是另一条（用 txt_Pro
    锁定单盘，只 6 条）。把两者塞进一条查询会把面积放宽到 300，变成 1402 条
    71 页 —— 为了 6 套房多抓 65 页，得不偿失。
    每条查询各自有 query 指纹，消失判定天然隔离，不会互相误杀。

用法:
    python scripts/run_queries.py [--db data/housing.db] [--config scripts/queries.json]
                                   [--only 2] [--interval 1.2]
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    queries = cfg.get("queries") or []
    if not queries:
        raise SystemExit("[fatal] queries.json 里没有 queries")
    return cfg, queries


def build_args(python, site, db, params, interval, max_pages):
    args = [python, os.path.join(HERE, "webforms_crawl.py"),
            "--site", site, "--db", db, "--interval", str(interval)]
    if max_pages:
        args += ["--max-pages", str(max_pages)]
    for k, v in params.items():
        args += ["--param", f"{k}={v}"]
    return args


def main():
    ap = argparse.ArgumentParser(description="批量执行 queries.json 里的查询")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "housing.db"))
    ap.add_argument("--config", default=os.path.join(HERE, "queries.json"))
    ap.add_argument("--only", type=int, default=0, help="只跑第 N 条（1 起），0=全部")
    ap.add_argument("--interval", type=float, default=1.2)
    ap.add_argument("--max-pages", type=int, default=0, help="调试用，0=全部页")
    ap.add_argument("--keep-going", action="store_true",
                    help="某条失败也继续跑后面的（默认也继续，但最后仍返回非 0）")
    args = ap.parse_args()

    cfg, queries = load_config(args.config)
    site = cfg.get("site") or os.path.join(HERE, "sites", "suzhou.json")
    if not os.path.isabs(site):
        site = os.path.join(ROOT, site)

    failures = []
    for i, q in enumerate(queries, 1):
        if args.only and i != args.only:
            continue
        print(f"\n══════ [{i}/{len(queries)}] {q.get('name') or '未命名'} ══════",
              flush=True)
        cmd = build_args(sys.executable, site, args.db, q.get("params") or {},
                         args.interval, args.max_pages)
        rc = subprocess.call(cmd)
        if rc != 0:
            # 一条失败不能拖垮另一条：继续跑完，最后统一返回非 0 让 CI 标红。
            failures.append((i, q.get("name"), rc))
            print(f"[warn] 第 {i} 条失败 rc={rc}，继续下一条", flush=True)

    if failures:
        print("\n[fail] 失败的查询：", file=sys.stderr)
        for i, name, rc in failures:
            print(f"  #{i} {name} rc={rc}", file=sys.stderr)
        return 1
    print(f"\n[done] {len(queries)} 条查询全部完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
