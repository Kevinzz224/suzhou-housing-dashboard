#!/usr/bin/env python3
"""测试 CI 工作流的「补跑守卫」逻辑（从 YAML 里抽脚本真跑）。

为什么单独测：守卫决定"今天抓过就不抓"，一旦判错会
  - 判成"没抓过"  -> 每次 cron 都重抓，一天 4 条快照，趋势被噪声污染
  - 判成"抓过了"  -> 整天不抓，用户以为坏了（2026-10-17 真实发生过漏跑）
所以必须覆盖：今天/昨天/空库/损坏库/跨日边界 五种情况。

跑法: python tests/test_workflow_guard.py
"""
import json
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
        self._write_queries(1)          # 默认单查询；多查询用例自己覆盖

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_queries(self, n):
        """在临时仓库里写一份 queries.json，控制「今天需要几条查询才算抓完」。"""
        os.makedirs(os.path.join(self.tmp, "scripts"), exist_ok=True)
        qs = [{"name": f"q{i}", "params": {"a": str(i)}} for i in range(1, n + 1)]
        with open(os.path.join(self.tmp, "scripts", "queries.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"site": "s.json", "queries": qs}, f, ensure_ascii=False)

    def _seed(self, taken_at=None, fetched=None, dtot=None, query="q1"):
        if os.path.exists(self.db):
            os.remove(self.db)
        if taken_at is None:
            return
        c = sqlite3.connect(self.db)
        c.executescript(
            "CREATE TABLE snapshots(id INTEGER PRIMARY KEY, taken_at TEXT,"
            " query TEXT, fetched INTEGER, dtot INTEGER);")
        c.execute("INSERT INTO snapshots(taken_at, query, fetched, dtot)"
                  " VALUES (?,?,?,?)", (taken_at, query, fetched, dtot))
        c.commit()
        c.close()

    def _add_snapshot(self, taken_at, query, fetched, dtot):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO snapshots(taken_at, query, fetched, dtot)"
                  " VALUES (?,?,?,?)", (taken_at, query, fetched, dtot))
        c.commit()
        c.close()

    def _run(self, force=False):
        """模拟 workflow：脚本 stdout 追加到 $GITHUB_OUTPUT，返回 (rc, 输出, stderr)。"""
        outfile = os.path.join(self.tmp, "out.txt")
        open(outfile, "w").close()
        env = dict(os.environ)
        if force:
            env["FORCE"] = "true"
        else:
            env.pop("FORCE", None)
        with open(outfile, "a", encoding="utf-8") as fh:
            r = subprocess.run([sys.executable, "-c", self.script],
                               cwd=self.tmp, stdout=fh,
                               stderr=subprocess.PIPE, text=True, env=env)
        with open(outfile, encoding="utf-8") as fh:
            out = fh.read().strip()
        return r.returncode, out, r.stderr

    # ── 核心分支 ────────────────────────────────────

    def test_skip_when_already_crawled_today(self):
        today = datetime.now(timezone.utc).astimezone(TZ8).strftime("%Y-%m-%d")
        self._seed(f"{today} 09:07:00", fetched=455, dtot=455)
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("done=true", out, "今天已完整抓过应跳过，避免重复快照")

    def test_run_when_last_crawl_was_yesterday(self):
        y = (datetime.now(timezone.utc).astimezone(TZ8) - timedelta(days=1)).strftime("%Y-%m-%d")
        self._seed(f"{y} 09:07:00", fetched=455, dtot=455)
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
        self._seed(f"{y} 23:59:59", fetched=455, dtot=455)
        rc, out, _ = self._run()
        self.assertIn("done=false", out)

    def test_incomplete_same_day_crawl_must_rerun(self):
        """当天抓过但没抓全（fetched < dtot）必须重抓。

        真实风险：09:07 那档抓一半崩了仍写快照，若守卫只看日期，
        09:27/10:07/10:37 三档全部跳过 -> 当天数据永久缺页且不自愈。
        """
        today = datetime.now(timezone.utc).astimezone(TZ8).strftime("%Y-%m-%d")
        self._seed(f"{today} 09:07:00", fetched=120, dtot=455)
        rc, out, err = self._run()
        self.assertIn("done=false", out, "抓一半也必须重抓")
        self.assertIn("已完成=0", err)

    def test_zero_dtot_must_rerun(self):
        """dtot=0 且一行都没抓到，才算这条查询「真的空」；有行没总数仍要重抓。"""
        today = datetime.now(timezone.utc).astimezone(TZ8).strftime("%Y-%m-%d")
        self._seed(f"{today} 09:07:00", fetched=0, dtot=0)
        rc, out, _ = self._run()
        self.assertIn("done=true", out, "0 条查询确实空了，不该整天反复重抓")

        self._seed(f"{today} 09:07:00", fetched=20, dtot=0)
        rc, out, _ = self._run()
        self.assertIn("done=false", out, "抓到行但站点没报总数，无法确认完整性")

    def test_force_bypasses_guard(self):
        """手动触发带 force=true 时必须强制重抓。"""
        today = datetime.now(timezone.utc).astimezone(TZ8).strftime("%Y-%m-%d")
        self._seed(f"{today} 09:07:00", fetched=455, dtot=455)
        rc, out, err = self._run(force=True)
        self.assertIn("done=false", out, "force 应绕过「今天已抓」守卫")
        self.assertIn("force=True", err)

    def test_empty_snapshots_table(self):
        c = sqlite3.connect(self.db)
        c.executescript(
            "CREATE TABLE snapshots(id INTEGER PRIMARY KEY, taken_at TEXT,"
            " query TEXT, fetched INTEGER, dtot INTEGER);")
        c.commit(); c.close()
        rc, out, _ = self._run()
        self.assertIn("done=false", out, "表存在但无数据应继续抓")

    # ── 多查询（珺华赋大户型）────────────────────────

    def test_multi_query_partial_must_rerun(self):
        """只跑完一部分查询不能算今天抓完。

        真实风险：主查询失败但大户型成功，旧的「看最后一条快照」逻辑
        会判成今天已抓完，主查询当天永久缺数据。
        """
        today = datetime.now(timezone.utc).astimezone(TZ8).strftime("%Y-%m-%d")
        self._write_queries(2)
        self._seed(f"{today} 09:07:00", fetched=443, dtot=443, query="q1")
        rc, out, err = self._run()
        self.assertIn("done=false", out, "1/2 条查询完成，不能算抓完")
        self.assertIn("已完成=1", err)

    def test_multi_query_all_complete_skips(self):
        today = datetime.now(timezone.utc).astimezone(TZ8).strftime("%Y-%m-%d")
        self._write_queries(2)
        self._seed(f"{today} 09:07:00", fetched=443, dtot=443, query="q1")
        self._add_snapshot(f"{today} 09:08:00", "q2", 6, 6)
        rc, out, err = self._run()
        self.assertIn("done=true", out, "两条都完整才跳过")
        self.assertIn("已完成=2", err)

    def test_multi_query_one_incomplete_must_rerun(self):
        today = datetime.now(timezone.utc).astimezone(TZ8).strftime("%Y-%m-%d")
        self._write_queries(2)
        self._seed(f"{today} 09:07:00", fetched=443, dtot=443, query="q1")
        self._add_snapshot(f"{today} 09:08:00", "q2", 3, 6)
        rc, out, err = self._run()
        self.assertIn("done=false", out, "有一条没抓全就要重跑")


def extract_push_retry_block():
    """抽出 workflow 里「commit 后 push 重试」那段 bash。"""
    src = open(WF, encoding="utf-8").read()
    m = re.search(r"(?m)^(\s+)ok=0\n((?:\1.*\n)*?\1\[ \"\$ok\" = 1 \].*\n)", src)
    if not m:
        raise RuntimeError("workflow 里找不到 push 重试块（结构被改动？）")
    indent = m.group(1)
    block = "ok=0\n" + m.group(2)
    return "\n".join(l[len(indent):] if l.startswith(indent) else l
                     for l in block.splitlines())


class TestPushRetryBehavior(unittest.TestCase):
    """真跑重试脚本（桩 git），而不是在 YAML 里搜字符串。

    字符串断言挡不住「把 ok=1 写在循环外」「exit 1 在永远到不了的分支里」这类
    假修复；只有执行一遍看退出码和 git 调用次数才作数。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pushretry_")
        self.block = extract_push_retry_block()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, fail_on=""):
        """用 bash 函数桩掉 git/sleep，再把 workflow 原文喂给 bash -s 执行。

        注意别用 `bash -c <script>`：本机 bash 解析到 WSL 的 /bin/bash，
        `-c` 传参时脚本里的 $VAR 会诡异地展开成空（实测），换 stdin 才稳定。
        """
        script = ("git(){ echo \"$@\" >> calls.txt; "
                  "case \"$*\" in *\"%s\"*) [ -n \"%s\" ] && return 1;; esac; "
                  "return 0; }\n"
                  "sleep(){ :; }\n" % (fail_on, fail_on)) + self.block + "\n"
        r = subprocess.run(["bash", "-e", "-s"], input=script.encode("utf-8"),
                           capture_output=True, cwd=self.tmp, timeout=120)
        calls = []
        path = os.path.join(self.tmp, "calls.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                calls = [l for l in f.read().splitlines() if l.strip()]
        return r, calls

    @staticmethod
    def _out(r):
        return (r.stdout or b"").decode("utf-8", "replace") + \
               (r.stderr or b"").decode("utf-8", "replace")

    def test_push_all_ok_exits_zero(self):
        if not shutil.which("bash"):
            self.skipTest("没有 bash")
        r, calls = self._run()
        self.assertEqual(r.returncode, 0, self._out(r))
        self.assertEqual(len(calls), 2, f"应恰好 pull+push 各一次，实际 {calls}")

    def test_pull_fails_three_times_exits_nonzero(self):
        if not shutil.which("bash"):
            self.skipTest("没有 bash")
        r, calls = self._run(fail_on="pull")
        self.assertEqual(r.returncode, 1, "3 次失败后必须非零退出，否则数据没上传却显示绿色")
        self.assertIn("连续 3 次推送失败", self._out(r))
        self.assertEqual(len(calls), 3, f"应恰好重试 3 轮，实际 {calls}")

    def test_push_fails_but_pull_ok_retries_three_times(self):
        if not shutil.which("bash"):
            self.skipTest("没有 bash")
        r, calls = self._run(fail_on="push")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(sum(1 for c in calls if c.startswith("pull")), 3)
        self.assertEqual(sum(1 for c in calls if c.startswith("push")), 3)


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

    def test_push_retry_fails_step_after_three_failures(self):
        """回归：循环最后一条是 sleep 时，3 次 push 全失败也会以退出码 0 结束。"""
        self.assertIn("ok=0", self.src)
        self.assertIn("if git pull --rebase --autostash origin main && git push; then", self.src)
        self.assertIn("ok=1", self.src)
        self.assertIn('[ "$ok" = 1 ] ||', self.src)
        self.assertIn("exit 1", self.src.split('[ "$ok" = 1 ] ||', 1)[1],
                      "push 全失败后必须非零退出，否则 CI 绿色但数据没上传")


if __name__ == "__main__":
    unittest.main(verbosity=2)
