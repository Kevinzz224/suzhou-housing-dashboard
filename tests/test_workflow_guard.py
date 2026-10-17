#!/usr/bin/env python3
"""测试 CI 工作流的「补跑守卫」逻辑（从 YAML 里抽脚本真跑）。

为什么单独测：守卫决定"今天抓过就不抓"，一旦判错会
  - 判成"没抓过"  -> 每次 cron 都重抓，一天 4 条快照，趋势被噪声污染
  - 判成"抓过了"  -> 整天不抓，用户以为坏了（2026-10-17 真实发生过漏跑）
所以必须覆盖：今天/昨天/空库/损坏库/跨日边界 五种情况。

跑法: python tests/test_workflow_guard.py
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
WF = os.path.join(ROOT, ".github", "workflows", "daily-crawl.yml")

TZ8 = timezone(timedelta(hours=8))


def extract_guard_script():
    """从 workflow YAML 里抽出守卫的 python heredoc。"""
    src = open(WF, encoding="utf-8").read()
    m = re.search(r"python - <<'PY' >> \"\$GITHUB_OUTPUT\"\n(.*?)\n\s*PY\n", src, re.S)
    if not m:
        raise RuntimeError("workflow 里找不到守卫脚本（heredoc 被改动？）")
    # 去掉 YAML 缩进
    return "\n".join(l[10:] if l.startswith(" " * 10) else l
                     for l in m.group(1).splitlines())


class TestWorkflowGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="guard_")
        os.makedirs(os.path.join(self.tmp, "data"), exist_ok=True)
        self.db = os.path.join(self.tmp, "data", "housing.db")
        self.script = extract_guard_script()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, taken_at=None):
        if os.path.exists(self.db):
            os.remove(self.db)
        if taken_at is None:
            return
        c = sqlite3.connect(self.db)
        c.executescript("CREATE TABLE snapshots(id INTEGER PRIMARY KEY, taken_at TEXT);")
        c.execute("INSERT INTO snapshots(taken_at) VALUES (?)", (taken_at,))
        c.commit()
        c.close()

    def _run(self):
        """模拟 workflow：脚本 stdout 追加到 $GITHUB_OUTPUT，返回 (rc, 输出, stderr)。"""
        outfile = os.path.join(self.tmp, "out.txt")
        open(outfile, "w").close()
        with open(outfile, "a", encoding="utf-8") as fh:
            r = subprocess.run([sys.executable, "-c", self.script],
                               cwd=self.tmp, stdout=fh,
                               stderr=subprocess.PIPE, text=True)
        with open(outfile, encoding="utf-8") as fh:
            out = fh.read().strip()
        return r.returncode, out, r.stderr

    # ── 核心分支 ────────────────────────────────────

    def test_skip_when_already_crawled_today(self):
        today = datetime.now(timezone.utc).astimezone(TZ8).strftime("%Y-%m-%d")
        self._seed(f"{today} 09:07:00")
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("done=true", out, "今天已抓过应跳过，避免重复快照")

    def test_run_when_last_crawl_was_yesterday(self):
        y = (datetime.now(timezone.utc).astimezone(TZ8) - timedelta(days=1)).strftime("%Y-%m-%d")
        self._seed(f"{y} 09:07:00")
        rc, out, _ = self._run()
        self.assertIn("done=false", out, "昨天抓的，今天应继续抓")

    def test_run_on_empty_db(self):
        self._seed(None)
        rc, out, _ = self._run()
        self.assertIn("done=false", out, "新仓库空库应继续抓")

    # ── 健壮性 ──────────────────────────────────────

    def test_corrupt_db_does_not_crash(self):
        """库损坏时必须照常抓取，不能让整条流水线挂掉。"""
        with open(self.db, "wb") as f:
            f.write(b"this is not a sqlite file")
        rc, out, _ = self._run()
        self.assertEqual(rc, 0, "损坏的库不应让脚本非零退出")
        self.assertIn("done=false", out, "读不到快照时应视作未抓取")

    def test_midnight_boundary_yesterday_2359_is_not_today(self):
        """跨日边界：昨天 23:59 不能被当成今天。"""
        y = (datetime.now(timezone.utc).astimezone(TZ8) - timedelta(days=1)).strftime("%Y-%m-%d")
        self._seed(f"{y} 23:59:59")
        rc, out, _ = self._run()
        self.assertIn("done=false", out)

    def test_empty_snapshots_table(self):
        c = sqlite3.connect(self.db)
        c.executescript("CREATE TABLE snapshots(id INTEGER PRIMARY KEY, taken_at TEXT);")
        c.commit(); c.close()
        rc, out, _ = self._run()
        self.assertIn("done=false", out, "表存在但无数据应继续抓")


class TestWorkflowConfig(unittest.TestCase):
    """cron 配置本身的断言（避整点、多档冗余）。"""

    def setUp(self):
        self.src = open(WF, encoding="utf-8").read()
        self.crons = re.findall(r"- cron:\s*'([^']+)'", self.src)

    def test_has_multiple_retry_slots(self):
        self.assertGreaterEqual(len(self.crons), 3,
                                "单档 cron 会因 GitHub 丢弃而整日漏跑，至少 3 档")

    def test_avoids_top_of_hour(self):
        """整点是全球最挤的时刻，GitHub 文档明确建议避开 :00。"""
        for c in self.crons:
            minute = c.split()[0]
            self.assertNotEqual(minute, "0",
                                f"cron {c!r} 落在整点，GitHub 高负载时更易被丢弃")

    def test_cron_is_utc_9am_beijing(self):
        """北京 9:00 = UTC 1:00；至少有一档落在 1 点（09:xx 北京）。"""
        hours = [int(c.split()[1]) for c in self.crons]
        self.assertIn(1, hours, "缺少 UTC 1 点档（北京 9 点）")

    def test_guard_step_wired(self):
        self.assertIn("id: guard", self.src)
        # 抓取/导出/提交三步都必须受守卫保护
        self.assertGreaterEqual(self.src.count("steps.guard.outputs.done != 'true'"), 3,
                                "抓取/导出/提交都应受守卫保护")
        self.assertIn("steps.guard.outputs.done == 'true'", self.src,
                      "应有跳过时的说明步骤（便于排查）")

    def test_gitattributes_marks_db_binary(self):
        """SQLite 是二进制，autocrlf=true 下必须声明 binary，否则会被换行转换破坏。"""
        ga = os.path.join(ROOT, ".gitattributes")
        self.assertTrue(os.path.exists(ga), "缺少 .gitattributes")
        self.assertIn("*.db binary", open(ga, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
