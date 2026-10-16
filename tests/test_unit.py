#!/usr/bin/env python3
"""单元测试：纯函数 + 数据库逻辑（stdlib unittest，零依赖）。

跑法:
    python -m unittest discover -s tests -v
    python tests/test_unit.py
"""
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

import serve                      # noqa: E402
import webforms_crawl as wf       # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
