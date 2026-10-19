#!/usr/bin/env python3
"""端到端测试：用真实 Chromium 跑 web/index.html，验证交互（零依赖，走 CDP）。

为什么要 E2E：之前的两个 bug（删除按钮无反应、订阅页回不去主页）
纯靠读代码根本看不出来 —— 一个是模板字符串生成 delSub(L1759...) 当变量名，
一个是视图切换没有出口。只有真点一遍才能发现。

跑法:
    python tests/test_e2e.py
退出码非 0 表示有失败，可直接用于 CI 门禁。

设计:
- 用 tools/chromium-*/chrome-win64/chrome.exe（Hermes 自带），起 headless + CDP
- 用 WebSocket 直连 DevTools 协议（纯标准库，不装 puppeteer/playwright）
- 静态托管场景：起一个 http.server 服务 web/ 目录（模拟 GitHub Pages）
"""
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
WEB = os.path.join(ROOT, "web")
TOOLS = os.path.abspath(os.path.join(ROOT, "..", "..", "..", "tools"))


def find_chrome():
    """定位 Hermes 自带的 Chromium。"""
    cands = []
    for base in (TOOLS, os.path.expanduser("~/AppData/Local/hermes/tools")):
        if os.path.isdir(base):
            for name in os.listdir(base):
                if name.startswith("chromium-"):
                    p = os.path.join(base, name, "chrome-win64", "chrome.exe")
                    if os.path.exists(p):
                        cands.append(p)
    for env in ("CHROME_PATH", "CHROMIUM_PATH", "GOOGLE_CHROME_SHIM"):
        if os.environ.get(env) and os.path.exists(os.environ[env]):
            cands.append(os.environ[env])
    # Linux（CI runner）
    for p in ("/usr/bin/chromium-browser", "/usr/bin/chromium",
              "/usr/bin/google-chrome", "/snap/bin/chromium"):
        if os.path.exists(p):
            cands.append(p)
    return cands[0] if cands else None


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class CDPClient:
    """极简 Chrome DevTools Protocol 客户端（WebSocket 手写帧，零依赖）。"""

    def __init__(self, ws_url, timeout=20):
        # ws://127.0.0.1:PORT/devtools/page/XXX
        path = ws_url.split("://", 1)[1]
        hostport, _, resource = path.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{resource} HTTP/1.1\r\nHost: {hostport}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        assert b"101" in buf.split(b"\r\n")[0], f"WebSocket 握手失败: {buf[:120]}"
        self._id = 0
        self._buf = buf.split(b"\r\n\r\n", 1)[1]

    def _send(self, payload):
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        header = bytes([0x81])
        n = len(data)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < (1 << 16):
            header += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            header += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def _recv_frame(self):
        def rd(n):
            out = b""
            while len(out) < n:
                chunk = self.sock.recv(n - len(out))
                if not chunk:
                    raise ConnectionError("socket closed")
                out += chunk
            return out
        b1, b2 = rd(2)
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(rd(2), "big")
        elif length == 127:
            length = int.from_bytes(rd(8), "big")
        return rd(length).decode("utf-8", "ignore")

    def call(self, method, **params):
        self._id += 1
        mid = self._id
        self._send({"id": mid, "method": method, "params": params})
        deadline = time.time() + 25
        while time.time() < deadline:
            msg = json.loads(self._recv_frame())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"{method} 超时")

    def eval(self, expr):
        r = self.call("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError("JS 异常: " + str(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class BrowserBase(unittest.TestCase):
    """只负责起 headless Chromium + CDP 连接；页面从哪来由子类决定。

    拆出基类的目的：静态托管（Pages）和本地 serve.py 两种模式要各跑一遍，
    之前把浏览器生命周期写在 DashboardE2E 里，想复用就只能继承它 —— 那样
    会连它的用例一起继承（父类的静态模式断言在 api 模式下必然失败）。
    """

    @classmethod
    def setUpClass(cls):
        cls.chrome = find_chrome()
        if not cls.chrome:
            raise unittest.SkipTest("未找到 Chromium，跳过 E2E")

        # 起 headless Chromium
        cls.profile = tempfile.mkdtemp(prefix="e2e_chrome_")
        # 部分 Chromium 构建（如 snap 版）不支持 --headless=new，用环境变量可切旧版
        headless = os.environ.get("E2E_HEADLESS", "new")
        args = [
            cls.chrome, f"--headless={headless}", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions",
            # Linux CI（root 用户 + 容器）必须关沙箱，否则进程直接退出，
            # 表现为读不到 DevToolsActivePort（CI 实战踩到）
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",          # 容器 /dev/shm 太小
            "--disable-background-networking",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={cls.profile}",
            "--window-size=390,844", "about:blank",
        ]
        # 日志留着，启动失败时能看出原因（而不是只说"读不到端口"）
        cls.chrome_log = os.path.join(cls.profile, "chrome.log")
        cls._log_fh = open(cls.chrome_log, "wb")
        cls.proc = subprocess.Popen(args, stdout=cls._log_fh, stderr=cls._log_fh)

        # 从 DevToolsActivePort 文件读真实端口（最多等 90 秒，CI 慢）
        port_file = os.path.join(cls.profile, "DevToolsActivePort")
        for _ in range(360):
            if os.path.exists(port_file):
                with open(port_file) as f:
                    first = f.read().splitlines()
                if first:
                    cls.cdp_port = int(first[0])
                    break
            if cls.proc.poll() is not None:      # 进程已退出，别白等
                break
            time.sleep(0.25)
        else:
            cls.cdp_port = None

        if not getattr(cls, "cdp_port", None):
            log = ""
            try:
                with open(cls.chrome_log, "rb") as f:
                    log = f.read().decode("utf-8", "ignore")[-1500:]
            except OSError:
                pass
            raise RuntimeError(
                f"Chromium 未启动（binary={cls.chrome}, rc={cls.proc.poll()}）\n"
                f"--- chrome 日志 ---\n{log}")

        # 拿页面 target。Chromium 刚启动时 /json 里可能还没有 page（尤其在
        # 机器上已有其它 chrome 实例、启动偏慢时），单次 next() 会直接
        # StopIteration —— 这是脚手架竞态，不是被测代码的问题，必须轮询等。
        page = None
        targets = []
        for _ in range(40):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{cls.cdp_port}/json", timeout=10) as r:
                    targets = json.load(r)
            except Exception:
                targets = []
            page = next((t for t in targets if t.get("type") == "page"), None)
            if page:
                break
            if cls.proc.poll() is not None:
                break
            time.sleep(0.25)
        if not page:
            raise RuntimeError(
                f"Chromium 没有 page target（rc={cls.proc.poll()}，"
                f"targets={[t.get('type') for t in targets]}）")
        cls.cdp = CDPClient(page["webSocketDebuggerUrl"])
        cls.cdp.call("Page.enable")
        cls.cdp.call("Runtime.enable")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.cdp.close()
        except Exception:
            pass
        try:
            cls._log_fh.close()
        except Exception:
            pass
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def setUp(self):
        self.cdp.eval("try{localStorage.clear()}catch(e){}")
        self.cdp.call("Page.navigate", url=self.base + "/index.html")
        self.wait_for("typeof window.go === 'function' && typeof PRO !== 'undefined'")
        # 等数据加载完（PRO 有内容）
        self.wait_for("Array.isArray(PRO) && PRO.length > 0", timeout=15)

    def visible(self, sel):
        """元素是否真的可见 —— 三重判定，任一不满足都算不可见。

        坑（弱断言）：只看元素自身的 computed style 会漏掉两种情况：
        ① 元素被 display:none 的祖先包着（自身 display 仍是 block）；
        ② 选择器根本没匹配到元素（返回 False，会让 assertFalse 假通过）。
        所以这里元素不存在直接抛错，可见性用 getClientRects() 判真实占位。"""
        return self.cdp.eval(
            f"""(()=>{{const e=document.querySelector('{sel}');
            if(!e) throw new Error('visible(): 元素不存在 {sel}');
            const s=getComputedStyle(e);
            if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
            return e.getClientRects().length>0;}})()""")

    def wait_for(self, expr, timeout=12):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self.cdp.eval(expr)
                if last:
                    return last
            except Exception as e:
                last = e
            time.sleep(0.15)
        raise AssertionError(f"等待超时: {expr!r} (最后值={last!r})")


class DashboardE2E(BrowserBase):
    """静态托管场景（GitHub Pages 同构）：web/ 目录 + data.json，无后端。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.chdir(WEB)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        super().tearDownClass()

    # ── 页面基础 ──────────────────────────────────────

    def test_01_page_loads_with_data(self):
        self.assertEqual(self.cdp.eval("document.title"), "苏州楼市 · 在售看板")
        self.assertGreater(self.cdp.eval("PRO.length"), 10)
        self.assertGreater(self.cdp.eval("D_STATS_ALIVE()"), 0)

    def test_02_static_mode_detected(self):
        """无后端时必须自动降级为静态模式（Pages 场景）。"""
        self.assertEqual(self.cdp.eval("MODE"), "static")

    def test_03_project_cards_rendered(self):
        n = self.cdp.eval("document.querySelectorAll('#projects .card').length")
        self.assertEqual(n, self.cdp.eval("PRO.length"))
        self.assertIn("在售", self.cdp.eval("document.querySelector('#projects').innerText"))

    def test_04_no_js_errors_on_load(self):
        errs = self.cdp.eval("(window.__errs||[]).length")
        self.assertEqual(errs, 0)

    def test_05_card_render_escapes_gov_fields(self):
        """楼盘名/公司名来自政务站，必须转义。

        只检查源码里出现 esc() 是弱断言（套错位置也会过）；这里直接往 PRO 塞一段
        带 onerror 的 HTML，跑真实渲染，然后看 DOM 里有没有冒出新元素、有没有执行。
        """
        self.cdp.eval("""(()=>{window.__p0=PRO.slice();
          PRO.push({project:'<img src=x onerror="window.__xss1=1">恶意盘',
            display:'<script>window.__xss2=1</script>',
            company:'<img src=x onerror="window.__xss3=1">皮包公司',
            alive:1,sold:0,total:1,sell_rate:0,avg_price:1,avg_area:1});renderList();})()""")
        time.sleep(0.4)
        self.assertEqual(
            self.cdp.eval("document.querySelectorAll('#projects img, #projects script').length"), 0,
            "卡片里冒出了 img/script 元素 —— esc() 没覆盖到该字段")
        txt = self.cdp.eval("document.querySelector('#projects').textContent")
        self.assertIn("恶意盘", txt)
        self.assertIn("<img", txt, "被转义后应作为纯文本出现，而不是变成元素")
        for v in ("__xss1", "__xss2", "__xss3"):
            self.assertEqual(self.cdp.eval(f"typeof window.{v}"), "undefined",
                             f"{v} 被执行了 —— 存在 XSS 注入面")
        self.cdp.eval("PRO=window.__p0;renderList()")

    # ── 排序 tabs ──────────────────────────────────────

    def test_10_sort_tabs_work(self):
        """四个排序 tab 都能切换，且列表数量不变（不丢卡片）。"""
        # HTML 用的是 data-sort 属性，值是全名（alive/rate/price/sold）
        for attr, key in (("rate", "rate"), ("price", "price"),
                          ("sold", "sold"), ("alive", "alive")):
            self.cdp.eval(
                f"document.querySelector('.tab[data-sort=\"{attr}\"]').click()")
            time.sleep(0.2)
            self.assertEqual(self.cdp.eval("sortKey"), key)
            self.assertEqual(
                self.cdp.eval("document.querySelectorAll('#projects .card').length"),
                self.cdp.eval("PRO.length"))

    def test_11_sort_actually_reorders(self):
        """排序必须真的改变顺序，不能只是高亮（假实现很容易蒙混过关）。"""
        self.cdp.eval("document.querySelector('.tab[data-sort=\"alive\"]').click()")
        time.sleep(0.25)
        by_alive = self.cdp.eval(
            "[...document.querySelectorAll('#projects .card')]"
            ".map(c=>parseInt(c.querySelector('.badge .n').innerText))")
        self.assertEqual(by_alive, sorted(by_alive, reverse=True),
                         "按在售排序时结果应递减")
        # 切换到均价，顺序应当不同（除非数据恰好同序）
        self.cdp.eval("document.querySelector('.tab[data-sort=\"price\"]').click()")
        time.sleep(0.25)
        prices = self.cdp.eval(
            "[...document.querySelectorAll('#projects .card')]"
            ".map(c=>parseInt(c.querySelectorAll('.metrics .m b')[0].innerText.replace(/[^0-9]/g,'')))")
        self.assertEqual(prices, sorted(prices, reverse=True),
                         "按均价排序时结果应递减")

    # ── 详情页 ────────────────────────────────────────

    def test_20_open_detail_and_back(self):
        self.cdp.eval("document.querySelector('#projects .card').click()")
        self.wait_for("document.querySelector('#detailView').style.display === 'block'")
        self.assertIn("返回列表", self.cdp.eval("document.querySelector('#detailView').innerText"))
        self.cdp.eval("document.querySelector('#detailView .back').click()")
        time.sleep(0.3)
        self.assertEqual(self.cdp.eval("document.querySelector('#listView').style.display"), "")
        self.assertEqual(self.cdp.eval("document.querySelector('#detailView').style.display"), "none")

    def test_21_detail_shows_real_project_data(self):
        """详情页不能只「能打开」：必须渲染本项目的数据（曲线点数/在售表/套数）。"""
        self.cdp.eval("window.__e2e_key=document.querySelector('#projects .card').dataset.p")
        self.cdp.eval("document.querySelector('#projects .card').click()")
        self.wait_for("document.querySelector('#detailView').style.display === 'block'")
        d = self.cdp.eval("CACHE.project_details[window.__e2e_key]")
        self.assertTrue(d, "data.json 里应有该项目的预计算详情")
        txt = self.cdp.eval("document.querySelector('#detailView').innerText")
        self.assertIn(f"{d['alive']} 套", txt)
        if d.get("alive_list"):
            self.assertIn(d["alive_list"][0]["location"], txt)
        snaps = d.get("snapshots") or []
        if len(snaps) >= 2:
            npts = self.cdp.eval(
                "(()=>{const p=document.querySelector('#detailView .spark polyline');"
                "return p ? p.getAttribute('points').trim().split(/\\s+/).length : 0})()")
            self.assertEqual(npts, len(snaps), "存量趋势点数应与快照数一致")
        else:
            self.assertIn("数据点不足", txt)

    def test_22_detail_render_escapes_gov_fields(self):
        """详情页的坐落/面积来自政务站，必须转义（存储型 XSS 面）。

        同样不看源码、看真实渲染：塞一条带 onerror 的房源进 project_details，
        打开详情页，断言 DOM 里没有新增 img/script、也没有脚本被执行。
        """
        self.cdp.eval("""(()=>{CACHE.project_details=CACHE.project_details||{};
          CACHE.project_details['__xss__']={
            project:'__xss__', display:'', company:'<img src=x onerror="window.__xssc=1">',
            alive:1, sold_total:0, listed_total:1, sell_rate:0,
            avg_price:1, min_price:1, max_price:1, avg_area:1,
            sold_7d:0, sold_30d:0, new_7d:0, new_30d:0,
            sold_rate_7d:0, sold_rate_30d:0, amount_30d_wan:0, days_to_clear:null,
            snapshots:[],
            alive_list:[{location:'<img src=x onerror="window.__xssl=1">',
                         area:'<img src=x onerror="window.__xssa=1">', price:1, layout:'x'}],
            recent_sold:[]};})()""")
        self.cdp.eval("openDetail('__xss__')")
        self.wait_for("document.querySelector('#detailView').style.display === 'block'")
        time.sleep(0.4)
        self.assertEqual(
            self.cdp.eval("document.querySelectorAll('#detailView img, #detailView script').length"),
            0, "详情页里冒出了 img/script 元素 —— 有字段没过 esc()")
        for v in ("__xssc", "__xssl", "__xssa"):
            self.assertEqual(self.cdp.eval(f"typeof window.{v}"), "undefined",
                             f"{v} 被执行了 —— 详情页存在 XSS 注入面")

    # ── 订阅：增删改查（之前的 bug 重灾区）────────────────

    def test_30_subscribe_adds_local_chip(self):
        self.cdp.eval("go('sub')")
        self.cdp.eval("document.querySelector('#kwInput').value='凤起潮鸣苑'")
        self.cdp.eval("document.querySelector('.sub-input button').click()")
        self.wait_for("document.querySelectorAll('#subs .chip').length === 1")
        self.assertEqual(self.cdp.eval("localSubs().length"), 1)
        self.assertEqual(self.cdp.eval("localSubs()[0].keyword"), "凤起潮鸣苑")

    def test_31_delete_button_removes_chip(self):
        """核心回归：删除按钮必须真的能删（曾因 id 未加引号变成变量名而静默失效）。

        加强点：光看「chip 消失」不够 —— 只重渲染内存、没写回 localStorage 也会绿。
        这里同时断言 ① chip 文案是刚订阅的关键词 ② localStorage 真被清空
        ③ 刷新页面后不复活。"""
        self.cdp.eval("go('sub')")
        self.cdp.eval("document.querySelector('#kwInput').value='测试盘'")
        self.cdp.eval("document.querySelector('.sub-input button').click()")
        self.wait_for("document.querySelectorAll('#subs .chip').length === 1")
        self.assertIn("测试盘", self.cdp.eval("document.querySelector('#subs .chip').innerText"),
                      "删除前 chip 应显示订阅关键词")
        sid = self.cdp.eval("String((localSubs()[0]||{}).id||'')")
        self.assertTrue(sid, "订阅项必须有 id，否则删除按钮无从定位")
        # 点 ×
        self.cdp.eval("document.querySelector('#subs .chip button').click()")
        self.wait_for("document.querySelectorAll('#subs .chip').length === 0", timeout=8)
        self.assertEqual(self.cdp.eval("localSubs().length"), 0)
        self.assertEqual(self.cdp.eval(
            "JSON.parse(localStorage.getItem('sz_subs_v1')||'[]').length"), 0,
            "删除必须写回 localStorage，不能只改内存里的 SUBS")
        # 刷新后不能复活
        self.cdp.call("Page.navigate", url=self.base + "/index.html")
        self.wait_for("typeof window.go === 'function'")
        self.wait_for("Array.isArray(PRO) && PRO.length > 0", timeout=15)
        self.assertEqual(self.cdp.eval("localSubs().length"), 0, "刷新后被删的订阅不能复活")
        self.cdp.eval("go('sub')")
        time.sleep(0.2)
        self.assertEqual(self.cdp.eval("document.querySelectorAll('#subs .chip').length"), 0)

    def test_32_subscribe_is_idempotent(self):
        self.cdp.eval("go('sub')")
        for _ in range(2):
            self.cdp.eval("document.querySelector('#kwInput').value='重复盘'")
            self.cdp.eval("document.querySelector('.sub-input button').click()")
            time.sleep(0.3)
        self.assertEqual(self.cdp.eval("localSubs().length"), 1)

    def test_33_subscribed_project_is_pinned(self):
        """订阅的楼盘必须置顶，且星标变实心 ★。

        注意：★ 现在位于卡片右上角的订阅按钮内（不再塞进楼盘名文本），
        断言也要跟着改 —— 否则测试会因为界面改版而假失败。"""
        self.cdp.eval("go('sub')")
        target = self.cdp.eval("PRO[PRO.length-1].project")   # 原先排最后的
        self.cdp.eval(f"document.querySelector('#kwInput').value={json.dumps(target)}")
        self.cdp.eval("document.querySelector('.sub-input button').click()")
        time.sleep(0.4)
        self.cdp.eval("go('list')")
        time.sleep(0.3)
        # 置顶：第一张卡片的 data-p 就是订阅项目
        first_key = self.cdp.eval("document.querySelector('#projects .card').dataset.p")
        self.assertEqual(first_key, target, "订阅的楼盘应排到列表最前")
        # 实心星标：第一张卡片的 .star 文本为 ★
        self.assertEqual(
            self.cdp.eval("document.querySelector('#projects .card .star').innerText"), "★",
            "已订阅的楼盘星标应为实心")

    # ── 订阅页导航（另一个 bug：回不去主页）────────────────

    def test_40_sub_view_has_back_home(self):
        self.cdp.eval("go('sub')")
        self.wait_for("document.querySelector('#subView').style.display === ''")
        self.assertIn("返回主页", self.cdp.eval("document.querySelector('#subView').innerText"))
        self.cdp.eval("document.querySelector('#subView .back').click()")
        time.sleep(0.3)
        self.assertEqual(self.cdp.eval("document.querySelector('#listView').style.display"), "")
        self.assertEqual(self.cdp.eval("document.querySelector('#subView').style.display"), "none")

    def test_41_header_nav_both_ways(self):
        """顶部导航：订阅 <-> 主页 双向都能切，且详情页不残留。"""
        self.cdp.eval("go('sub')")
        self.assertEqual(self.cdp.eval("document.querySelector('#subView').style.display"), "")
        self.cdp.eval("go('list')")
        self.assertEqual(self.cdp.eval("document.querySelector('#subView').style.display"), "none")
        self.assertEqual(self.cdp.eval("document.querySelector('#listView').style.display"), "")

    def test_42_detail_to_sub_to_list(self):
        """详情页 -> 订阅 -> 主页：之前这条路径会错乱。"""
        self.cdp.eval("document.querySelector('#projects .card').click()")
        self.wait_for("document.querySelector('#detailView').style.display === 'block'")
        self.cdp.eval("go('sub')")
        time.sleep(0.3)
        self.assertEqual(self.cdp.eval("document.querySelector('#detailView').style.display"), "none")
        self.assertEqual(self.cdp.eval("document.querySelector('#subView').style.display"), "")
        self.cdp.eval("go('list')")
        time.sleep(0.3)
        self.assertEqual(self.cdp.eval("document.querySelector('#listView').style.display"), "")
        self.assertEqual(self.cdp.eval("document.querySelector('#detailView').style.display"), "none")

    # ── 主页卡片订阅按钮（用户反馈缺这个）────────────────

    def test_60_card_has_subscribe_button(self):
        """主页每张卡片都要有 ☆ 订阅按钮（用户反馈没有）。"""
        n_cards = self.cdp.eval("document.querySelectorAll('#projects .card').length")
        n_stars = self.cdp.eval("document.querySelectorAll('#projects .star').length")
        self.assertEqual(n_stars, n_cards, "每张卡片都应有一个订阅星标按钮")
        self.assertEqual(self.cdp.eval("document.querySelector('#projects .star').innerText"), "☆")

    def test_61_card_star_toggles_subscription(self):
        """点星标要能订阅/取消，且不能触发卡片跳详情页。

        加强点：① 断言订阅的是「被点的那张卡」而不是别的项目；
        ② 详情页不仅 display:none，还不允许有任何内容（被渲染过就算 bug）；
        ③ 取消订阅后星标回 ☆，且列表卡片数不变（不能因为重渲染丢卡）。"""
        n_cards = self.cdp.eval("document.querySelectorAll('#projects .card').length")
        key = self.cdp.eval(
            "document.querySelector('#projects .star').closest('.card').dataset.p")
        self.assertTrue(key, "卡片必须有 data-p，否则无法确认订阅的是哪个项目")
        self.cdp.eval("document.querySelector('#projects .star').click()")
        self.wait_for("localSubs().length === 1")
        self.assertEqual(self.cdp.eval("localSubs()[0].keyword"), key,
                         "星标必须订阅它所在的那张卡片")
        # 关键：不应跳到详情页（事件冒泡没阻止的话会跳）
        self.assertFalse(self.visible("#detailView"), "点星标不应触发卡片跳转详情页")
        self.assertEqual(self.cdp.eval("view"), "list", "点星标后视图应仍在列表")
        self.assertEqual(self.cdp.eval("document.querySelector('#detailView').innerText"), "",
                         "详情页根本没被渲染，innerText 必须是空的")
        # 订阅后置顶的是同一个项目，星标实心
        self.assertEqual(self.cdp.eval("document.querySelector('#projects .card').dataset.p"), key,
                         "订阅的项目应置顶")
        self.assertEqual(
            self.cdp.eval("document.querySelector('#projects .star').dataset.sub"), key,
            "置顶卡片的星标应属于同一个项目")
        self.assertEqual(self.cdp.eval("document.querySelector('#projects .star').innerText"), "★")
        self.assertEqual(self.cdp.eval("document.querySelectorAll('#projects .card').length"),
                         n_cards, "重渲染后不能丢卡片")
        # 再点一次取消
        self.cdp.eval("document.querySelector('#projects .star').click()")
        self.wait_for("localSubs().length === 0")
        self.assertEqual(self.cdp.eval("document.querySelector('#projects .star').innerText"), "☆")
        self.assertFalse(self.visible("#detailView"), "取消订阅也不应跳详情页")

    # ── 搜索下拉候选（用户反馈缺这个）──────────────────

    def test_70_search_shows_suggestions(self):
        """输入关键词要弹出匹配下拉。"""
        self.cdp.eval("go('sub')")
        self.cdp.eval("""(()=>{const i=document.querySelector('#kwInput');
            i.value='凤起'; i.dispatchEvent(new Event('input',{bubbles:true}));})()""")
        self.wait_for("document.querySelector('#suggest').style.display === 'block'")
        n = self.cdp.eval("document.querySelectorAll('#suggest .sug-item').length")
        self.assertGreater(n, 0, "输入'凤起'应有候选")
        self.assertIn("凤起", self.cdp.eval("document.querySelector('#suggest').innerText"))

    def test_71_suggest_matches_alias_name(self):
        """推广名也要能搜到（凤起潮鸣苑 的推广名是 绿城凤起潮鸣）。"""
        self.cdp.eval("go('sub')")
        self.cdp.eval("""(()=>{const i=document.querySelector('#kwInput');
            i.value='道壹号'; i.dispatchEvent(new Event('input',{bubbles:true}));})()""")
        self.wait_for("document.querySelector('#suggest').style.display === 'block'", timeout=8)
        txt = self.cdp.eval("document.querySelector('#suggest').innerText")
        self.assertIn("苏州道壹号", txt)

    def test_72_suggest_matches_pinyin_initials(self):
        """拼音首字母匹配：jhyz -> 君华雅筑。"""
        self.cdp.eval("go('sub')")
        self.cdp.eval("""(()=>{const i=document.querySelector('#kwInput');
            i.value='jhyz'; i.dispatchEvent(new Event('input',{bubbles:true}));})()""")
        self.wait_for("document.querySelector('#suggest').style.display === 'block'", timeout=8)
        txt = self.cdp.eval("document.querySelector('#suggest').innerText")
        self.assertIn("君华雅筑", txt)

    def test_73_click_suggestion_fills_input(self):
        """点候选要填入输入框。"""
        self.cdp.eval("go('sub')")
        self.cdp.eval("""(()=>{const i=document.querySelector('#kwInput');
            i.value='君华'; i.dispatchEvent(new Event('input',{bubbles:true}));})()""")
        self.wait_for("document.querySelectorAll('#suggest .sug-item').length > 0")
        self.cdp.eval("document.querySelector('#suggest .sug-item').click()")
        time.sleep(0.2)
        self.assertEqual(self.cdp.eval("document.querySelector('#kwInput').value"), "君华雅筑")
        self.assertEqual(self.cdp.eval("document.querySelector('#suggest').style.display"), "none")

    def test_74_enter_subscribes_selected_suggestion(self):
        """下拉里按回车 = 订阅该楼盘（键盘流）。"""
        self.cdp.eval("go('sub')")
        self.cdp.eval("""(()=>{const i=document.querySelector('#kwInput');
            i.value='君华'; i.dispatchEvent(new Event('input',{bubbles:true}));})()""")
        self.wait_for("document.querySelectorAll('#suggest .sug-item').length > 0")
        self.cdp.eval("""(()=>{const i=document.querySelector('#kwInput');
            i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));})()""")
        self.wait_for("localSubs().length === 1", timeout=8)
        self.assertEqual(self.cdp.eval("localSubs()[0].keyword"), "君华雅筑")

    def test_75_suggest_hides_on_escape_and_blur(self):
        self.cdp.eval("go('sub')")
        self.cdp.eval("""(()=>{const i=document.querySelector('#kwInput');
            i.value='君华'; i.dispatchEvent(new Event('input',{bubbles:true}));})()""")
        self.wait_for("document.querySelector('#suggest').style.display === 'block'")
        self.cdp.eval("""(()=>{const i=document.querySelector('#kwInput');
            i.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));})()""")
        time.sleep(0.2)
        self.assertEqual(self.cdp.eval("document.querySelector('#suggest').style.display"), "none")

    def test_76_no_suggestion_for_gibberish(self):
        """无匹配时应隐藏下拉，不能弹空框。"""
        self.cdp.eval("go('sub')")
        self.cdp.eval("""(()=>{const i=document.querySelector('#kwInput');
            i.value='zzzzz不存在xx'; i.dispatchEvent(new Event('input',{bubbles:true}));})()""")
        time.sleep(0.3)
        self.assertEqual(self.cdp.eval("document.querySelector('#suggest').style.display"), "none")

    # ── 数据正确性 ────────────────────────────────────

    def test_50_no_alive_zero_in_trend(self):
        """历史坏快照（alive=0）不能画进曲线 —— 否则出现 455->0 的假暴跌。"""
        bad = self.cdp.eval(
            "Object.values(CACHE.project_details).reduce((n,d)=>"
            "n+(d.snapshots||[]).filter(s=>!s.alive).length,0)")
        self.assertEqual(bad, 0)

    def test_51_aliases_displayed(self):
        """推广名要显示，且备案名以副标题形式保留。"""
        txt = self.cdp.eval("document.querySelector('#projects').innerText")
        self.assertIn("保利珺华赋", txt)      # 君华雅筑 的推广名
        self.assertIn("备案名", txt)

    def test_52_subscriptions_persist_across_reload(self):
        self.cdp.eval("go('sub')")
        self.cdp.eval("document.querySelector('#kwInput').value='持久化盘'")
        self.cdp.eval("document.querySelector('.sub-input button').click()")
        self.wait_for("localSubs().length === 1")
        self.cdp.call("Page.navigate", url=self.base + "/index.html")
        self.wait_for("typeof window.go === 'function'")
        self.wait_for("Array.isArray(SUBS) && SUBS.length > 0", timeout=15)
        self.assertEqual(self.cdp.eval("localSubs()[0].keyword"), "持久化盘")

    # ── 手动抓取按钮（静态站的"后端"= GitHub Actions）────────
    def test_80_crawl_button_visible(self):
        """按钮必须在页面上真实可见可点，而不是只有 DOM 节点。"""
        rect = self.cdp.eval(
            "(() => { const b=document.querySelector('#crawlBtn');"
            " if(!b) return null;"
            " const r=b.getBoundingClientRect();"
            " return {w:r.width,h:r.height,dis:b.disabled}; })()")
        self.assertIsNotNone(rect, "缺少 #crawlBtn")
        self.assertGreater(rect["w"], 0, "按钮宽度为 0（不可见）")
        self.assertGreater(rect["h"], 0, "按钮高度为 0（不可见）")
        self.assertFalse(rect["dis"], "按钮不应默认禁用")

    def test_81_click_without_token_opens_modal(self):
        """没有 token 时点击必须弹输入层，不能静默失败或直接报错。"""
        self.cdp.eval("localStorage.removeItem('gh_pat_v1')")
        self.cdp.eval("document.querySelector('#crawlBtn').click()")
        self.wait_for(
            "document.querySelector('#patMask').classList.contains('on')",
            timeout=5)
        self.assertTrue(
            self.cdp.eval("document.querySelector('#patMask').classList.contains('on')"),
            "首次点击应弹 token 输入层")

    def test_82_cancel_closes_modal_without_triggering(self):
        """取消后弹层要关、状态要提示已取消，且不能真的发起抓取。

        注意：setUp 每个用例都会 localStorage.clear() + 重新导航，
        所以本用例必须自己先把弹层打开（不能依赖上一个用例的遗留状态）。
        """
        self.cdp.eval("localStorage.removeItem('gh_pat_v1')")
        self.cdp.eval("document.querySelector('#crawlBtn').click()")
        self.wait_for(
            "document.querySelector('#patMask').classList.contains('on')", timeout=5)

        self.cdp.eval("document.querySelector('#patCancel').click()")
        self.wait_for(
            "!document.querySelector('#patMask').classList.contains('on')", timeout=5)
        # 状态文本由 promise 续体异步写入，等它出现而不是立刻断言
        self.wait_for(
            "document.querySelector('#crawlStatus').textContent.includes('取消')",
            timeout=5)
        self.assertIn("取消",
                      self.cdp.eval("document.querySelector('#crawlStatus').textContent"))
        self.assertFalse(
            self.cdp.eval("document.querySelector('#crawlBtn').disabled"),
            "取消后按钮必须恢复可用")
        # 没给 token 就绝不能真的发起抓取
        self.assertFalse(
            self.cdp.eval("document.querySelector('#crawlBtn').textContent.includes('抓取中')"),
            "取消后不应进入抓取态")

    def test_84_network_failure_is_not_reported_as_token_error(self):
        """api.github.com 不可达时必须说「网络不通」，绝不能报「Token 无效」。

        两者处置方式完全不同：一个是网络/代理问题，一个是重新签发 token。
        报错了，用户会反复重填 token 却永远好不了。
        """
        self.cdp.eval("localStorage.setItem('gh_pat_v1','github_pat_fake_for_test')")
        self.cdp.eval(
            "window.__realFetch = window.fetch;"
            " window.fetch = function(){ return Promise.reject("
            "new TypeError('Failed to fetch')); };")
        try:
            self.cdp.eval("triggerCrawl()")
            self.wait_for(
                "document.querySelector('#crawlStatus').textContent.length > 0",
                timeout=10)
            txt = self.cdp.eval("document.querySelector('#crawlStatus').textContent")
            self.assertIn("网络不通", txt, f"网络失败应提示网络问题，实际={txt!r}")
            self.assertNotIn("Token", txt, f"网络失败不能甩锅给 token，实际={txt!r}")
            self.assertNotEqual(
                self.cdp.eval(
                    "getComputedStyle(document.querySelector('#actionsLink')).display"),
                "none", "失败时必须给出兜底入口（.h-sub 是 flex，inline 会被 blockify 成 block）")
        finally:
            self.cdp.eval("window.fetch = window.__realFetch")

    def test_85_trend_delta_label_includes_days(self):
        """存量趋势的差值必须带天数（「近4天 -2 套」），不能只写「-2 套」。

        只写数字会被读成当天暴跌 —— 用户已经当成 bug 报过两次。
        """
        self.cdp.eval(
            "openDetail(document.querySelector('#projects .card').dataset.p)")
        self.wait_for(
            "document.querySelector('#detailView').style.display === 'block'",
            timeout=10)
        lbl = self.cdp.eval(
            "(() => { const t=document.querySelector('#detailView svg text');"
            " return t ? t.textContent : null; })()")
        self.assertIsNotNone(lbl, "详情页应有趋势图")
        self.assertIn("近", lbl, f"差值标签要说明时间跨度，实际={lbl!r}")
        self.assertIn("天", lbl, f"差值标签要带天数，实际={lbl!r}")

    def test_83_page_embeds_no_real_token(self):
        """页面源码里绝不能出现真实 token —— 公开仓库等于全世界可读。

        这条是安全红线：一旦有人图省事把 token 写进 HTML，
        等于把仓库写权限公开。必须由门禁而不是自觉来守。
        """
        src = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
        for pat in (r"github_pat_[A-Za-z0-9_]{20,}", r"ghp_[A-Za-z0-9]{30,}"):
            self.assertIsNone(re.search(pat, src),
                              f"页面内嵌了疑似真实 token（{pat}）")


class ApiModeE2E(BrowserBase):
    """本地 serve.py 模式（MODE='api'）：真后端 + 真浏览器。

    为什么单独补：其余 E2E 全跑静态托管，serve.py 的 /api/* 只有单测直连，
    没有任何测试验证「前端在 api 模式下能渲染、订阅能落库且删除能打到服务端」。
    数据库用临时副本，绝不改真实 data/housing.db。

    只继承 BrowserBase（浏览器生命周期），不继承 DashboardE2E 的用例 ——
    静态模式的断言（localStorage 为空、MODE=static）在 api 模式下必然失败。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()            # headless Chromium + CDP
        src_db = os.path.join(ROOT, "data", "housing.db")
        if not os.path.exists(src_db):
            raise unittest.SkipTest("没有 data/housing.db，跳过 api 模式 E2E")
        cls.api_tmp = tempfile.mkdtemp(prefix="e2e_api_")
        cls.api_db = os.path.join(cls.api_tmp, "housing.db")
        shutil.copyfile(src_db, cls.api_db)

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.api_port = s.getsockname()[1]
        s.close()
        cls.api_base = f"http://127.0.0.1:{cls.api_port}"
        cls.api_log = os.path.join(cls.api_tmp, "serve.log")
        cls._api_fh = open(cls.api_log, "wb")
        cls.api_proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "scripts", "serve.py"),
             "--db", cls.api_db, "--host", "127.0.0.1", "--port", str(cls.api_port)],
            stdout=cls._api_fh, stderr=cls._api_fh)
        for _ in range(120):
            try:
                with urllib.request.urlopen(cls.api_base + "/api/stats", timeout=2) as r:
                    json.load(r)
                break
            except Exception:
                if cls.api_proc.poll() is not None:
                    break
                time.sleep(0.25)
        else:
            # 超时：进程可能半死不活，先收尸再抛错（tearDownClass 这时不会被调用）
            cls.api_proc.kill()
            cls.api_proc.wait(timeout=10)
            cls._api_fh.close()
        if cls.api_proc.poll() is not None:
            raise RuntimeError("serve.py 起不来（见 %s）" % cls.api_log)
        cls.base = cls.api_base        # 父类 setUp 据此导航到后端页面

    @classmethod
    def tearDownClass(cls):
        try:
            cls.api_proc.terminate()
            cls.api_proc.wait(timeout=10)
        except Exception:
            try:
                cls.api_proc.kill()
            except Exception:
                pass
        try:
            cls._api_fh.close()
        except Exception:
            pass
        shutil.rmtree(cls.api_tmp, ignore_errors=True)
        super().tearDownClass()

    def _api_json(self, path):
        with urllib.request.urlopen(self.api_base + path, timeout=10) as r:
            return json.load(r)

    def test_a1_api_mode_renders(self):
        self.assertEqual(self.cdp.eval("MODE"), "api", "有后端时必须走 api 模式")
        self.assertGreater(self.cdp.eval("PRO.length"), 10)
        self.assertEqual(self.cdp.eval("(window.__errs||[]).length"), 0)
        self.assertEqual(self.cdp.eval("document.querySelectorAll('#projects .card').length"),
                         self.cdp.eval("PRO.length"))

    def test_a2_detail_matches_api_payload(self):
        """详情页的套数/在售行/曲线点数必须与 /api/project 返回一致。"""
        key = self.cdp.eval("document.querySelector('#projects .card').dataset.p")
        d = self._api_json("/api/project?name=" + urllib.parse.quote(key) + "&days=30")
        self.cdp.eval("document.querySelector('#projects .card').click()")
        self.wait_for("document.querySelector('#detailView').style.display === 'block'")
        txt = self.cdp.eval("document.querySelector('#detailView').innerText")
        self.assertIn(f"{d['alive']} 套", txt)
        rows = self.cdp.eval("document.querySelectorAll('#detailView table')[0].rows.length")
        self.assertEqual(rows, len(d["alive_list"]) + 1, "在售表行数应等于 API 返回条数+表头")
        self.assertEqual(len(d["alive_list"]), min(d["alive"], 50))
        self.assertEqual(len(d["recent_sold"]), min(d["sold_total"], 10))
        if len(d["snapshots"]) >= 2:
            npts = self.cdp.eval(
                "(()=>{const p=document.querySelector('#detailView .spark polyline');"
                "return p?p.getAttribute('points').trim().split(/\\s+/).length:0})()")
            self.assertEqual(npts, len(d["snapshots"]))

    def test_a3_subscribe_and_delete_roundtrip_server(self):
        """api 模式：订阅落库；删除必须打到服务端，刷新不能复活。"""
        kw = "验收测试盘"
        kwjs = json.dumps(kw, ensure_ascii=False)
        self.cdp.eval("go('sub')")
        self.cdp.eval(f"document.querySelector('#kwInput').value={kwjs}")
        self.cdp.eval("document.querySelector('.sub-input button').click()")
        self.wait_for(f"localSubs().some(s=>s.keyword==={kwjs})")
        # 等服务端 id 回填（改成 'r'+id），否则删除打不到服务端
        self.wait_for(
            "String((localSubs().find(s=>s.keyword===" + kwjs + ")||{}).id||'').startsWith('r')",
            timeout=10)
        self.assertIn(kw, [s["keyword"] for s in self._api_json("/api/subscriptions")],
                      "订阅必须落库到服务端")
        # 精确点这一条的 ×（列表里可能还有别的订阅）
        self.cdp.eval(
            f"[...document.querySelectorAll('#subs .chip')].find(c=>c.innerText.includes({kwjs}))"
            ".querySelector('button').click()")
        self.wait_for(f"!localSubs().some(s=>s.keyword==={kwjs})")
        deadline = time.time() + 10
        while time.time() < deadline:
            if kw not in [s["keyword"] for s in self._api_json("/api/subscriptions")]:
                break
            time.sleep(0.2)
        self.assertNotIn(kw, [s["keyword"] for s in self._api_json("/api/subscriptions")],
                         "删除没打到服务端 —— 刷新后被删的订阅会复活")
        # 刷新验证不复活
        self.cdp.call("Page.navigate", url=self.base + "/index.html")
        self.wait_for("typeof window.go === 'function'")
        self.wait_for("Array.isArray(PRO) && PRO.length > 0", timeout=15)
        self.assertFalse(self.cdp.eval(f"localSubs().some(s=>s.keyword==={kwjs})"),
                         "刷新后被删的订阅复活了")


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=True)
