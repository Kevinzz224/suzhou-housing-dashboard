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
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
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


class DashboardE2E(unittest.TestCase):
    """静态托管场景（GitHub Pages 同构）：web/ 目录 + data.json，无后端。"""

    @classmethod
    def setUpClass(cls):
        cls.chrome = find_chrome()
        if not cls.chrome:
            raise unittest.SkipTest("未找到 Chromium，跳过 E2E")

        # 1) 起静态服务器（模拟 Pages）
        os.chdir(WEB)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

        # 2) 起 headless Chromium
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

        # 3) 拿页面 target
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cls.cdp_port}/json", timeout=10) as r:
            targets = json.load(r)
        page = next(t for t in targets if t["type"] == "page")
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
        cls.httpd.shutdown()

    def setUp(self):
        self.cdp.eval("try{localStorage.clear()}catch(e){}")
        self.cdp.call("Page.navigate", url=self.base + "/index.html")
        self.wait_for("typeof window.go === 'function' && typeof PRO !== 'undefined'")
        # 等数据加载完（PRO 有内容）
        self.wait_for("Array.isArray(PRO) && PRO.length > 0", timeout=15)

    def visible(self, sel):
        """元素是否真的可见。必须用 computed style：
        内联 style.display 为空时，可能是 CSS 在隐藏（.detail{display:none}），
        只看内联会误判为"可见"（踩过）。"""
        return self.cdp.eval(
            f"(()=>{{const e=document.querySelector('{sel}');"
            f"if(!e) return false;"
            f"const s=getComputedStyle(e);"
            f"return s.display!=='none' && s.visibility!=='hidden';}})()")

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

    # ── 订阅：增删改查（之前的 bug 重灾区）────────────────

    def test_30_subscribe_adds_local_chip(self):
        self.cdp.eval("go('sub')")
        self.cdp.eval("document.querySelector('#kwInput').value='凤起潮鸣苑'")
        self.cdp.eval("document.querySelector('.sub-input button').click()")
        self.wait_for("document.querySelectorAll('#subs .chip').length === 1")
        self.assertEqual(self.cdp.eval("localSubs().length"), 1)
        self.assertEqual(self.cdp.eval("localSubs()[0].keyword"), "凤起潮鸣苑")

    def test_31_delete_button_removes_chip(self):
        """核心回归：删除按钮必须真的能删（曾因 id 未加引号变成变量名而静默失效）。"""
        self.cdp.eval("go('sub')")
        self.cdp.eval("document.querySelector('#kwInput').value='测试盘'")
        self.cdp.eval("document.querySelector('.sub-input button').click()")
        self.wait_for("document.querySelectorAll('#subs .chip').length === 1")
        # 点 ×
        self.cdp.eval("document.querySelector('#subs .chip button').click()")
        self.wait_for("document.querySelectorAll('#subs .chip').length === 0", timeout=8)
        self.assertEqual(self.cdp.eval("localSubs().length"), 0)

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
        """点星标要能订阅/取消，且不能触发卡片跳详情页。"""
        self.cdp.eval("document.querySelector('#projects .star').click()")
        self.wait_for("localSubs().length === 1")
        # 关键：不应跳到详情页（事件冒泡没阻止的话会跳）
        self.assertFalse(self.visible("#detailView"), "点星标不应触发卡片跳转详情页")
        self.assertEqual(self.cdp.eval("view"), "list", "点星标后视图应仍在列表")
        self.assertEqual(self.cdp.eval("document.querySelector('#projects .star').innerText"), "★")
        # 再点一次取消
        self.cdp.eval("document.querySelector('#projects .star').click()")
        self.wait_for("localSubs().length === 0")
        self.assertEqual(self.cdp.eval("document.querySelector('#projects .star').innerText"), "☆")

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


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=True)
