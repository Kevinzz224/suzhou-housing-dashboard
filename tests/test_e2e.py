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
    for env in ("CHROME_PATH", "CHROMIUM_PATH"):
        if os.environ.get(env) and os.path.exists(os.environ[env]):
            cands.append(os.environ[env])
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
        cls.proc = subprocess.Popen([
            cls.chrome, "--headless=new", "--disable-gpu", "--no-first-run",
            "--remote-debugging-port=0", f"--user-data-dir={cls.profile}",
            "--window-size=390,844", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 从 DevToolsActivePort 文件读真实端口
        port_file = os.path.join(cls.profile, "DevToolsActivePort")
        for _ in range(120):
            if os.path.exists(port_file):
                with open(port_file) as f:
                    cls.cdp_port = int(f.read().splitlines()[0])
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Chromium 未启动（读不到 DevToolsActivePort）")

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
        """订阅的楼盘必须置顶 + 加 ★。"""
        self.cdp.eval("go('sub')")
        target = self.cdp.eval("PRO[PRO.length-1].project")   # 原先排最后的
        self.cdp.eval(f"document.querySelector('#kwInput').value={json.dumps(target)}")
        self.cdp.eval("document.querySelector('.sub-input button').click()")
        time.sleep(0.4)
        self.cdp.eval("go('list')")
        time.sleep(0.3)
        first = self.cdp.eval("document.querySelector('#projects .card .pname').innerText")
        self.assertIn(target, first)
        self.assertIn("★", first)

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
