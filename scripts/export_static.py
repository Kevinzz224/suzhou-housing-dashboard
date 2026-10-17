#!/usr/bin/env python3
"""把 SQLite 里的计算结果导出成静态 JSON，供 GitHub Pages 等纯静态托管使用。

用法:
    python export_static.py --db ../data/housing.db --out ../web/data.json

输出结构（和 serve.py 的 API 一一对应，前端可无缝切换）:
    {
      "generated_at": "...",
      "stats": {...},
      "projects": [...],
      "project_details": {项目名: {...}},
      "trend": [...],
      "changes": [...],
      "subscriptions": [...]
    }

设计要点:
- 与 serve.py 复用同一套聚合逻辑（import 它，避免两份实现漂移）
- 静态托管没有后端，所以把所有可能点开项目的详情预计算好
- 订阅变更只能走仓库文件 / GitHub Issue，前端只读
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import serve  # noqa: E402  复用聚合逻辑


def build(db_path, days=30):
    conn = sqlite3.connect(db_path)
    # 兼容老库：补列
    for tbl, col, ddl in [
        ("listings", "sold_at", "TEXT DEFAULT NULL"),
        ("status_changes", "pushed_at", "TEXT DEFAULT NULL"),
        ("snapshots", "sold", "INTEGER DEFAULT 0"),
        ("snapshots", "alive", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    conn.commit()

    projects = serve.api_projects(conn)
    details = {}
    for p in projects:
        d = serve.api_project(conn, p["project"], days)
        if d:
            details[p["project"]] = d

    try:
        subs = serve.q(conn, "SELECT * FROM subscriptions WHERE active=1 ORDER BY id DESC")
    except sqlite3.OperationalError:
        subs = []
    try:
        changes = serve.q(conn, """SELECT sc.*, l.location FROM status_changes sc
            LEFT JOIN listings l ON l.site=sc.site AND l.key_id=sc.key_id
            ORDER BY sc.id DESC LIMIT 50""")
    except sqlite3.OperationalError:
        changes = []

    payload = {
        "generated_at": serve.now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "stats": serve.api_stats(conn),
        "projects": projects,
        "project_details": details,
        "trend": serve.api_trend(conn, days),
        "changes": changes,
        "subscriptions": subs,
    }
    conn.close()
    return payload


def main():
    ap = argparse.ArgumentParser(description="导出静态 JSON")
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--subs-file", default=None,
                    help="额外合并的订阅文件（GitHub 场景由 Issue 解析生成）")
    args = ap.parse_args()

    payload = build(args.db, args.days)

    # 合并外部订阅来源（静态托管下订阅存在仓库里）
    if args.subs_file and os.path.exists(args.subs_file):
        try:
            extra = json.load(open(args.subs_file, encoding="utf-8"))
            have = {s.get("keyword") for s in payload["subscriptions"]}
            for kw in extra:
                k = kw if isinstance(kw, str) else kw.get("keyword")
                if k and k not in have:
                    payload["subscriptions"].append({
                        "id": len(payload["subscriptions"]) + 1, "keyword": k,
                        "note": "from repo", "created_at": "", "active": 1})
        except Exception as e:
            print(f"[warn] 合并订阅失败: {e}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(args.out)
    print(f"[export] {args.out}  ({size/1024:.1f} KB)")
    print(f"  项目 {len(payload['projects'])} 个 | 详情 {len(payload['project_details'])} 份"
          f" | 快照点 {len(payload['trend'])} | 变化 {len(payload['changes'])}"
          f" | 订阅 {len(payload['subscriptions'])}")
    print(f"  在售 {payload['stats']['alive']} 套 / 已售 {payload['stats']['sold']} 套")


if __name__ == "__main__":
    main()
