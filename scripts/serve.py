#!/usr/bin/env python3
"""纯标准库 HTTP API + 静态页服务（零第三方依赖，零 AI 调用）。

用法:
    python serve.py --db ../data/housing.db [--port 8899] [--host 127.0.0.1]

路由:
    GET  /                     -> index.html（移动端看板）
    GET  /api/projects         -> 项目聚合列表（在售/已售/均价/去化）
    GET  /api/project?name=xx  -> 单项目 7天/30天销售与去化明细
    GET  /api/trend?days=30    -> 全局快照趋势（存量曲线）
    GET  /api/subscriptions    -> 订阅列表
    POST /api/subscriptions    JSON {keyword, note} 新增订阅
    POST /api/subscriptions/delete  JSON {id} 取消订阅
    GET  /api/stats            -> 总览

设计要点:
- ThreadingHTTPServer 并发处理请求；每请求独立 SQLite 连接，避免串库
- 全部参数化 SQL，无注入面
- JSON 响应统一带 Access-Control-Allow-Origin: *（方便本地调试）
"""
import argparse
import json
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB = None


def now_cn():
    """统一的北京时间（UTC+8），返回 naive datetime 便于和库存时间字符串比较。

    Actions runner 是 UTC、本地是 UTC+8；sold_at/snapshots 都按北京时间落库，
    统计「今天 / 最近 N 天」也必须用同一个时区，否则会偏 8 小时。
    """
    return datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=8))).replace(tzinfo=None)


def parse_days(value, default=30):
    """解析 days 参数：非法值抛 ValueError，由 HTTP 层转成 400 而不是 500。"""
    if value in (None, ""):
        return default
    try:
        d = int(value)
    except (TypeError, ValueError):
        raise ValueError("days 必须是正整数")
    if d <= 0:
        raise ValueError("days 必须是正整数")
    return d


def project_of(location):
    """从"苏州工业园区八达街108号天熠星湖雅苑2幢702"提取楼盘名。

    坑：有些地址在「号」与「幢」之间**没有楼盘名**（如"万寿街168号3幢2402"，
    那是老小区/自建房），旧实现会把门牌号当成楼盘名或返回空 -> 显示"未知"。
    这里对这种情况回退到路名 + "（待补）"标注，而不是静默丢成"未知"。
    """
    if not location:
        return "未知"
    head = location.split("幢")[0] if "幢" in location else location
    m = re.match(r"^苏州工业园区(.*?号)(.*)$", head)
    if m:
        head = m.group(2)                      # 取「号」之后的部分
    head = re.sub(r"^\d+", "", head)            # 去掉纯数字楼栋号（如 "3"）
    head = re.sub(r"\d+$", "", head).strip()    # 去掉尾部数字
    if head and re.search(r"[\u4e00-\u9fa5A-Za-z]", head):
        return head
    # 号与幢之间无名称：用路名兜底，并标注待补
    fb = re.match(r"^苏州工业园区(.*?)\d*号", location)
    return (fb.group(1) + "（待补）") if fb else "未知"


def apply_alias(name, cfg_alias):
    """把备案名换成推广名（联网核实的映射，找不到就用备案名）。"""
    return (cfg_alias or {}).get(name) or name


_ALIAS_CACHE = None


def _load_alias():
    """读站点配置里的 name_alias（备案名 -> 推广名）。"""
    global _ALIAS_CACHE
    if _ALIAS_CACHE is None:
        try:
            cfg = json.load(open(os.path.join(HERE, "sites", "suzhou.json"), encoding="utf-8"))
            _ALIAS_CACHE = cfg.get("name_alias") or {}
        except Exception:
            _ALIAS_CACHE = {}
    return _ALIAS_CACHE


def _table_cols(conn, table):
    """取表的列名集合（用于兼容老库缺列的情况）。"""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def q(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def api_projects(conn, site=None):
    sql = "SELECT * FROM listings WHERE 1=1"
    args = []
    if site:
        sql += " AND site=?"
        args.append(site)
    rows = q(conn, sql, args)
    alias = _load_alias()
    agg = {}
    for r in rows:
        proj = project_of(r.get("location"))
        a = agg.setdefault(proj, {
            "project": proj, "display": apply_alias(proj, alias),
            "alive": 0, "sold": 0, "total": 0,
            "prices": [], "areas": [], "company": r.get("company") or "",
        })
        a["total"] += 1
        if r.get("sold_at"):
            a["sold"] += 1
        else:
            a["alive"] += 1
            if r.get("price"):
                a["prices"].append(r["price"])
            if r.get("area"):
                a["areas"].append(r["area"])
    out = []
    for a in agg.values():
        n = len(a["prices"]) or 1
        a["avg_price"] = round(sum(a["prices"]) / n)
        a["avg_area"] = round(sum(a["areas"]) / (len(a["areas"]) or 1), 1)
        a["sell_rate"] = round(a["sold"] / a["total"] * 100, 1) if a["total"] else 0
        del a["prices"], a["areas"]
        out.append(a)
    out.sort(key=lambda x: (-x["alive"], x["project"]))
    return out


def project_snapshots(conn, project, days=30):
    """按项目重算存量曲线：某个快照时刻 T，该项目在售数 =
    first_seen_at <= T 且 (sold_at 为空 或 sold_at > T) 的房源数。

    为什么不用 snapshots.alive：那是全站数字，直接画到项目页会串数据。
    """
    now = now_cn()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    times = [r["taken_at"] for r in
             q(conn, "SELECT DISTINCT taken_at FROM snapshots ORDER BY taken_at")
             if r["taken_at"] >= cutoff]
    if not times:
        return []
    rows = [r for r in q(conn, "SELECT location, first_seen_at, sold_at FROM listings")
            if project_of(r.get("location")) == project]
    if not rows:
        return []
    out = []
    for t in times:
        # 站点时间精度到秒，同一分钟内视为同一时刻
        alive = 0
        for r in rows:
            fs = r.get("first_seen_at") or ""
            sd = r.get("sold_at") or ""
            if fs and fs <= t and (not sd or sd > t):
                alive += 1
        out.append({"date": t[:10], "time": t[11:16], "alive": alive})
    # 去掉连续重复点会让曲线变粗，这里全保留（前端只画点，不影响）
    return out


def api_project(conn, name, days=30):
    rows = q(conn, "SELECT * FROM listings")
    rows = [r for r in rows if project_of(r.get("location")) == name]
    if not rows:
        return None
    now = now_cn()

    def within(r, d, field):
        v = r.get(field)
        if not v:
            return False
        try:
            return (now - datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S")).days < d
        except ValueError:
            return False

    def in_range(r, d):
        """最近 d 天内成交的：sold_at 落在窗口内；新房源：first_seen_at 落在窗口内。"""
        return within(r, d, "sold_at")

    # 快照序列 —— 必须按「本项目」重算，不能直接读全库 snapshots。
    # 曾经直接复用全站快照，导致每个项目详情页都画同一条全站曲线：
    # 君华雅筑卖掉 1 套，30 个项目全都显示 -1（用户报的真 bug）。
    # 这里用 listings 的 first_seen_at / sold_at 反推各时刻的在售数，
    # 好处是不需要新表，历史数据也能立刻算对。
    snaps = project_snapshots(conn, name, days)

    sold7 = [r for r in rows if in_range(r, 7)]
    sold30 = [r for r in rows if in_range(r, 30)]
    new7 = [r for r in rows if within(r, 7, "first_seen_at")]
    new30 = [r for r in rows if within(r, 30, "first_seen_at")]

    alive = [r for r in rows if not r.get("sold_at")]
    sold = [r for r in rows if r.get("sold_at")]
    prices = [r["price"] for r in alive if r.get("price")]
    areas = [r["area"] for r in alive if r.get("area")]

    # 去化速度：套/天
    sold7_rate = round(len(sold7) / 7, 2)
    sold30_rate = round(len(sold30) / 30, 2)
    # 按金额去化（万元/天）
    amt30 = sum((r.get("price") or 0) * (r.get("area") or 0) / 10000 for r in sold30)

    # 预计清盘天数
    days_to_clear = round(len(alive) / sold7_rate) if sold7_rate > 0 else None

    return {
        "project": name,
        "display": apply_alias(name, _load_alias()),
        "company": rows[0].get("company") or "",
        "alive": len(alive),
        "sold_total": len(sold),
        "listed_total": len(rows),
        "sell_rate": round(len(sold) / len(rows) * 100, 1) if rows else 0,
        "avg_price": round(sum(prices) / len(prices)) if prices else None,
        "min_price": round(min(prices)) if prices else None,
        "max_price": round(max(prices)) if prices else None,
        "avg_area": round(sum(areas) / len(areas), 1) if areas else None,
        "sold_7d": len(sold7),
        "sold_30d": len(sold30),
        "new_7d": len(new7),
        "new_30d": len(new30),
        "sold_rate_7d": sold7_rate,
        "sold_rate_30d": sold30_rate,
        "amount_30d_wan": round(amt30, 1),
        "days_to_clear": days_to_clear,
        # project_snapshots() 已按项目算好，直接透传
        "snapshots": snaps,
        "recent_sold": [
            {"location": r.get("location"), "area": r.get("area"),
             "price": r.get("price"), "sold_at": (r.get("sold_at") or "")[:10]}
            for r in sorted(sold, key=lambda x: x.get("sold_at") or "", reverse=True)[:10]
        ],
        "alive_list": [
            {"location": r.get("location"), "area": r.get("area"), "price": r.get("price"),
             "layout": r.get("layout")}
            for r in sorted(alive, key=lambda x: -(x.get("price") or 0))[:50]
        ],
    }


def api_trend(conn, days=30):
    """全局存量曲线。

    过滤 alive IS NULL 或 0 的快照：alive 列是后期加的，ALTER TABLE 给历史行
    填的默认值是 0，直接画会让曲线突然掉到 0（表现为"455 -> 0"的假暴跌）。
    历史行没有可用的在售数，只能剔除；宁可曲线短一点也不画假数据。
    """
    now = now_cn()
    cols = _table_cols(conn, "snapshots")
    has_new = "fetched" in cols and "dtot" in cols
    sel = ("taken_at, alive, sold, total, fetched, dtot" if has_new
           else "taken_at, alive, sold, total")
    snaps = q(conn, f"SELECT {sel} FROM snapshots ORDER BY taken_at")
    out = []
    for s in snaps:
        if s.get("alive") in (None, 0):
            continue
        if s["taken_at"][:10] < (now - timedelta(days=days)).strftime("%Y-%m-%d"):
            continue
        item = {"date": s["taken_at"][:10], "time": s["taken_at"][11:16],
                "alive": s["alive"], "sold": s["sold"],
                "dataset_total": s["dtot"] if has_new else s["total"]}
        out.append(item)
    return out


def api_stats(conn):
    alive = conn.execute("SELECT COUNT(*) FROM listings WHERE sold_at IS NULL").fetchone()[0]
    sold = conn.execute("SELECT COUNT(*) FROM listings WHERE sold_at IS NOT NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    last = conn.execute("SELECT MAX(taken_at) FROM snapshots").fetchone()[0]
    # 最近一次快照相对上一次的变化（只比较有效快照，避免和 alive=0 的历史行相减）
    snaps = [s for s in q(conn, "SELECT taken_at, alive, sold FROM snapshots"
                               " ORDER BY taken_at DESC LIMIT 10")
             if s.get("alive") not in (None, 0)]
    delta = None
    if len(snaps) >= 2:
        delta = {"alive": snaps[0]["alive"] - snaps[1]["alive"],
                 "sold": snaps[0]["sold"] - snaps[1]["sold"]}
    today = now_cn().strftime("%Y-%m-%d")
    sold_today = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE sold_at LIKE ?", (today + "%",)).fetchone()[0]
    return {"alive": alive, "sold": sold, "total": total, "last_crawl": last,
            "last_delta": delta, "sold_today": sold_today,
            "snapshot_count": conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静音默认日志
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        one = lambda k, d=None: (qs.get(k) or [d])[0]
        conn = sqlite3.connect(DB)
        try:
            if path in ("/", "/index.html"):
                return self._file(os.path.join(HERE, "..", "web", "index.html"),
                                  "text/html; charset=utf-8")
            if path == "/api/projects":
                return self._json(api_projects(conn, one("site")))
            if path == "/api/project":
                name = one("name", "")
                days = parse_days(one("days", "30"))
                data = api_project(conn, name, days)
                return self._json(data if data else {"error": "项目不存在"}, 404 if not data else 200)
            if path == "/api/trend":
                return self._json(api_trend(conn, parse_days(one("days", "30"))))
            if path == "/api/changes":
                rows = q(conn, """SELECT sc.*, l.location FROM status_changes sc
                    LEFT JOIN listings l ON l.site=sc.site AND l.key_id=sc.key_id
                    ORDER BY sc.id DESC LIMIT 50""")
                return self._json({"changes": rows})
            if path == "/api/subscriptions":
                return self._json(q(conn,
                    "SELECT * FROM subscriptions WHERE active=1 ORDER BY id DESC"))
            if path == "/api/stats":
                return self._json(api_stats(conn))
            self.send_error(404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)
        finally:
            conn.close()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        conn = sqlite3.connect(DB)
        try:
            if u.path == "/api/subscriptions":
                body = self._body_json()
                kw = (body.get("keyword") or "").strip()
                if not kw:
                    return self._json({"error": "keyword 不能为空"}, 400)
                conn.execute(
                    "INSERT INTO subscriptions (site, keyword, note, created_at) VALUES (?,?,?,?)",
                    ("suzhou-cansale", kw, (body.get("note") or "").strip(),
                     now_cn().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
                return self._json({"ok": True})
            if u.path == "/api/subscriptions/delete":
                body = self._body_json()
                sid = body.get("id")
                if sid in (None, ""):
                    return self._json({"error": "id 不能为空"}, 400)
                conn.execute("UPDATE subscriptions SET active=0 WHERE id=?", (sid,))
                conn.commit()
                return self._json({"ok": True})
            self.send_error(404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)
        finally:
            conn.close()


def main():
    global DB
    ap = argparse.ArgumentParser(description="房产看板 API 服务")
    ap.add_argument("--db", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    DB = os.path.abspath(args.db)
    # 确保表存在（空库也能起服务）
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS listings(site TEXT, key_id TEXT, first_seen_at TEXT,
        last_seen_at TEXT, project TEXT, location TEXT, company TEXT, usage TEXT,
        layout TEXT, area REAL, price REAL, total REAL, status TEXT, extra TEXT,
        sold_at TEXT DEFAULT NULL);
    CREATE TABLE IF NOT EXISTS status_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,
        site TEXT, key_id TEXT, field TEXT, old TEXT, new TEXT, changed_at TEXT,
        pushed_at TEXT DEFAULT NULL);
    CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,
        site TEXT, taken_at TEXT, query TEXT, total INTEGER, new INTEGER,
        changed INTEGER, sold INTEGER DEFAULT 0, alive INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,
        site TEXT, keyword TEXT, note TEXT, created_at TEXT, active INTEGER DEFAULT 1);
    """)
    conn.commit()
    # 老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
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
    conn.close()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"看板服务已启动: http://{args.host}:{args.port}  (DB: {DB})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
