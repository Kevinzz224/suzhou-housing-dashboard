# DeepSeek 4.1 flash 验收报告

独立验收：不采信 `REVIEW_GLM.md` / `FIX_KIMI.md` 的结论，全部自己跑、自己看、自己判断。
验收对象是当前工作区（HEAD `ae9847b` + 本次改动）。**未执行 git commit / push。**

---

## 测试结果（真实输出）

命令：`CHROME_PATH=C:\Users\Kevinzz224\AppData\Local\hermes\tools\chromium-1208\chrome-win64\chrome.exe bash tests/run_all.sh`
末尾输出：`✓ 全部测试通过，可以部署`（退出码 0）

| 套件 | 用例数 | 结果 | 备注 |
|---|---|---|---|
| 单元测试 `test_unit.py` | 78（原 66，本次 +12） | OK（9.1s） | 仅 ResourceWarning（serve.py:93 未关配置文件句柄），不影响结果 |
| CI 工作流守卫 `test_workflow_guard.py` | 15（原 12，本次 +3） | OK（1.2s） | 新增 3 条**执行**型 push 重试用例（原来只有字符串匹配） |
| 前端静态检查 `test_frontend_lint.py` | 20 项检查 | 20/20 ✓ | `✓ 前端静态检查全部通过` |
| 端到端 `test_e2e.py` | 32（原 27，本次 +5） | OK（14.0s） | 新增 2 条 XSS 注入用例 + 3 条 api 模式用例（真 serve.py + 真浏览器） |

E2E 逐条结果（节选，均为真实 ok）：
```
test_a1_api_mode_renders ... ok
test_a2_detail_matches_api_payload ... ok
test_a3_subscribe_and_delete_roundtrip_server ... ok
test_01_page_loads_with_data ... ok
test_05_card_render_escapes_gov_fields ... ok
test_21_detail_shows_real_project_data ... ok
test_22_detail_render_escapes_gov_fields ... ok
test_31_delete_button_removes_chip ... ok
test_50_no_alive_zero_in_trend ... ok
test_61_card_star_toggles_subscription ... ok
test_76_no_suggestion_for_gibberish ... ok
Ran 32 tests in 14.040s / OK
```

**没有编造任何通过项。** E2E 在本机可跑（Chromium 1208 自动发现），未出现 skip。

---

## 测试有效性审计

### 结论：原测试覆盖了主路径，但存在 4 处会「假通过」的弱断言，已全部补强

| # | 位置 | 原问题（为什么会假通过） | 本次改法 |
|---|---|---|---|
| 1 | `tests/test_e2e.py:238` `visible()` | ① 元素不存在时返回 `False`，而调用处是 `assertFalse(visible('#detailView'))` —— id 写错/被删也会绿；② 只查元素自身 computed style，祖先 `display:none` 时元素自身仍是 `block`，会被判成「可见」 | 元素不存在直接 `throw`（调用即失败）；改用 `getClientRects().length` 判真实占位，并补 `opacity:0` |
| 2 | `tests/test_e2e.py` `test_31` 删除按钮 | 只断言「chip 消失 + 内存 `localSubs()` 为 0」。若删除只重渲染内存、没写回 localStorage，测试照样绿 | 增加：chip 文案必须是刚订阅的关键词、订阅项必须有 id、`localStorage['sz_subs_v1']` 必须真空、**刷新页面后不复活** |
| 3 | `tests/test_e2e.py` `test_61` 星标拦截 | 只断言 `visible('#detailView')===false` + `view==='list'`；星级按钮可能订阅到「别的项目」也过 | 增加：断言订阅的是**被点那张卡**（`closest('.card').dataset.p`）、置顶卡片星标同属该项目、详情页 `innerText` 必须为空（渲染过就算 bug）、重渲染不丢卡片 |
| 4 | `tests/test_workflow_guard.py:228` push 重试（原字符串断言） | 纯字符串匹配（`ok=0`/`ok=1`/`[ "$ok" = 1 ]` 存在即可）。把 `ok=1` 写错位置、`exit 1` 放进死分支都能过 | 新增执行型用例 `TestPushRetryBehavior`（`:129`）：从 YAML 抽出该 bash 块，用 shell 函数桩掉 git/sleep，断言 3 种情形的退出码与 git 调用次数（全成功=2 次调用/rc 0；pull 连失=3 次调用/rc 1；pull 成功 push 失=6 次调用/rc 1） |

### 新增覆盖（原来为零的缺口）

- `tests/test_e2e.py:test_05` / `test_22`：**XSS 注入实测**。往 `PRO` / `CACHE.project_details` 塞 `<img onerror>`，跑真实渲染，断言 DOM 里没有冒出新 `img/script`、`window.__xss*` 未被执行。原来是「源码里出现 esc() 就算过」的静态弱断言。
- `tests/test_e2e.py:ApiModeE2E`：**api 模式（本地 serve.py）零覆盖**这条缺口。拉起真 `serve.py`（临时库副本）+ 真浏览器，断言 `MODE==='api'`、详情页套数/在售行数/曲线点数与 `/api/project` 完全一致、订阅落库且删除能打到服务端。
- `tests/test_unit.py:TestProjectSnapshotsBoundaries`：末点/边界三连（`sold_at == taken_at` 同秒口径、`days` 跨日窗口含 cutoff、复活回溯改写历史点）。
- `tests/test_unit.py:test_upsert_keeps_first_seen_at`：`first_seen_at` 重抓不得被刷新（曲线全靠它反推）。
- `tests/test_unit.py:TestParseDays`：`0/-1/abc/1.5/空格` 必须抛错（HTTP 层才有 400 而不是 500）。
- `tests/test_unit.py:test_create_subscription_returns_server_id`：POST 必须返回真实服务端 id 且对应落库行；删后 GET 必须为空。
- `tests/test_unit.py:TestRealDataIntegrity`：拿真实 `data/housing.db`（复制到临时目录后只读）跑 `export_static.build()`，直接断言最终产物的 5 条不变量（见下节）。

### 补强不是「写完就算」——做了三次变异验证（改坏 → 测试必须红）

| 变异 | 结果 |
|---|---|
| `web/index.html:323` 把 `${esc(r.area)}` 改回 `${r.area}` | `test_22` 失败：`AssertionError: 1 != 0 : 详情页里冒出了 img/script 元素` ✓ |
| `web/index.html:244` 把 `${esc(p.company||'')}` 改回不转义 | `test_05` 失败：`AssertionError: 1 != 0 : 卡片里冒出了 img/script 元素` ✓ |
| `scripts/serve.py:396` 改回只返回 `{"ok": true}`（不回 id） | `test_a3` 失败：等待 `id.startsWith('r')` 超时 ✓ |

三处均已恢复并重跑全绿。

---

## 代码复查

### 已发现并**本次修复**的问题

1. **[中] api 模式下「取消订阅」打不到服务端，刷新后订阅复活**（`scripts/serve.py:384-396` + `web/index.html:513-535`、`538`）
   - 触发条件：本地跑 `serve.py`（`MODE==='api'`）→ 订阅（POST 落库）→ 点 chip 的 ×。
   - 根因：`addSub()` 本地记录 id 是 `'L'+Date.now()`，而 `delSub()` 只在 id 以 `'r'` 开头时才调 `/api/subscriptions/delete`；POST 又不回服务端 id，于是本地删了、服务端 `active` 仍是 1，下次 `load()` → `mergeSubs()` 把这条合并回来 —— 删除形同虚设（和历史上静态模式那个「删了又复活」是同一类 bug，只是换了个分支）。
   - 修法：POST 返回 `{ok, id: lastrowid}`；`addSub()` 拿到后把本地 id 改成 `'r'+id` 并 `renderSubs()` 重渲染（chip 的 `data-del` 是渲染时写入的，不重渲染仍是旧 id）；`delSub()` 原逻辑即生效。
   - 回归测试：`test_a3_subscribe_and_delete_roundtrip_server`（变异验证会红，见上）。

2. **[低] 详情页表格的「面积 / 成交日期」未转义**（`web/index.html:323`、`329`）
   - 尽管当前 `suzhou.json` 的 `row_regex` 把面积限定为 `[\d.]+`、`sold_at` 由本地 `now_cn()` 生成，暂无注入路径；但详情页其余字段都过了 `esc()`，漏这两处属同类面（接入别的站点、或正则放宽即成立）。
   - 修法：两处套 `esc()`；同时把 `esc()` 改为 `String(s==null?'':s)` 以免将来对数字调用时抛 `TypeError`。
   - 回归测试：`test_22`（变异验证会红）。

### 未修，需主控决策 / 下一轮处理

3. **[中] 最后一次快照之后成交 → 曲线末点 > 当前在售数**（`scripts/serve.py:151-179`）
   - 触发条件：`mark_sold()` 已写 `sold_at`（`webforms_crawl.py:481`），但在 `INSERT INTO snapshots`（`:497`）之前进程崩了/被 kill，中间只隔一次 COUNT 查询。
   - 实测（临时库，1 套快照 10:00、成交 11:00）：
     `snapshots=[{'date':'2026-10-17','time':'10:00','alive':1}] alive=0 → 末点 != alive: True`
   - 影响：详情页曲线末点比真实在售多 1，且会一直错到下一次快照；「末点必须等于 alive」这条不变量在这条路径上不成立（现有数据 30/30 成立，只是运气好没崩过）。
   - 建议修法：`project_snapshots()` 末尾若 `max(taken_at) < now` 则追加一个 `{"time":"现在","alive":当前在售}` 的合成点，或把快照写入放在 `mark_sold` 之前（先记快照再判死，语义上快照即「抓取时的在售」）。

4. **[中] CI「当天已抓」守卫只判日期、不判完整性**（`.github/workflows/daily-crawl.yml:41-66`）
   - 触发条件：09:07 那档抓到一半（分页失败/超时/进程被杀）—— 它仍会写一条快照（`webforms_crawl.py:496` 无条件 INSERT），于是 09:27/10:07/10:37 三档全部 `done=true` 跳过，当天数据永久缺页且不会自愈。
   - 附带：`workflow_dispatch` 手动触发同样被守卫拦住（没有 `force` 入口），想重抓只能改库/删快照。
   - 现有守卫只保证「不会误标已售」（`disappearance_guard` 那一层是对的），不保证「当天抓全」。
   - 建议修法：`done = 最后快照日期==今天 且 fetched>=dtot>0`（`snapshots` 已有 `fetched`/`dtot` 两列，字段现成）；再给 `workflow_dispatch` 加一个 `force` input。

5. **[中] 历史行的时区订正仍未做（不只是「显示无感」）**（`data/housing.db`：`status_changes id=1.changed_at` 与 `listings(key_id=53754.10.1302_MD001).sold_at`）
   - 实测值：`changed_at='2026-10-17 02:17:03'`，同轮快照 `taken_at='2026-10-17 10:17:03'` —— 该行按 UTC 落库，差 8 小时。
   - 影响面比 FIX_KIMI.md 描述的更大：`sold_at` 是 `sold_today` 与项目曲线的输入。当前这行恰好在同日 02:17，所以「今日成交=1」仍对；但若成交发生在北京时间 00:00–08:00（=UTC 前一天 16:00–24:00），`sold_at` 的日期会落到前一天 → 「今日成交」少 1、项目曲线把成交记早一天。属数据订正任务，代码链路已统一 `now_cn()`。

### 轻微（未改，记录在案）

6. `scripts/serve.py:170` 注释「同一分钟内视为同一时刻」与实现不符：代码是 `sd > t` 的严格秒级比较（同秒即判已售）。本次已用 `test_sold_at_same_second_as_snapshot_is_not_alive` 把口径钉死，建议改注释。
7. 复活回溯：`listings` 只存最终状态，`sold_at` 被清空后无法回放「售出→再上架」的空窗，历史点会把它算成一直在售（`TestProjectSnapshotsBoundaries.test_revival_is_retroactive_known_tradeoff` 已按当前口径固定）。要做对需要 availability 区间表或按 `status_changes` 重建。
8. `scripts/serve.py:425` 的建表 DDL 与 `webforms_crawl.init_db()` 漂移：缺 `listings.query`、`snapshots.fetched/dtot`。实测真实库 `PRAGMA table_info(listings)` 确实没有 `query`（下次爬虫跑 `init_db()` 才补）。单独用 serve.py 建的空库，其后 `mark_sold(query=...)` 前的补列依赖 crawler，别处直连会缺列。
9. `api_stats.last_delta.sold` 语义无意义：`snapshots.sold` 存的是「本轮新检出数」而非累计，两次相减得 -1（实测 `last_delta={'alive':0,'sold':-1}`）。前端未用它（`web/index.html:216-217` 的 `d`/`arrow` 是死代码）。
10. 文档漂移：`README.md:13` 仍写「静态托管下订阅写入 `web/subscriptions.json`」（实际只存 localStorage，仓库里也没有该文件）；`README.md:4` 写「每日 9:00」，实际是 09:07/09:27/10:07/10:37 四档。
11. `scripts/notify.py` 未接入任何 workflow —— 订阅推送实际不存在（订阅仅是本地收藏）。
12. `tests/run_all.sh:10-11`、`24-25` 每个套件跑两遍（一遍显示日志、一遍取退出码）。日志与退出码来自**两次不同运行**：显示的那次红了、判定的那次绿了，就会打印「全部通过」但滚动日志里全是 FAILED；同时耗时翻倍。建议改为跑一次、捕获状态码。
13. 数值类插值仍未转义（`${p.avg_area}`、`${d.sold_rate_7d}`、`fmt()` 输出等）。来源是服务端计算值，当前无注入路径；`esc()` 现已能安全接收数字，后续顺手包一层即可。

---

## 数据核对

命令：`python scripts/export_static.py --db data/housing.db --out <临时目录>/data_verify.json`
输出：`[export] 75.1 KB / 项目 30 个 | 详情 30 份 | 快照点 7 | 变化 1 | 订阅 1 / 在售 454 套 / 已售 1 套`

| 核对项 | 实际数字 | 结论 |
|---|---|---|
| 每个项目 `snapshots` 末点 == 该项目 `alive` | 30 个项目，0 个不一致 | ✅ |
| `stats.alive` == 所有项目 `alive` 之和 | 454 == 454（sum(total)=455 == stats.total=455；alive+sold=455） | ✅ |
| 是否存在 `alive=0` 的坏点 | 0 个 | ✅ |
| 推广名映射 | 11 条 `name_alias`，10 条生效；`庭樾雅园` 映射为空串按设计回退备案名；无「指向不存在项目」的死映射 | ✅ |
| `project_details` 覆盖 | 30/30，key 与项目名完全一致 | ✅ |
| 趋势点 | 7 点，全部 `alive>0` 且时间升序（10-16 23:05 → 10-17 10:22） | ✅ |
| 详情页数据 | 首卡（叠景星园 89 / 凤起潮鸣苑 78 / 天熠星湖雅苑 46）末点均等于 alive | ✅ |

以上 5 类不变量已固化为 `tests/test_unit.py:TestRealDataIntegrity`，今后数据被爬虫改坏会直接红。

---

## 结论

**可以发布（静态 Pages 部署链）**，但有 2 个条件 / 1 个建议：

- 条件 1（数据订正，独立小任务）：把 `data/housing.db` 里 `status_changes id=1.changed_at` 与对应 `listings.sold_at` 从 `2026-10-17 02:17:03` 订正为 `10:17:03`。不订正不会立刻出错，但任何发生在北京时间凌晨的成交都会算错日期。
- 条件 2（下一轮）：`daily-crawl.yml` 的当日守卫补「完整性」判据（`fetched >= dtot`），否则一次半途失败会让当天数据静默缺页且四档补跑全部跳过；同时给 `workflow_dispatch` 加 `force`。
- 建议：`project_snapshots()` 在「最后一次快照早于当前时间」时补一个「现在」点，消除「末点 > alive」这条结构性偏差。

本次已修复并带回归测试的：api 模式订阅删除不落库（刷新复活）、详情页两处未转义 + `esc()` 数字安全化。
本次只改测试与上述 3 个文件，未 commit / 未 push。工作区改动：
```
scripts/serve.py             |   6 +-      (POST 返回服务端 id)
web/index.html               |  21 +-      (esc 数字安全化 / 详情表格转义 / addSub 回填 r-id)
tests/test_e2e.py            | 283 +++++   (visible 加固 / test_31 / test_61 / XSS×2 / api 模式 3 条 / BrowserBase 拆分)
tests/test_unit.py           | 189 +++++   (边界×3 / first_seen_at / parse_days / POST id / 真数据自检)
tests/test_workflow_guard.py |  74 +++++   (push 重试执行型 3 条)
```
