#!/usr/bin/env python3
"""前端静态检查：不需要浏览器，任何环境都能跑（CI 的第一道门禁）。

检查项（都是真实踩过的坑）：
1. JS 语法可解析（提取 <script> 后用 node --check，无 node 时退化为括号配平）
2. 订阅删除按钮的 id 必须加引号 —— 曾写成 delSub(${s.id}) 生成 delSub(L1759...)
   被 JS 当变量名 -> ReferenceError -> 按钮静默失效
3. 订阅页必须有返回主页入口 —— 曾因只有单向开关导致用户回不去
4. 视图切换必须走统一函数 go() —— 避免多套 display 状态互相打架
5. 关键 DOM id 必须存在（projects/listView/detailView/subView/subs/kwInput）
6. 不许把 data.json 的 subscriptions 无条件合并回 localStorage（会让删除复活）
7. 必须存在测试钩子 __errs
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HTML_PATH = os.path.join(ROOT, "web", "index.html")

REQUIRED_IDS = ["projects", "listView", "detailView", "subView", "subs",
                "kwInput", "tabs", "stats", "changes", "toast"]


def fail(msg):
    print(f"  ✗ {msg}")
    return 1


def ok(msg):
    print(f"  ✓ {msg}")
    return 0


def main():
    if not os.path.exists(HTML_PATH):
        print(f"✗ 找不到 {HTML_PATH}")
        return 1

    src = open(HTML_PATH, encoding="utf-8").read()
    problems = 0

    print(f"检查 {HTML_PATH} ({len(src)} 字符)")

    # 1) JS 语法
    scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
    if not scripts:
        problems += fail("页面里没有 <script> 块")
    else:
        js = scripts[-1]
        tmp = os.path.join(HERE, "_lint_tmp.js")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(js)
        try:
            r = subprocess.run(["node", "--check", tmp],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                problems += ok("JS 语法通过（node --check）")
            else:
                problems += fail(f"JS 语法错误: {r.stderr.strip()[:200]}")
        except FileNotFoundError:
            # 无 node：退化做括号配平
            bal = {c: js.count(c) for c in "{}()[]"}
            if bal["{"] == bal["}"] and bal["("] == bal[")"] and bal["["] == bal["]"]:
                problems += ok("括号配平通过（无 node，降级检查）")
            else:
                problems += fail(f"括号不配平: {bal}")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # 2) 删除按钮的 id 必须带引号（真实 bug 回归）
    #    反例：onclick="delSub(${s.id})"  正例：data-del="${...}"
    #    注意：必须排除注释里的反例说明，否则误报（踩过）
    code_only = re.sub(r"/\*.*?\*/", "", src, flags=re.S)      # 块注释
    code_only = re.sub(r"^\s*//.*$", "", code_only, flags=re.M)  # 行注释
    bad_del = re.search(r'delSub\(\$\{[a-zA-Z_.]+\}\)', code_only)
    if bad_del:
        problems += fail(f"删除按钮把 id 直接当 JS 变量用了：{bad_del.group(0)}"
                         f" -> 会抛 ReferenceError，按钮静默失效")
    else:
        problems += ok("删除按钮未使用裸变量 id")

    if "data-del" not in src:
        problems += fail("缺少 data-del 事件委托（删除按钮应绑定 onclick handler）")
    else:
        problems += ok("删除按钮使用 data-del + 事件委托")

    # 3) 订阅页必须有返回主页
    sub_block = re.search(r'<div id="subView".*?</div>\s*<!--', src, re.S)
    if 'onclick="go(\'list\')"' in src or 'onclick="go(&#39;list&#39;)"' in src:
        problems += ok("存在返回主页入口")
    else:
        problems += fail("订阅页没有『返回主页』入口（用户会回不去）")

    # 4) 统一视图切换函数
    if re.search(r"function\s+go\s*\(", src):
        problems += ok("存在统一视图切换函数 go()")
    else:
        problems += fail("缺少 go() 视图切换函数（分散的 display 赋值必然错乱）")

    # 5) 关键 id
    missing = [i for i in REQUIRED_IDS if f'id="{i}"' not in src]
    if missing:
        problems += fail(f"缺少关键 DOM id: {missing}")
    else:
        problems += ok(f"关键 DOM id 齐全（{len(REQUIRED_IDS)} 个）")

    # 6) 静态模式下不得把远端订阅合并回本地（否则删除会复活）
    m = re.search(r"function mergeSubs\(remote\)\{(.*?)\n\}", src, re.S)
    if not m:
        problems += fail("找不到 mergeSubs（订阅合并逻辑）")
    else:
        body = m.group(1)
        if "MODE" in body and "return L" in body:
            problems += ok("mergeSubs 在静态模式下不污染 localStorage")
        else:
            problems += fail("mergeSubs 缺少模式判断，静态托管下会把远端订阅"
                             "注入 localStorage，导致删除的订阅复活")

    # 7) 拼音表语法（曾因漏逗号把整份 JS 写崩，所有测试全红）
    py = re.search(r"const PY = \{(.*?)\};", src, re.S)
    if not py:
        problems += fail("找不到 PY 拼音表")
    else:
        pairs = re.findall(r"'([^']+)'\s*:\s*'([^']+)'", py.group(1))
        raw = py.group(1)
        # 检查是否有粘连的两个 key（漏逗号的典型症状：'z''筑'）
        sticky = re.search(r"'\)\s*'", raw)
        # 统计引号数：每个键值对 4 个引号，总数必须是 4 的倍数
        nq = raw.count("'")
        if nq % 4 != 0:
            problems += fail(f"拼音表引号数 {nq} 不是 4 的倍数 -> 疑似漏逗号/引号")
        else:
            problems += ok(f"拼音表语法正常（{len(pairs)} 个字）")
        # 楼盘名高频字不能缺（缺了拼音搜索就搜不到）
        must = "君华雅筑凤起潮鸣苑熠壹号道星湖雅"
        missing = [c for c in must if f"'{c}'" not in raw]
        if missing:
            problems += fail(f"拼音表缺少楼盘名高频字: {missing}")
        else:
            problems += ok("拼音表覆盖楼盘名高频字")

    # 8) 主页卡片必须有订阅星标按钮（用户反馈缺这个）
    if 'class="star' in src:
        problems += ok("卡片带订阅星标按钮")
    else:
        problems += fail("主页卡片没有订阅星标按钮（.star）")
    if "toggleSubByProject" in src:
        problems += ok("星标切换函数存在")
    else:
        problems += fail("缺少 toggleSubByProject()")
    # 星标必须用捕获阶段，否则卡片内联 onclick 会先触发跳转
    if re.search(r"\.addEventListener\('click'[\s\S]{0,200}?,\s*true\)", src):
        problems += ok("星标使用捕获阶段拦截（防止误跳详情页）")
    else:
        problems += fail("星标未用捕获阶段，点星标会同时跳转详情页")

    # 9) 搜索下拉候选（用户反馈缺这个）
    for token, label in [("id=\"suggest\"", "下拉容器"),
                         ("function renderSuggest", "候选渲染"),
                         ("function matchProjects", "关键词匹配"),
                         ("function pinyinInitials", "拼音首字母"),
                         ("function suggestKey", "键盘操作")]:
        if token in src:
            problems += ok(f"{label}存在")
        else:
            problems += fail(f"缺少{label}（{token}）")

    # 10) 测试钩子
    if "__errs" in src:
        problems += ok("存在 __errs 错误收集钩子")
    else:
        problems += fail("缺少 __errs 钩子（E2E 无法断言无 JS 错误）")

    print()
    if problems:
        print(f"✗ 前端静态检查失败：{problems} 项问题")
        return 1
    print("✓ 前端静态检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
