#!/usr/bin/env python3
"""通用 WebForms 政府站爬虫主程序。

用法:
    python webforms_crawl.py --site sites/suzhou.json --query cansale \
        --param ddl_qy=RD002 --param txt_Area1=140 --param txt_Area2=180 [--db data.db]

流程:
  1. GET 查询页，解析 __VIEWSTATE/__EVENTVALIDATION/__VIEWSTATEGENERATOR
  2. POST 查询条件 -> 拿结果页（含分页总数）
  3. 逐页 POST 翻页（__EVENTTARGET=分页控件）
  4. 解析表格行 -> UPSERT 进 SQLite，状态变化写 status_changes

设计要点:
  - 每轮查询都重新 GET 拿新 VIEWSTATE（旧的一提交就报"视图状态无效"）
  - 直连失败才走代理池（proxy_pool.py），政务站用代理通常更慢
  - 单页解析失败不影响其他页，失败页码记录下来轮重试
"""
import argparse
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def now_cn():
    """统一的北京时间戳。
    关键：GitHub Actions runner 是 UTC，本地是 UTC+8。两边都写本地时间会让
    快照混入两种时区，按字符串排序后曲线会前后颠倒（实测踩过）。
    所以一律换算成 UTC+8 再落库。"""
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def query_key(query):
    """查询条件的稳定序列化（key 排序），用于 listings/snapshots 对齐。
    dict 遍历顺序会随 --param 顺序变，直接 json.dumps 会把同一条件判成不同 query。"""
    return json.dumps(dict(query), ensure_ascii=False, sort_keys=True)


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def decode_page(raw):
    """把 curl 返回的 bytes 按页面声明的 charset 解码。

    不能用 subprocess.run(text=True)：它会按运行机 locale 解码（Windows 常是
    cp936，CI 是 UTF-8）。页面编码和 locale 不一致时中文会变乱码，row_regex
    全失配，最后表现成误导性的「查询返回空结果」。默认按 UTF-8，政务站大多如此。
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    head = raw[:4096].decode("latin-1", "ignore")
    m = re.search(r"<meta[^>]+charset=[\"']?([A-Za-z0-9_-]+)", head, re.I)
    enc = (m.group(1).lower() if m else "utf-8")
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def http(url, data=None, timeout=90, proxy=None, referer=None, cookie_jar=None):
    """HTTP 走 curl 子进程：政务站对 urllib 的默认头不友好（实测 POST 查询条件丢失、
    返回空表单），curl + 显式 UA/Referer/cookie jar 稳定得多。
    data=None 时 GET，否则 POST（--data-urlencode 保证 $ 和 + 不被吃掉）。"""
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-A", UA, "-L"]
    if referer:
        cmd += ["-e", referer]
    if cookie_jar:
        cmd += ["-c", cookie_jar, "-b", cookie_jar]
    if proxy:
        cmd += ["-x", f"http://{proxy}"]
    cmd.append(url)
    body_file = None
    if data is not None:
        # 关键坑：VIEWSTATE 有 ~19KB，Windows 命令行上限 32KB，直接 --data-urlencode
        # 会报 WinError 206（文件名或扩展名太长）。统一写 body 文件再 --data-binary @file。
        fd, body_file = tempfile.mkstemp(suffix=".txt", prefix="wf_body_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(urllib.parse.urlencode(data))
        cmd[-1:-1] = ["--data-binary", "@" + body_file,
                      "-H", "Content-Type: application/x-www-form-urlencoded"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else str(r.stderr)
            raise RuntimeError(f"curl 失败: {err[:200]}")
        return decode_page(r.stdout)
    finally:
        if body_file:
            try:
                os.unlink(body_file)
            except OSError:
                pass


def hidden_fields(page):
    """提取 WebForms 三个隐藏域。"""
    out = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        m = re.search(r'name="%s"[^>]*value="([^"]*)"' % name, page)
        if m:
            out[name] = html.unescape(m.group(1))
    return out


def visible_text(page):
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))


class Site:
    def __init__(self, cfg, cookie_jar=None):
        self.cfg = cfg
        self.name = cfg["name"]
        self.entry = cfg["entry"]            # 查询页 URL
        self.referer = cfg.get("referer")    # 有些站校验 Referer
        self.row_re = cfg["row_regex"]        # 结果表格行的正则（含所有字段分组）
        self.fields = cfg["fields"]          # 分组名 -> 列名
        self.key_field = cfg["key_field"]    # 主键字段名
        self.status_field = cfg.get("status_field")  # 状态字段名（可空）
        self.total_re = cfg.get("total_regex")       # "共 N 条"
        self.pager_target = cfg["pager_target"]      # __EVENTTARGET 值
        self.pager_field = cfg["pager_field"]        # 页码下拉框 name
        self.cookie_jar = cookie_jar

    def _http(self, data=None, proxy=None):
        return http(self.entry, data=data, proxy=proxy,
                    referer=self.referer, cookie_jar=self.cookie_jar)

    @staticmethod
    def qualify(params, prefix="ctl00$MainContent$"):
        """把用户给的短控件名补成 WebForms 全名。
        --param ddl_qy=RD002 -> ctl00$MainContent$ddl_qy=RD002
        （漏了前缀站点会当没填查询条件，静默返回全量数据——非常隐蔽的坑）"""
        out = {}
        for k, v in params.items():
            if k.startswith("__") or "$" in k:
                out[k] = v
            else:
                out[prefix + k] = v
        return out

    def fetch_page(self, params, page_idx=None, proxy=None, retries=3, prev_fields=None):
        """page_idx=None 表示查询（第一页）；否则翻到第 page_idx 页（从 0 开始）。
        prev_fields 传上一页返回的隐藏域（WebForms 翻页必须用最新 VIEWSTATE，
        用首次 GET 的旧 VIEWSTATE 会静默失败返回空表）。"""
        last_err = None
        for attempt in range(retries):
            try:
                if prev_fields is not None:
                    fields = dict(prev_fields)
                else:
                    page = self._http(proxy=proxy)
                    fields = hidden_fields(page)
                if not fields.get("__VIEWSTATE"):
                    raise RuntimeError("拿不到 VIEWSTATE（站点无响应或被挡）")
                data = dict(fields)
                # 只传非空参数：实测该站收到空串字段（txt_Pro= 等）会静默丢查询条件，
                # 返回空表单或全量，导致筛选失效。
                data.update({k: v for k, v in self.qualify(params).items()
                             if v not in ("", None)})
                if page_idx is None:
                    data["ctl00$MainContent$bt_select"] = "查询"
                else:
                    data["__EVENTTARGET"] = self.pager_target
                    data["__EVENTARGUMENT"] = ""
                    data["__LASTFOCUS"] = ""
                    data[self.pager_field] = str(page_idx)
                res = self._http(data=data, proxy=proxy)
                if page_idx is None and not self.parse_rows(res):
                    last_err = RuntimeError("查询返回空结果（站点抽风）")
                    time.sleep(2 + attempt * 2)
                    continue
                return res
            except Exception as e:
                last_err = e
                time.sleep(2 + attempt * 2)
        raise RuntimeError(f"{self.name}: 重试 {retries} 次仍失败 - {last_err}")

    def parse_rows(self, page):
        rows = []
        for m in re.finditer(self.row_re, page, re.S):
            g = m.groupdict()
            row = {}
            for col, grp in self.fields.items():
                v = g.get(grp)
                row[col] = html.unescape(re.sub(r"<[^>]+>", "", v)).strip() if v else ""
            rows.append(row)
        return rows

    def total_count(self, page):
        if not self.total_re:
            return None
        m = re.search(self.total_re, visible_text(page))
        return int(m.group(1)) if m else None


def db_upsert(conn, site_name, rows, site, query=None):
    """增量入库：新记录插入；已存在则更新 last_seen_at，状态变化记 status_changes。

    query 传入时把「本轮查询条件」写到 listings.query：消失判定必须只作用于同一
    查询条件覆盖的房源，否则换 --param 跑一次会把条件外房源误判成已售。"""
    cur = conn.cursor()
    now = now_cn()
    new, changed = 0, 0
    for r in rows:
        key = r.get(site.key_field)
        if not key:
            continue
        cur.execute("SELECT * FROM listings WHERE site=? AND key_id=?", (site_name, key))
        old = cur.fetchone()
        if old is None:
            cols = ["site", "key_id", "first_seen_at", "last_seen_at"] + list(r.keys())
            vals = [site_name, key, now, now] + [r[c] for c in r]
            if query is not None and "query" not in cols:
                cols.append("query")
                vals.append(query)
            cur.execute(
                f"INSERT INTO listings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                vals,
            )
            new += 1
        else:
            oldmap = dict(zip([d[0] for d in cur.description], old))
            if site.status_field and oldmap.get(site.status_field) != r.get(site.status_field):
                cur.execute(
                    "INSERT INTO status_changes (site,key_id,field,old,new,changed_at) VALUES (?,?,?,?,?,?)",
                    (site_name, key, site.status_field,
                     oldmap.get(site.status_field), r.get(site.status_field), now),
                )
                changed += 1
            # 已售房源重新出现 = 退房再售/撤销重挂（或上一轮误标）。UPDATE 列来自
            # r，不含 sold_at，旧代码会让 sold_at 永久残留；这里显式复活并记一条
            # availability 变化，去化统计才不会把「重新在售」继续算作已售。
            if oldmap.get("sold_at"):
                cur.execute("UPDATE listings SET sold_at=NULL WHERE site=? AND key_id=?",
                            (site_name, key))
                cur.execute(
                    "INSERT INTO status_changes (site,key_id,field,old,new,changed_at) VALUES (?,?,?,?,?,?)",
                    (site_name, key, "availability", "已售/下架", "可售", now),
                )
                changed += 1
            for col, val in r.items():
                cur.execute(f"UPDATE listings SET [{col}]=?, last_seen_at=? WHERE site=? AND key_id=?",
                            (val, now, site_name, key))
            if query is not None:
                cur.execute("UPDATE listings SET query=? WHERE site=? AND key_id=?",
                            (query, site_name, key))
    conn.commit()
    return new, changed


def init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS listings(
        site TEXT, key_id TEXT, first_seen_at TEXT, last_seen_at TEXT,
        project TEXT, location TEXT, company TEXT, usage TEXT, layout TEXT,
        area REAL, price REAL, total REAL, status TEXT, extra TEXT,
        query TEXT DEFAULT NULL,
        sold_at TEXT DEFAULT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_listings_key ON listings(site, key_id);
    CREATE TABLE IF NOT EXISTS status_changes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site TEXT, key_id TEXT, field TEXT, old TEXT, new TEXT, changed_at TEXT,
        pushed_at TEXT DEFAULT NULL
    );
    CREATE TABLE IF NOT EXISTS snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site TEXT, taken_at TEXT, query TEXT, total INTEGER, new INTEGER,
        changed INTEGER, sold INTEGER DEFAULT 0, alive INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS subscriptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site TEXT, keyword TEXT, note TEXT, created_at TEXT, active INTEGER DEFAULT 1
    );
    """)
    # 老库兼容：补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
    # 注意：所有新增列都必须登记在这里，否则老库升级后会缺列报错。
    for tbl, col, ddl in [
        ("listings", "sold_at", "TEXT DEFAULT NULL"),
        ("listings", "query", "TEXT DEFAULT NULL"),
        ("status_changes", "pushed_at", "TEXT DEFAULT NULL"),
        ("snapshots", "sold", "INTEGER DEFAULT 0"),
        ("snapshots", "alive", "INTEGER DEFAULT 0"),
        ("snapshots", "fetched", "INTEGER DEFAULT 0"),
        ("snapshots", "dtot", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def mark_sold(conn, site_name, seen_keys, now, query=None):
    """本轮没出现在可售列表里的房源 => 已售/下架。
    这是"去化速度"的数据来源：可售列表只显示未售房源，消失即成交。

    query 传入时只判定同一查询条件覆盖的房源；不同 --param 抓出来的 listings
    绝不能互相判死（严重问题：换查询条件会把条件外房源全部误标已售）。"""
    cur = conn.cursor()
    if query is None:
        cur.execute("SELECT key_id, sold_at FROM listings WHERE site=? AND sold_at IS NULL",
                    (site_name,))
    else:
        cur.execute("SELECT key_id, sold_at FROM listings"
                    " WHERE site=? AND sold_at IS NULL AND query=?",
                    (site_name, query))
    gone = [k for (k, _) in cur.fetchall() if k not in seen_keys]
    for k in gone:
        cur.execute("UPDATE listings SET sold_at=? WHERE site=? AND key_id=?", (now, site_name, k))
        cur.execute(
            "INSERT INTO status_changes (site,key_id,field,old,new,changed_at) VALUES (?,?,?,?,?,?)",
            (site_name, k, "availability", "可售", "已售/下架", now),
        )
    conn.commit()
    return len(gone)


def disappearance_guard(conn, site_name, query, *, pages_known, failed_pages,
                        all_rows, total, max_pages):
    """消失判定（mark_sold）的安全门：不满足完整性就跳过。

    为什么必须这么严：mark_sold 的前提是「本轮 seen_keys 覆盖该查询的全部在售
    结果集」。手动 --max-pages 截断、分页条文案变化导致只抓第 1 页、换查询条件，
    都会让大批活房源「消失」而被误标已售。这里把每种不完整情形都拦下并给出原因。
    返回 (是否允许, 跳过原因)。
    """
    if max_pages:
        return False, f"使用了 --max-pages={max_pages}，结果集被截断"
    if failed_pages:
        return False, f"有失败页 {failed_pages}"
    if not pages_known:
        return False, "分页总数未识别（共 N 页 正则失配），无法确认已抓全"
    if total is not None and all_rows != total:
        return False, f"抓到 {all_rows} 行 ≠ 站点总数 {total} 条，结果集不完整"
    if all_rows <= 0:
        return False, "本轮 0 行，无法区分「真的售罄」和「查询/解析异常」"
    prev = conn.execute(
        "SELECT query FROM snapshots WHERE site=? ORDER BY id DESC LIMIT 1",
        (site_name,)).fetchone()
    if not prev:
        return False, "没有上一轮快照可比对查询条件，首轮不做消失判定"
    try:
        prev_query = json.loads(prev[0] or "{}")
    except json.JSONDecodeError:
        prev_query = None
    if prev_query != dict(query):
        return False, "查询条件与上一轮不一致，条件外房源会被误判消失"
    return True, ""


def fetch_first_page(site, params, use_proxy):
    """抓第一页；直连失败且允许代理时才逐个走代理池兜底。

    旧代码拿到代理列表后仍调 site.fetch_page(params)（没传 proxy），等于拿完代理
    继续直连重试，是纯死代码。这里逐个代理试，全部失败才把最后的错误抛出去。
    """
    try:
        return site.fetch_page(params)
    except Exception as e:
        if not use_proxy:
            raise
        print(f"[warn] 直连失败: {e}，试代理池")
        proxies = proxies_from_pool()
        if not proxies:
            raise
        last = e
        for p in proxies:
            try:
                return site.fetch_page(params, proxy=p)
            except Exception as pe:
                print(f"[warn] 代理 {p} 失败: {pe}")
                last = pe
        raise last


def proxies_from_pool():
    """调 proxy_pool.py 拿可用代理；失败返回空列表（不影响直连主路径）。"""
    try:
        out = subprocess.run([sys.executable, os.path.join(HERE, "proxy_pool.py"), "get", "5"],
                             capture_output=True, text=True, timeout=30)
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser(description="WebForms 政府站爬虫")
    ap.add_argument("--site", required=True, help="站点配置 JSON 路径")
    ap.add_argument("--db", default=os.path.join(HERE, "..", "data", "housing.db"))
    ap.add_argument("--param", action="append", default=[], help="查询参数 k=v，可多次")
    ap.add_argument("--max-pages", type=int, default=0, help="0=全部页")
    ap.add_argument("--interval", type=float, default=1.0, help="翻页间隔秒")
    ap.add_argument("--use-proxy", action="store_true", help="直连失败时启用代理池兜底")
    ap.add_argument("--cookies", default=None, help="curl cookie jar 路径（默认用系统临时目录）")
    args = ap.parse_args()

    if not args.cookies:
        import tempfile
        args.cookies = os.path.join(tempfile.gettempdir(), "webforms_cookies.txt")

    with open(args.site, "r", encoding="utf-8") as f:
        site = Site(json.load(f), cookie_jar=args.cookies)
    params = dict(kv.split("=", 1) for kv in args.param)
    qkey = query_key(params)

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    conn = init_db(args.db)

    # 第一页（带查询条件）
    page = fetch_first_page(site, params, args.use_proxy)

    total = site.total_count(page)
    rows = site.parse_rows(page)
    if not rows:
        print("[warn] 第一页没解析到数据，检查 row_regex 是否匹配当前页面")
    n_new, n_chg = db_upsert(conn, site.name, rows, site, query=qkey)
    print(f"[page 1] {len(rows)} 行 (新增 {n_new}, 状态变化 {n_chg})" + (f", 共 {total} 条" if total else ""))
    all_rows = len(rows)

    # 从文本里算总页数："共 N 页"
    m = re.search(r"共\s*(\d+)\s*页", visible_text(page))
    pages_known = m is not None
    pages = int(m.group(1)) if m else 1
    if args.max_pages:
        pages = min(pages, args.max_pages)

    failed_pages = []
    prev_fields = hidden_fields(page)  # 第一页结果里的隐藏域，翻页要用
    seen_keys = {r.get(site.key_field) for r in rows}
    for p in range(1, pages):
        try:
            pg = site.fetch_page(params, page_idx=p, prev_fields=prev_fields)
            prs = site.parse_rows(pg)
            if not prs:
                failed_pages.append(p + 1)
                continue
            prev_fields = hidden_fields(pg)  # 链式更新
            a, b = db_upsert(conn, site.name, prs, site, query=qkey)
            all_rows += len(prs)
            seen_keys.update(r.get(site.key_field) for r in prs)
            print(f"[page {p+1}/{pages}] {len(prs)} 行 (新增 {a}, 变化 {b})")
        except Exception as e:
            print(f"[page {p+1}] 失败: {e}")
            failed_pages.append(p + 1)
        time.sleep(args.interval)

    # 消失判定前先过安全门：结果集不完整（截断/分页失配/失败页/换查询条件）时
    # 绝不能把「没爬到」误判成「已售」。
    n_sold = 0
    can_sold, why = disappearance_guard(
        conn, site.name, params, pages_known=pages_known,
        failed_pages=failed_pages, all_rows=all_rows, total=total,
        max_pages=args.max_pages)
    if can_sold:
        # 老库 listings 没有 query 列（补列后是 NULL）。本项目的在售库历史以来只有
        # 这一个查询条件，安全门又已确认本轮 query 与上轮一致，因此把 NULL 老行
        # 回填到本轮 query，否则这些「上个版本抓取的在售房」永远不会参与消失判定。
        conn.execute("UPDATE listings SET query=? WHERE site=? AND query IS NULL",
                     (qkey, site.name))
        # 必须用 now_cn()：GitHub Actions runner 是 UTC，直接 time.strftime 会让
        # status_changes 记 UTC、snapshots 记北京时间，同一轮数据出现两个时区
        # （2026-10-17 首次真实成交就暴露了：快照 10:17 vs 变化 02:17）。
        n_sold = mark_sold(conn, site.name, seen_keys, now_cn(), query=qkey)
        if n_sold:
            print(f"[sold] 本轮新检出已售/下架 {n_sold} 套")
    else:
        print(f"[sold] 跳过消失判定：{why}")

    cur = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE site=? AND sold_at IS NULL", (site.name,))
    alive = cur.fetchone()[0]

    # 字段语义（历史包袱说明）：
    #   total   = 本轮实际抓到的行数（旧代码写错了语义，保留兼容不动）
    #   fetched = 同上，语义明确的别名
    #   dtot    = 站点页面报的数据集总条数（"共 N 条"）
    run_ts = now_cn()          # 本轮统一时间戳，快照与变化记录必须一致
    conn.execute(
        "INSERT INTO snapshots (site,taken_at,query,total,new,changed,sold,alive,fetched,dtot)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (site.name, run_ts, qkey,
         all_rows, n_new, n_chg, n_sold, alive, all_rows, total or 0),
    )
    conn.commit()
    print(f"[done] 本次 {all_rows} 行入库; 失败页: {failed_pages or '无'}; 库存在售 {alive} 套")
    conn.close()


if __name__ == "__main__":
    main()
