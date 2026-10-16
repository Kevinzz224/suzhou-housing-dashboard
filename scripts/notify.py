#!/usr/bin/env python3
"""订阅监控 + 推送：查 status_changes 里没推过的变化，按订阅过滤后推送。

用法:
    python notify.py --db ../data/housing.db --webhook <企业微信webhook URL> [--dry-run]

推送渠道：企业微信群机器人 webhook（Markdown）。换渠道改 send() 一处即可。
"""
import argparse
import json
import os
import sqlite3
import time
import urllib.request


def push_wechat(webhook, markdown):
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": markdown}}).encode()
    req = urllib.request.Request(webhook, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def fetch_unsent(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT sc.id, sc.site, sc.key_id, sc.field, sc.old, sc.new, sc.changed_at,
               l.project, l.location
        FROM status_changes sc
        LEFT JOIN listings l ON l.site=sc.site AND l.key_id=sc.key_id
        WHERE sc.pushed_at IS NULL
        ORDER BY sc.id
    """)
    try:
        cols = [d[0] for d in cur.description]
    except Exception:
        cols = []
    return cols, cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description="订阅变化推送")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "housing.db"))
    ap.add_argument("--webhook", required=True, help="企业微信机器人 webhook URL")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    # 兼容老库：补 pushed_at 列
    try:
        conn.execute("ALTER TABLE status_changes ADD COLUMN pushed_at TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    cols, rows = fetch_unsent(conn)
    if not rows:
        print("[notify] 没有新变化")
        return

    # 按房源聚合，一条消息一堆变化
    lines = [f"**房产订阅提醒** ({time.strftime('%m-%d %H:%M')})"]
    for r in rows:
        rec = dict(zip(cols, r))
        name = rec.get("location") or rec.get("key_id")
        lines.append(f"- {name}：{rec['field']} {rec['old']} → **{rec['new']}**")
    content = "\n".join(lines)

    if args.dry_run:
        print(content)
    else:
        resp = push_wechat(args.webhook, content)
        print(f"[notify] 推送 {len(rows)} 条 -> {resp[:100]}")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            conn.execute("UPDATE status_changes SET pushed_at=? WHERE id=?", (now, r[0]))
        conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
