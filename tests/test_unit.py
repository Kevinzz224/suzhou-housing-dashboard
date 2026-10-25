#!/usr/bin/env python3
"""单元测试：纯函数 + 数据库逻辑（stdlib unittest，零依赖）。

跑法:
    python -m unittest discover -s tests -v
    python tests/test_unit.py
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

import serve                      # noqa: E402
import webforms_crawl as wf       # noqa: E402
import export_static              # noqa: E402  导出产物也是被测对象


# ───────────────────────── 测试夹具 ─────────────────────────

def make_row_td(shse_id, loc, cid, comp, usage, layout, area, price):
    """构造一条与 suzhou.json row_regex 匹配的真实结构表格行。"""
    return (
        f'<tr>  \t\t\t<td OnMouseOver="c=this.style.backgroundColor;style.cursor=&#39;hand&#39;;" '
        f'onclick="javascript:window.showModalDialog(&#39;SaleInfoHouseView.aspx?SHSE_ID={shse_id}&#39;, window, &#39;dialogWidth:820px;&#39;);return false;">{loc}</td>'
        f'<td onclick="javascript:window.showModalDialog(&#39;ComPanyView.aspx?C_ID={cid}&#39;, window, &#39;x&#39;);return false;">{comp}</td>'
        f'<td>{usage}</td><td>{layout}</td><td>{area}</td><td>{price}</td></tr>'
    )


SAMPLE_TABLE_HTML = (
    '<table id="MainContent_PageGridView1">'
    '<tr style="background-color:DarkKhaki;font-weight:bold;">'
    '<th scope="col">房屋坐落</th><th scope="col">房地产公司名称</th>'
    '<th scope="col">房屋用途</th><th scope="col">房屋套型</th>'
    '<th scope="col">建筑面积</th><th scope="col">参考单价</th></tr>'
    + make_row_td("51027.10.2002_MD001", "51027双湖湾花园10幢2002", "549376",
                  "仁恒地产（苏州）有限公司", "住宅", "3室2厅2卫", "144.66", "45000")
    + make_row_td("53754.10.1004_MD001", "君华雅筑10幢1004", "590773",
                  "苏州市保嘉置业有限公司", "住宅", "4室2厅2卫", "152.96", "42679.95")
    + '</table>'
    '<td align="left" class="ft">共&nbsp455&nbsp条&nbsp--&nbsp第&nbsp1&nbsp页&nbsp--&nbsp共&nbsp23&nbsp页</td>'
)

SAMPLE_PAGE_HTML = (
    '<html><head><title>可售房屋查询</title></head><body>'
    '<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="/wEPDwUJNjgwNjc0MDM3D2QWAmYPZBYC" />'
    '<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="4A995636" />'
    '<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="/wEdAAqIi+lWC8ZBR+WXY" />'
    '<script>var x = 1;</script>'
    + SAMPLE_TABLE_HTML +
    '</body></html>'
)

SITE_CFG = None


def site_cfg():
    global SITE_CFG
    if SITE_CFG is None:
        import json
        with open(os.path.join(SCRIPTS, "sites", "suzhou.json"), encoding="utf-8") as f:
            SITE_CFG = json.load(f)
    return SITE_CFG


# ───────────────────────── 1. 纯函数 ─────────────────────────

class TestHiddenFields(unittest.TestCase):
    def test_extracts_three_webforms_fields(self):
        f = wf.hidden_fields(SAMPLE_PAGE_HTML)
        self.assertEqual(f["__VIEWSTATEGENERATOR"], "4A995636")
        self.assertTrue(f["__VIEWSTATE"].startswith("/wEPDwUJ"))
        self.assertIn("__EVENTVALIDATION", f)

    def test_missing_field_is_absent_not_crash(self):
        f = wf.hidden_fields("<html><body>no fields</body></html>")
        self.assertEqual(f, {})

    def test_unescapes_html_entities(self):
        page = '<input type="hidden" name="__VIEWSTATE" value="a&amp;b" />'
        self.assertEqual(wf.hidden_fields(page)["__VIEWSTATE"], "a&b")


class TestVisibleText(unittest.TestCase):
    def test_strips_tags_and_scripts(self):
        txt = wf.visible_text(SAMPLE_PAGE_HTML)
        self.assertIn("共 455 条", txt)
        self.assertNotIn("<td", txt)
        self.assertNotIn("var x = 1", txt)   # script 必须被剔除

    def test_collapses_whitespace(self):
        self.assertNotIn("\n", wf.visible_text("<div>a\n\n   b</div>"))


class TestQualify(unittest.TestCase):
    """控件名必须补 ctl00$MainContent$ 前缀 —— 漏了站点会静默返回全量数据。"""

    def test_short_name_gets_prefixed(self):
        got = wf.Site.qualify({"ddl_qy": "RD002"})
        self.assertEqual(got, {"ctl00$MainContent$ddl_qy": "RD002"})

    def test_dunder_fields_untouched(self):
        got = wf.Site.qualify({"__VIEWSTATE": "x", "__EVENTTARGET": "y"})
        self.assertEqual(got, {"__VIEWSTATE": "x", "__EVENTTARGET": "y"})

    def test_already_qualified_untouched(self):
        key = "ctl00$MainContent$txt_Area1"
        self.assertEqual(wf.Site.qualify({key: "140"}), {key: "140"})

    def test_mixed_batch(self):
        got = wf.Site.qualify({"ddl_qy": "RD002", "__VIEWSTATE": "v", "x": "1"})
        self.assertEqual(set(got), {"ctl00$MainContent$ddl_qy", "__VIEWSTATE",
                                    "ctl00$MainContent$x"})


class TestSiteParseRows(unittest.TestCase):
    def setUp(self):
        self.site = wf.Site(site_cfg())

    def test_parses_all_fields(self):
        rows = self.site.parse_rows(SAMPLE_TABLE_HTML)
        self.assertEqual(len(rows), 2)
        r = rows[0]
        self.assertEqual(r["key_id"], "51027.10.2002_MD001")
        self.assertEqual(r["location"], "51027双湖湾花园10幢2002")
        self.assertEqual(r["company"], "仁恒地产（苏州）有限公司")
        self.assertEqual(r["layout"], "3室2厅2卫")
        self.assertEqual(r["area"], "144.66")
        self.assertEqual(r["price"], "45000")

    def test_skips_header_and_pager(self):
        rows = self.site.parse_rows(SAMPLE_TABLE_HTML)
        self.assertTrue(all(r["key_id"] for r in rows))
        self.assertNotIn("房屋坐落", [r.get("location") for r in rows])

    def test_empty_page_yields_no_rows(self):
        self.assertEqual(self.site.parse_rows("<html></html>"), [])

    def test_decimal_price_preserved(self):
        rows = self.site.parse_rows(SAMPLE_TABLE_HTML)
        self.assertEqual(rows[1]["price"], "42679.95")


class TestSiteTotalCount(unittest.TestCase):
    def test_reads_total_from_text(self):
        site = wf.Site(site_cfg())
        self.assertEqual(site.total_count(SAMPLE_PAGE_HTML), 455)

    def test_missing_total_returns_none(self):
        site = wf.Site(site_cfg())
        self.assertIsNone(site.total_count("<html>没有数字</html>"))


class TestNowCn(unittest.TestCase):
    """时间戳必须统一到 UTC+8，否则本地/Actions 混写会让曲线错序。"""

    def test_format(self):
        s = wf.now_cn()
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_is_utc_plus_8(self):
        got = datetime.strptime(wf.now_cn(), "%Y-%m-%d %H:%M:%S")
        want = datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=8))).replace(tzinfo=None)
        self.assertLess(abs((got - want).total_seconds()), 5)


class TestParseDays(unittest.TestCase):
    """days 参数解析：非法值必须抛 ValueError（HTTP 层转 400，不能 500）。"""

    def test_defaults(self):
        self.assertEqual(serve.parse_days(None), 30)
        self.assertEqual(serve.parse_days(""), 30)
        self.assertEqual(serve.parse_days("7"), 7)

    def test_rejects_non_positive_and_non_numeric(self):
        for v in ("0", "-1", "-999", "abc", "1.5", "30d", " "):
            with self.assertRaises(ValueError, msg=f"days={v!r} 应被拒绝"):
                serve.parse_days(v)


# ───────────────────────── 2. 项目名与别名 ─────────────────────────

class TestProjectOf(unittest.TestCase):
    def test_standard_address(self):
        self.assertEqual(
            serve.project_of("苏州工业园区八达街108号天熠星湖雅苑2幢702"),
            "天熠星湖雅苑")

    def test_address_without_project_name_gets_fallback(self):
        """号与幢之间没有楼盘名（老小区），不能丢成"未知"。"""
        got = serve.project_of("苏州工业园区万寿街168号3幢2402")
        self.assertNotEqual(got, "未知")
        self.assertIn("万寿街", got)

    def test_no_building_suffix(self):
        self.assertEqual(serve.project_of("亭苑26幢203"), "亭苑")

    def test_empty_returns_unknown(self):
        self.assertEqual(serve.project_of(""), "未知")
        self.assertEqual(serve.project_of(None), "未知")

    def test_project_with_trailing_digit_stripped(self):
        # "凤起潮鸣苑48" -> "凤起潮鸣苑"
        self.assertEqual(
            serve.project_of("苏州工业园区金鸡湖大道1327号凤起潮鸣苑48幢301"),
            "凤起潮鸣苑")


class TestAlias(unittest.TestCase):
    def test_known_alias_applied(self):
        alias = serve._load_alias()
        self.assertEqual(serve.apply_alias("君华雅筑", alias), "保利珺华赋")

    def test_unknown_name_passthrough(self):
        self.assertEqual(serve.apply_alias("亭苑", {}), "亭苑")

    def test_empty_alias_falls_back_to_original(self):
        self.assertEqual(serve.apply_alias("庭樾雅园", {"庭樾雅园": ""}), "庭樾雅园")

    def test_alias_file_only_maps_real_projects(self):
        """别名表的 key 必须是真实存在的备案名，不能有拼写错误。"""
        alias = serve._load_alias()
        self.assertGreater(len(alias), 5)
        for k in alias:
            self.assertRegex(k, r"^[\u4e00-\u9fa5A-Za-z]+$")

    def test_alias_keys_exist_in_real_db(self):
        """只验正则挡不住「写错备案名」；必须对照真实库里的项目名。"""
        db = os.path.join(ROOT, "data", "housing.db")
        if not os.path.exists(db):
            self.skipTest("无真实库可对照")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            projects = {serve.project_of(r[0]) for r in conn.execute("SELECT location FROM listings")}
        finally:
            conn.close()
        if not projects:
            self.skipTest("真实库为空")
        missing = sorted(set(serve._load_alias()) - projects)
        self.assertEqual(missing, [], "别名表 key 必须是库里真实存在的备案名")


# ───────────────────────── 3. 数据库与增量 ─────────────────────────

class TestDbLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="housing_test_")
        self.db = os.path.join(self.tmp, "t.db")
        self.conn = wf.init_db(self.db)
        self.site = wf.Site(site_cfg())

    def tearDown(self):
        self.conn.close()

    def _rows(self, n=3):
        return [{"key_id": f"K{i}", "location": f"某盘{i}幢10{i}", "company": "公司",
                 "usage": "住宅", "layout": "4室2厅", "area": str(150 + i),
                 "price": str(40000 + i)} for i in range(n)]

    def test_init_db_creates_all_tables(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("listings", "status_changes", "snapshots", "subscriptions"):
            self.assertIn(t, names)

    def test_init_db_migrates_legacy_schema(self):
        """老库没有 sold_at/alive 等列，init_db 必须补上，否则查询全崩。"""
        legacy = os.path.join(self.tmp, "legacy.db")
        c = sqlite3.connect(legacy)
        c.executescript("""
            CREATE TABLE listings(site TEXT, key_id TEXT, first_seen_at TEXT,
                last_seen_at TEXT, project TEXT, location TEXT, company TEXT,
                usage TEXT, layout TEXT, area REAL, price REAL, total REAL,
                status TEXT, extra TEXT);
            CREATE TABLE snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT,
                taken_at TEXT, query TEXT, total INTEGER, new INTEGER, changed INTEGER);
        """)
        c.commit(); c.close()
        c2 = wf.init_db(legacy)
        cols_l = {r[1] for r in c2.execute("PRAGMA table_info(listings)")}
        cols_s = {r[1] for r in c2.execute("PRAGMA table_info(snapshots)")}
        c2.close()
        self.assertIn("sold_at", cols_l)
        self.assertIn("query", cols_l)
        for col in ("sold", "alive", "fetched", "dtot"):
            self.assertIn(col, cols_s)

    def test_upsert_inserts_new_rows(self):
        n_new, n_chg = wf.db_upsert(self.conn, "test", self._rows(3), self.site)
        self.assertEqual((n_new, n_chg), (3, 0))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM listings").fetchone()[0], 3)

    def test_upsert_is_idempotent(self):
        """重跑同一批数据不能重复插入（增量抓取的核心保证）。"""
        wf.db_upsert(self.conn, "test", self._rows(3), self.site)
        n_new, _ = wf.db_upsert(self.conn, "test", self._rows(3), self.site)
        self.assertEqual(n_new, 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM listings").fetchone()[0], 3)

    def test_upsert_updates_changed_price(self):
        wf.db_upsert(self.conn, "test", self._rows(1), self.site)
        changed = self._rows(1)
        changed[0]["price"] = "99999"
        wf.db_upsert(self.conn, "test", changed, self.site)
        got = self.conn.execute(
            "SELECT price FROM listings WHERE key_id='K0'").fetchone()[0]
        # price 在 schema 里是 REAL 列，SQLite 会把 '99999' 存成 99999.0，
        # 所以按数值比较（曾误写成字符串比较导致假失败）。
        self.assertAlmostEqual(float(got), 99999.0)

    def test_upsert_keeps_first_seen_at(self):
        """first_seen_at 是「该房源首次出现在可售列表」的时间，重抓绝不能刷新它。

        存量曲线完全靠这个字段反推（first_seen_at <= T 才算当时在售），
        一旦被 last_seen_at 覆盖，历史点会集体右移。"""
        wf.db_upsert(self.conn, "test", self._rows(1), self.site)
        self.conn.execute(
            "UPDATE listings SET first_seen_at='2020-01-01 00:00:00' WHERE key_id='K0'")
        self.conn.commit()
        wf.db_upsert(self.conn, "test", self._rows(1), self.site)
        row = self.conn.execute(
            "SELECT first_seen_at, last_seen_at FROM listings WHERE key_id='K0'").fetchone()
        self.assertEqual(row[0], "2020-01-01 00:00:00")
        self.assertGreater(row[1], wf.now_cn()[:10] + " 00:00:00", "last_seen_at 应刷新")

    def test_upsert_skips_rows_without_key(self):
        rows = [{"key_id": "", "location": "x"}, {"key_id": None, "location": "y"}]
        n_new, _ = wf.db_upsert(self.conn, "test", rows, self.site)
        self.assertEqual(n_new, 0)

    def test_mark_sold_flags_missing_listings(self):
        """本轮没出现 = 成交/下架 —— 去化速度的数据来源。"""
        wf.db_upsert(self.conn, "test", self._rows(3), self.site)
        seen = {"K0", "K1"}                     # K2 消失
        n = wf.mark_sold(self.conn, "test", seen, wf.now_cn())
        self.assertEqual(n, 1)
        sold = self.conn.execute(
            "SELECT key_id FROM listings WHERE sold_at IS NOT NULL").fetchall()
        self.assertEqual([r[0] for r in sold], ["K2"])

    def test_mark_sold_records_status_change(self):
        wf.db_upsert(self.conn, "test", self._rows(2), self.site)
        wf.mark_sold(self.conn, "test", {"K0"}, wf.now_cn())
        rows = self.conn.execute(
            "SELECT key_id, old, new FROM status_changes").fetchall()
        self.assertEqual(rows[0][0], "K1")
        self.assertEqual(rows[0][1], "可售")
        self.assertIn("已售", rows[0][2])

    def test_mark_sold_is_stable_across_runs(self):
        """已标记过的不能再标一次（否则每轮都重复记变化）。"""
        wf.db_upsert(self.conn, "test", self._rows(2), self.site)
        self.assertEqual(wf.mark_sold(self.conn, "test", {"K0"}, wf.now_cn()), 1)
        self.assertEqual(wf.mark_sold(self.conn, "test", {"K0"}, wf.now_cn()), 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM status_changes").fetchone()[0], 1)

    def test_mark_sold_returns_zero_when_all_present(self):
        wf.db_upsert(self.conn, "test", self._rows(3), self.site)
        self.assertEqual(wf.mark_sold(self.conn, "test", {"K0", "K1", "K2"},
                                      wf.now_cn()), 0)

    def test_upsert_revives_resold_listing(self):
        """已售房源重新出现时必须清 sold_at 并记「复活」，否则误标会永久化。"""
        wf.db_upsert(self.conn, "test", self._rows(3), self.site)
        wf.mark_sold(self.conn, "test", {"K0", "K1"}, wf.now_cn())   # K2 先被判已售
        n_new, n_chg = wf.db_upsert(self.conn, "test", self._rows(3), self.site)
        self.assertEqual(n_new, 0)
        self.assertEqual(n_chg, 1, "K2 复活应计为一次 availability 变化")
        self.assertIsNone(self.conn.execute(
            "SELECT sold_at FROM listings WHERE key_id='K2'").fetchone()[0])
        last = self.conn.execute(
            "SELECT old, new FROM status_changes WHERE key_id='K2' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(last), ("已售/下架", "可售"))

    def test_mark_sold_is_scoped_to_current_query(self):
        """换 --param 抓另一个条件时，条件外房源绝不能被本轮消失判定误杀。"""
        qa, qb = wf.query_key({"area": "140-180"}), wf.query_key({"area": "90-120"})
        ra = self._rows(2)
        rb = self._rows(1)
        rb[0]["key_id"] = "B0"
        wf.db_upsert(self.conn, "test", ra, self.site, query=qa)
        wf.db_upsert(self.conn, "test", rb, self.site, query=qb)
        n = wf.mark_sold(self.conn, "test", {"K0"}, wf.now_cn(), query=qa)
        self.assertEqual(n, 1, "同一 query 下没出现的 K1 才应判已售")
        sold = {r[0] for r in self.conn.execute(
            "SELECT key_id FROM listings WHERE sold_at IS NOT NULL")}
        self.assertEqual(sold, {"K1"})
        self.assertIsNone(self.conn.execute(
            "SELECT sold_at FROM listings WHERE key_id='B0'").fetchone()[0])


class TestProjectSnapshots(unittest.TestCase):
    """项目级存量曲线必须按项目算，不能复用全站快照。

    真实 bug（用户报的「-1 套」）：君华雅筑卖掉 1 套，
    30 个项目详情页全都显示 -1 —— 因为直接读了 snapshots.alive（全站数字）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ps_")
        self.db = os.path.join(self.tmp, "t.db")
        self.conn = wf.init_db(self.db)
        # 造 3 个快照时刻
        # 时间轴设计（避免"售出"和"新增"撞在同一秒，否则永远看不到中间态）：
        #   23:00 -> A1,A2           = 2
        #   09:00 -> A1,A2           = 2（A3 尚未出现）
        #   10:00 -> A1,A2,A3        = 3（A3 于 09:30 上架）
        #   12:00 -> A1,A3           = 2（A2 于 11:00 售出）
        self.times = ["2026-10-16 23:00:00", "2026-10-17 09:00:00",
                      "2026-10-17 10:00:00", "2026-10-17 12:00:00"]
        for t in self.times:
            self.conn.execute(
                "INSERT INTO snapshots (site,taken_at,query,total,new,changed,sold,alive)"
                " VALUES ('s',?,'{}',0,0,0,0,0)", (t,))
        rows = [
            ("A1", "甲苑1幢101", "2026-10-16 22:00:00", None),          # 一直在售
            ("A2", "甲苑1幢102", "2026-10-16 22:00:00", "2026-10-17 11:00:00"),  # 11点售出
            ("A3", "甲苑1幢103", "2026-10-17 09:30:00", None),          # 09:30 上架
            ("B1", "乙苑1幢201", "2026-10-16 22:00:00", None),          # 对照组
        ]
        for k, loc, fs, sd in rows:
            self.conn.execute(
                "INSERT INTO listings (site,key_id,first_seen_at,last_seen_at,location,sold_at)"
                " VALUES ('s',?,?,?,?,?)", (k, fs, fs, loc, sd))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_curve_is_per_project(self):
        snaps = serve.project_snapshots(self.conn, "甲苑", days=3650)
        vals = [s["alive"] for s in snaps]
        self.assertEqual(vals, [2, 2, 3, 2],
                         "甲苑：A3 于 09:30 上架(10:00 点看到 3)，A2 于 11:00 售出(12:00 点回落到 2)")

    def test_untouched_project_stays_flat(self):
        """没成交的项目曲线必须是平的（这就是用户报的 bug）。"""
        snaps = serve.project_snapshots(self.conn, "乙苑", days=3650)
        self.assertEqual([s["alive"] for s in snaps], [1, 1, 1, 1],
                         "乙苑没有任何变化，不能出现 -1")

    def test_last_point_equals_current_alive(self):
        d = serve.api_project(self.conn, "甲苑", days=3650)
        self.assertEqual(d["snapshots"][-1]["alive"], d["alive"],
                         "曲线末点必须等于当前在售数")

    def test_different_projects_differ(self):
        a = serve.api_project(self.conn, "甲苑", days=3650)
        b = serve.api_project(self.conn, "乙苑", days=3650)
        self.assertNotEqual([s["alive"] for s in a["snapshots"]],
                            [s["alive"] for s in b["snapshots"]],
                            "两个项目的曲线不应相同")

    def test_unknown_project_returns_empty(self):
        self.assertEqual(serve.project_snapshots(self.conn, "不存在", days=3650), [])

    def test_appends_now_point_when_changed_after_last_snapshot(self):
        """成交发生在最后一次快照之后（抓取中途崩溃）时，必须补「现在」点。

        否则曲线末点会高于真实在售数，且一直错到下一次抓取。
        """
        # 只有 10:00 一个快照；随后 A2 在 11:00 售出，但没有 11:00 的快照
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM snapshots")
        c.execute("INSERT INTO snapshots(site,taken_at,query,total,new,changed,sold,alive)"
                  " VALUES ('s','2026-10-17 10:00:00','{}',0,0,0,0,0)")
        c.execute("UPDATE listings SET sold_at='2026-10-17 11:00:00' WHERE key_id='A2'")
        c.commit()
        c.close()
        snaps = serve.project_snapshots(self.conn, "甲苑", days=3650)
        self.assertEqual(snaps[-1]["time"], "现在",
                         "快照后有变化时应追加「现在」点")
        self.assertEqual(snaps[-1]["alive"], 2,
                         "现在点应反映真实在售（A1 + A3）")
        d = serve.api_project(self.conn, "甲苑", days=3650)
        self.assertEqual(d["snapshots"][-1]["alive"], d["alive"],
                         "补点后末点必须等于当前在售")


class TestProjectSnapshotsBoundaries(unittest.TestCase):
    """存量曲线的边界口径：同一秒、跨日窗口、复活后的历史点。

    这三处原来没有任何测试 —— 而它们正是「末点 ≠ 当前在售数」的高发地带。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="psb_")
        self.conn = wf.init_db(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.conn.close()

    def _snap(self, t):
        self.conn.execute(
            "INSERT INTO snapshots (site,taken_at,query,total,new,changed,sold,alive)"
            " VALUES ('s',?,'{}',0,0,0,0,0)", (t,))
        self.conn.commit()

    def _listing(self, key, loc, first_seen, sold_at=None):
        self.conn.execute(
            "INSERT INTO listings (site,key_id,first_seen_at,last_seen_at,location,sold_at)"
            " VALUES ('s',?,?,?,?,?)", (key, first_seen, first_seen, loc, sold_at))
        self.conn.commit()

    def test_sold_at_same_second_as_snapshot_is_not_alive(self):
        """sold_at == taken_at：同一秒按「已售」算。

        口径必须固定，否则末点会在 1/0 之间抖（mark_sold 与快照时间戳都是
        now_cn()，秒级相同完全可能）。"""
        t = "2026-10-17 10:00:00"
        self._snap(t)
        self._listing("A", "甲苑1幢101", "2026-10-16 09:00:00")
        self._listing("B", "甲苑1幢102", "2026-10-16 09:00:00", sold_at=t)
        self.assertEqual([s["alive"] for s in serve.project_snapshots(self.conn, "甲苑", days=3650)],
                         [1])
        d = serve.api_project(self.conn, "甲苑", days=3650)
        self.assertEqual(d["alive"], 1)
        self.assertEqual(d["snapshots"][-1]["alive"], d["alive"],
                         "同一秒售出也不能让末点偏离当前在售数")

    def test_days_window_is_inclusive_of_cutoff_date(self):
        """days=1 时跨日两点都要保留（16 日 23:59 与 17 日 00:01 属同一窗口）。"""
        self._snap("2026-10-15 10:00:00")     # 窗口外
        self._snap("2026-10-16 23:59:00")
        self._snap("2026-10-17 00:01:00")
        self._listing("A", "甲苑1幢101", "2026-10-14 09:00:00")
        with mock.patch.object(serve, "now_cn", return_value=datetime(2026, 10, 17, 12, 0, 0)):
            snaps = serve.project_snapshots(self.conn, "甲苑", days=1)
        self.assertEqual([s["time"] for s in snaps], ["23:59", "00:01"])

    def test_revival_is_retroactive_known_tradeoff(self):
        """已知取舍：售出后再上架（sold_at 被清空）无法回放中间那段空窗。

        listings 只存最终状态，没有 available 区间表，所以历史点会把它算成一直
        在售。这条断言把当前口径钉死的目的是：以后若改成「按 status_changes
        重建区间」，必须是有意识地改，而不是悄悄变。"""
        self._snap("2026-10-15 10:00:00")
        self._snap("2026-10-17 10:00:00")
        self._listing("A", "甲苑1幢101", "2026-10-14 09:00:00", sold_at=None)  # 曾售出又复活
        self.assertEqual([s["alive"] for s in serve.project_snapshots(self.conn, "甲苑", days=3650)],
                         [1, 1])


# ───────────────────────── 4. 爬虫健壮性（GLM 审查回归） ─────────────────────────

class TestDecodePage(unittest.TestCase):
    def test_defaults_to_utf8_without_meta(self):
        raw = "<html><body>中文可售</body></html>".encode("utf-8")
        self.assertEqual(wf.decode_page(raw), "<html><body>中文可售</body></html>")

    def test_respects_meta_charset_gbk(self):
        raw = '<meta charset="gbk"><html><body>中文可售</body></html>'.encode("gbk")
        self.assertEqual(wf.decode_page(raw),
                         '<meta charset="gbk"><html><body>中文可售</body></html>')

    def test_unknown_charset_falls_back_to_utf8(self):
        raw = '<meta charset="x-no-such-charset"><html>中文</html>'.encode("utf-8")
        self.assertIn("中文", wf.decode_page(raw))


class TestHttpEncoding(unittest.TestCase):
    def test_http_decodes_bytes_explicitly_not_by_locale(self):
        """subprocess.run 不能再带 text=True（Windows cp936 会把 UTF-8 页面解成乱码）。"""
        fake = mock.Mock(returncode=0, stdout="<html>中文</html>".encode("utf-8"), stderr=b"")
        with mock.patch.object(wf.subprocess, "run", return_value=fake) as m:
            got = wf.http("http://example.test")
        self.assertEqual(got, "<html>中文</html>")
        self.assertNotIn("text", m.call_args.kwargs)


class TestFetchPageLogic(unittest.TestCase):
    HIDDEN_ONLY = (
        '<input type="hidden" name="__VIEWSTATE" value="v" />'
        '<input type="hidden" name="__EVENTVALIDATION" value="e" />'
        '<html><body>no rows</body></html>'
    )

    def setUp(self):
        self.site = wf.Site(site_cfg())

    def test_query_params_are_qualified_and_empty_dropped(self):
        calls = []
        self.site._http = lambda data=None, proxy=None: (calls.append(data), SAMPLE_PAGE_HTML)[1]
        self.site.fetch_page({"txt_Pro": "", "ddl_qy": "RD002"}, retries=1)
        self.assertIsNone(calls[0], "查询前必须先 GET 拿 VIEWSTATE")
        sent = calls[1]
        self.assertEqual(sent["ctl00$MainContent$ddl_qy"], "RD002")
        self.assertNotIn("ctl00$MainContent$txt_Pro", sent, "空串参数会让站点丢查询条件")
        self.assertEqual(sent["ctl00$MainContent$bt_select"], "查询")

    def test_empty_result_retries_then_raises(self):
        calls = []
        self.site._http = lambda data=None, proxy=None: (calls.append(data), self.HIDDEN_ONLY)[1]
        with self.assertRaises(RuntimeError):
            self.site.fetch_page({}, retries=2)
        self.assertEqual(len(calls), 4, "每次尝试 = GET 拿隐藏域 + POST 查询，共 2 轮")

    def test_paging_uses_prev_fields_and_event_target(self):
        calls = []
        self.site._http = lambda data=None, proxy=None: (calls.append(data), SAMPLE_PAGE_HTML)[1]
        prev = {"__VIEWSTATE": "v2", "__EVENTVALIDATION": "e2"}
        self.site.fetch_page({}, page_idx=1, prev_fields=prev, retries=1)
        self.assertEqual(len(calls), 1, "翻页必须用上一页隐藏域，不能重新 GET")
        sent = calls[0]
        self.assertEqual(sent["__VIEWSTATE"], "v2")
        self.assertEqual(sent["__EVENTTARGET"], self.site.pager_target)
        self.assertEqual(sent[self.site.pager_field], "1")


class TestProxyFallback(unittest.TestCase):
    class FakeSite:
        name = "fake"

        def __init__(self, script):
            self.script = script
            self.calls = []

        def fetch_page(self, params, proxy=None):
            self.calls.append(proxy)
            got = self.script(proxy)
            if isinstance(got, Exception):
                raise got
            return got

    def test_direct_success_never_touches_proxy_pool(self):
        site = self.FakeSite(lambda proxy: "ok")
        with mock.patch.object(wf, "proxies_from_pool") as pool:
            self.assertEqual(wf.fetch_first_page(site, {}, use_proxy=True), "ok")
        pool.assert_not_called()
        self.assertEqual(site.calls, [None])

    def test_proxy_fallback_actually_passes_proxy(self):
        """回归：旧代码拿到代理列表却没传 proxy=，等于继续直连重试。"""
        site = self.FakeSite(lambda proxy: RuntimeError("直连失败") if proxy is None else
                             (RuntimeError("代理1失败") if proxy == "1.1.1.1:80" else "ok"))
        with mock.patch.object(wf, "proxies_from_pool", return_value=["1.1.1.1:80", "2.2.2.2:80"]):
            self.assertEqual(wf.fetch_first_page(site, {}, use_proxy=True), "ok")
        self.assertEqual(site.calls, [None, "1.1.1.1:80", "2.2.2.2:80"])

    def test_no_proxy_available_reraises_direct_error(self):
        site = self.FakeSite(lambda proxy: RuntimeError("直连失败"))
        with mock.patch.object(wf, "proxies_from_pool", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "直连失败"):
                wf.fetch_first_page(site, {}, use_proxy=True)


class TestDisappearanceGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="guard_")
        self.db = os.path.join(self.tmp, "t.db")
        self.conn = wf.init_db(self.db)

    def tearDown(self):
        self.conn.close()

    def _seed_prev(self, query):
        self.conn.execute("INSERT INTO snapshots (site,query) VALUES (?,?)",
                          ("s", json.dumps(query, ensure_ascii=False)))
        self.conn.commit()

    def test_blocks_max_pages_truncation(self):
        self._seed_prev({"a": "1"})
        ok, why = wf.disappearance_guard(
            self.conn, "s", {"a": "1"}, pages_known=True, failed_pages=[],
            all_rows=10, total=10, max_pages=5)
        self.assertFalse(ok)
        self.assertIn("max-pages", why)

    def test_blocks_when_pager_count_unknown(self):
        self._seed_prev({"a": "1"})
        ok, why = wf.disappearance_guard(
            self.conn, "s", {"a": "1"}, pages_known=False, failed_pages=[],
            all_rows=20, total=None, max_pages=0)
        self.assertFalse(ok)
        self.assertIn("分页总数未识别", why)

    def test_blocks_when_rows_short_of_site_total(self):
        self._seed_prev({"a": "1"})
        ok, why = wf.disappearance_guard(
            self.conn, "s", {"a": "1"}, pages_known=True, failed_pages=[],
            all_rows=20, total=455, max_pages=0)
        self.assertFalse(ok)
        self.assertIn("结果集不完整", why)

    def test_blocks_when_query_changed(self):
        self._seed_prev({"ddl_qy": "RD001"})
        ok, why = wf.disappearance_guard(
            self.conn, "s", {"ddl_qy": "RD002"}, pages_known=True, failed_pages=[],
            all_rows=10, total=10, max_pages=0)
        self.assertFalse(ok)
        self.assertIn("查询条件", why)

    def test_allows_complete_result_with_same_query_any_order(self):
        self.conn.execute("INSERT INTO snapshots (site,query) VALUES (?,?)",
                          ("s", '{"b": "2", "a": "1"}'))
        self.conn.commit()
        ok, why = wf.disappearance_guard(
            self.conn, "s", {"a": "1", "b": "2"}, pages_known=True, failed_pages=[],
            all_rows=10, total=10, max_pages=0)
        self.assertTrue(ok, why)

    def test_first_run_without_snapshot_skips(self):
        ok, why = wf.disappearance_guard(
            self.conn, "s", {"a": "1"}, pages_known=True, failed_pages=[],
            all_rows=10, total=10, max_pages=0)
        self.assertFalse(ok)
        self.assertIn("没有上一轮快照", why)


class TestServeAggregates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="serve_")
        self.db = os.path.join(self.tmp, "t.db")
        self.conn = wf.init_db(self.db)

    def tearDown(self):
        self.conn.close()

    def _add_listing(self, key, loc, area, price, sold_at=None):
        self.conn.execute(
            "INSERT INTO listings (site,key_id,first_seen_at,last_seen_at,location,area,price,sold_at)"
            " VALUES ('s',?,?,?,?,?,?,?)",
            (key, "2026-01-01 00:00:00", "2026-01-01 00:00:00", loc, area, price, sold_at))
        self.conn.commit()

    def test_api_projects_aggregates_numbers(self):
        self._add_listing("A1", "苏州工业园区某路1号甲苑1幢101", 140, 40000)
        self._add_listing("A2", "苏州工业园区某路1号甲苑1幢102", 150, 41000)
        self._add_listing("A3", "苏州工业园区某路1号甲苑1幢103", 160, 42000,
                          sold_at="2026-01-02 00:00:00")
        got = {p["project"]: p for p in serve.api_projects(self.conn)}["甲苑"]
        self.assertEqual((got["alive"], got["sold"], got["total"]), (2, 1, 3))
        self.assertEqual(got["avg_price"], 40500)
        self.assertEqual(got["sell_rate"], 33.3)

    def test_api_trend_filters_alive_zero_or_null_legacy_rows(self):
        """alive=0/NULL 是 ALTER 补列的历史假值，画进曲线就是 455->0 假暴跌。"""
        rows = [
            ("2026-10-15 09:00:00", 0, 0, 100, 100, 100),
            ("2026-10-16 09:00:00", None, 0, 100, 100, 100),
            ("2026-10-17 09:00:00", 10, 1, 11, 11, 11),
        ]
        for t, alive, sold, total, fetched, dtot in rows:
            self.conn.execute(
                "INSERT INTO snapshots (site,taken_at,query,total,new,changed,sold,alive,fetched,dtot)"
                " VALUES ('s',?,'{}',?,0,0,?,?,?,?)",
                (t, total, sold, alive, fetched, dtot))
        self.conn.commit()
        out = serve.api_trend(self.conn, days=3650)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["alive"], 10)
        self.assertEqual(out[0]["dataset_total"], 11)

    def test_api_stats_sold_today_uses_beijing_date(self):
        """UTC 前一天 16:00-24:00 对应北京时间今天， sold_today 不能按 UTC 日期算。"""
        self._add_listing("B1", "乙苑1幢101", 100, 30000,
                          sold_at="2026-01-02 00:30:00")   # 北京时间 1/2
        self._add_listing("B2", "乙苑1幢102", 100, 30000,
                          sold_at="2026-01-01 23:30:00")   # 北京时间 1/1
        with mock.patch.object(serve, "now_cn", return_value=datetime(2026, 1, 2, 1, 0, 0)):
            stats = serve.api_stats(self.conn)
        self.assertEqual(stats["sold_today"], 1)


class TestHttpApi(unittest.TestCase):
    """HTTP 层参数校验：之前 days 非法值 500、delete 无 id 也 ok。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="http_")
        self.db = os.path.join(self.tmp, "t.db")
        wf.init_db(self.db).close()
        self.old_db = serve.DB
        serve.DB = self.db
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        serve.DB = self.old_db

    def _request(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")

    def test_delete_subscription_without_id_is_400(self):
        code, body = self._request("/api/subscriptions/delete", method="POST", body={})
        self.assertEqual(code, 400)
        self.assertIn("id", body)

    def test_invalid_days_returns_400_not_500(self):
        for path in ("/api/trend?days=abc", "/api/project?name=x&days=abc"):
            code, body = self._request(path)
            self.assertEqual(code, 400, path)
            self.assertIn("days", body)

    def test_unknown_route_is_404(self):
        code, _ = self._request("/api/nope")
        self.assertEqual(code, 404)

    def test_create_subscription_returns_server_id(self):
        """POST 必须回服务端 id：前端要靠它取消服务端订阅（否则删除后复活）。"""
        code, body = self._request("/api/subscriptions", method="POST",
                                   body={"keyword": "某某盘"})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data.get("ok"))
        self.assertIsInstance(data.get("id"), int, f"应返回数字 id，实际 {data!r}")
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT id, keyword, active FROM subscriptions WHERE id=?",
                (data["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (data["id"], "某某盘", 1), "返回的 id 必须对应真实落库行")
        # 删掉之后 GET 不能再返回
        code, _ = self._request("/api/subscriptions/delete", method="POST",
                                body={"id": data["id"]})
        self.assertEqual(code, 200)
        code, body = self._request("/api/subscriptions")
        self.assertEqual(json.loads(body), [])


class TestRealDataIntegrity(unittest.TestCase):
    """端到端数据自检：拿真实 data/housing.db 跑一遍导出，核对最终产物。

    前面所有测试都是造数据，这里是唯一「真数据」级别的断言：末点偏离、alive
    汇总错、出现 alive=0 的坏点、别名对不上项目，都会直接红。
    库先复制到临时目录，保证被测文件只读。
    """

    @classmethod
    def setUpClass(cls):
        src = os.path.join(ROOT, "data", "housing.db")
        if not os.path.exists(src):
            raise unittest.SkipTest("没有 data/housing.db，跳过真数据自检")
        cls.tmp = tempfile.mkdtemp(prefix="realdata_")
        try:
            cls.db = os.path.join(cls.tmp, "housing.db")
            shutil.copyfile(src, cls.db)
            try:
                cls.payload = export_static.build(cls.db, 30)
            except sqlite3.DatabaseError as e:
                raise unittest.SkipTest(f"真实库不可读: {e}")
            if not cls.payload["projects"]:
                raise unittest.SkipTest("真实库没有项目数据")
        except Exception:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_project_last_point_equals_alive(self):
        bad = []
        for pr in self.payload["projects"]:
            d = self.payload["project_details"][pr["project"]]
            snaps = d.get("snapshots") or []
            last = snaps[-1]["alive"] if snaps else None
            if last != d["alive"]:
                bad.append((pr["project"], last, d["alive"], len(snaps)))
        self.assertEqual(bad, [], f"曲线末点与当前在售数不一致: {bad}")

    def test_stats_equals_sum_of_projects(self):
        p = self.payload
        self.assertEqual(p["stats"]["alive"], sum(x["alive"] for x in p["projects"]))
        self.assertEqual(p["stats"]["sold"], sum(x["sold"] for x in p["projects"]))
        self.assertEqual(p["stats"]["total"], sum(x["total"] for x in p["projects"]))
        self.assertEqual(p["stats"]["total"],
                         p["stats"]["alive"] + p["stats"]["sold"])

    def test_no_alive_zero_project(self):
        """项目在售数不能为负；若在售为 0，必须是全部售罄（sold == total）。"""
        bad = [x["project"] for x in self.payload["projects"]
               if x["alive"] < 0 or (x["alive"] == 0 and x["sold"] != x["total"])]
        self.assertEqual(bad, [], f"出现 alive 非法或未售罄却为 0 的坏点: {bad}")

    def test_detail_coverage_and_display_names(self):
        p = self.payload
        names = {x["project"] for x in p["projects"]}
        self.assertEqual(set(p["project_details"]), names, "每个项目都必须有预计算详情")
        self.assertEqual([x["project"] for x in p["projects"] if not x.get("display")], [])
        # 别名表不能指向不存在的项目（死配置）
        dead = sorted(set(serve._load_alias()) - names)
        self.assertEqual(dead, [], f"别名表指向不存在的项目: {dead}")

    def test_trend_points_are_positive_and_ordered(self):
        t = self.payload["trend"]
        self.assertGreaterEqual(len(t), 1)
        self.assertEqual([x["alive"] for x in t if x["alive"] <= 0], [],
                         "趋势里不能有 alive<=0 的点（假暴跌）")
        key = [x["date"] + " " + x["time"] for x in t]
        self.assertEqual(key, sorted(key), "趋势点必须按时间升序")


if __name__ == "__main__":
    unittest.main(verbosity=2)
