# GLM-5.3 代码审查报告

审查范围：`scripts/webforms_crawl.py`、`scripts/serve.py`、`scripts/export_static.py`、`web/index.html`、`tests/*`、`.github/workflows/*`。
验证手段：全部文件通读；`python tests/test_unit.py`（41 项通过）；`python tests/test_frontend_lint.py`（18 项通过）；对 `data/housing.db` 做只读 SQL 验证。未修改任何代码文件。

## 严重（会导致数据错误 / 功能失效）

- [scripts/webforms_crawl.py:262-276 + 332-335 + 357-363] `mark_sold` 的前提「本轮 seen_keys 覆盖该站点全部在售结果集」可被三种方式打破，任何一种都会把大批活房源误标成已售：① 手动跑 `--max-pages 5`（截断后 `failed_pages` 为空，第 340 行的守卫不拦）；② 332 行 `共\s*(\d+)\s*页` 正则失配时 `pages=1`，站点分页条文案一变就静默只抓第一页，其余 20+ 页全部判死；③ 换 `--param` 查询条件（如不同面积区间）——`mark_sold` 只按 site 名过滤，不看 `snapshots.query`，条件外的房源全部「消失」 | 触发条件：手动带 `--max-pages`/不同参数运行，或站点分页文案变化 | 建议修法：消失判定前校验「抓到行数 == dtot（或页数已知且全部成功）且本轮 query 与上轮一致」，不满足就跳过并记日志；`--max-pages` 非 0 时直接禁用消失判定。
- [scripts/webforms_crawl.py:212-214 + 262-276] 已售房源重新出现时 `sold_at` 永不清除：`db_upsert` 的 UPDATE 列来自 `r`（key_id/location/company/usage/layout/area/price），不含 `sold_at`，行已存在走 else 分支只更新这些列 | 触发条件：房源退房再售/撤销后重新上架（政务网常态）；或上一条误标后的下一次正常抓取——误标造成的错误因此**永久化** | 建议修法：UPDATE 时若 `oldmap["sold_at"]` 非空则一并置 NULL，并写一条 `status_changes(field='availability', old='已售/下架', new='可售')`。实测当前库 455 套、1 套已售、0 套复活，属尚未爆发而非不存在。

## 中等（健壮性 / 可维护性）

- [scripts/webforms_crawl.py:317-321] 代理兜底是死代码：`proxies_from_pool()` 拿到代理列表后，321 行调 `site.fetch_page(params)` 没传 `proxy=`，等于拿完代理继续直连重试 | 触发条件：直连失败 + `--use-proxy` | 建议修法：逐个 `site.fetch_page(params, proxy=p)` 试，或至少删掉死路径。
- [.github/workflows/daily-crawl.yml:100-104] push 重试循环 3 次全失败后步骤仍以退出码 0 结束（循环最后执行的是 `sleep 5`，`&&` 链中的失败被 bash 吞掉，`-e` 也不拦）→ 数据已 commit 但从未 push，CI 显示绿色，当天数据静默丢失 | 触发条件：连续 3 次 push 失败（non-fast-forward 未被 rebase 解决、网络抖动） | 建议修法：成功标志位 `ok=0`，循环内成功置 1，循环后 `[ "$ok" = 1 ] || exit 1`。
- [scripts/serve.py:279-281, 135, 164] `export_static` 在 Actions（UTC）上执行时，`api_stats` 的 `sold_today` 用 `datetime.now()`（UTC 日期）去 LIKE 匹配北京时间落库的 `sold_at`；`project_snapshots`/`api_project` 的 days 窗口同理偏 8 小时 | 触发条件：daily-crawl 的 push 触发（yml:15-19）不限时段，北京时间 00:00-08:00 的代码 push（= UTC 前一天 16:00-24:00）会让「今日成交」显示成前一天的数字 | 建议修法：与 `now_cn()` 统一，全部用 UTC+8 计算「今天」和窗口。
- [scripts/webforms_crawl.py:70] `subprocess.run(..., text=True)` 按运行机 locale 解码 curl 输出：CI 是 UTF-8、本地 Windows 是 cp936。若站点编码与运行机 locale 不符（UTF-8 页面在 Windows 上按 GBK 解），中文变乱码 → `row_regex` 全失配 → 表现为「查询返回空结果（站点抽风）」重试 3 次后失败，极具误导性 | 触发条件：本地 Windows 直跑 + 站点编码 ≠ cp936（或反之） | 建议修法：`text=False` 拿 bytes，按页面 `<meta charset>`（默认 UTF-8）显式解码。
- [web/index.html:498] `loadChanges` 渲染 `${c.old}→${c.new}` 未过 `esc()`。`status_changes.old/new` 来自爬虫抓的站点字段（通用框架支持 `status_field`），异常/恶意 HTML 会原样注入看板（存储型 XSS）。当前 suzhou.json `status_field: null`，old/new 是 `mark_sold` 写死的常量，所以**现在**不可利用 | 触发条件：接入任何带 `status_field` 的站点，或站点字段被污染 | 建议修法：改为 `${esc(c.old)}→${esc(c.new)}`。

## 轻微（风格 / 注释）

- [web/index.html:428 与 433] 拼音表 `'尚'` 重复定义（先 `'尚':'s'` 后 `'尚':'z'`），JS 后者覆盖前者 → 含「尚」的楼盘首字母匹配 `z` 而非 `s`。删 433 行那个键即可。
- [scripts/serve.py:370-374] `POST /api/subscriptions/delete` 对 body 无 `id`（None）也返回 `{ok:true}`，应返回 400；[serve.py:334,338] `days` 传非数字会直接抛 500。
- [scripts/sites/suzhou.json:33-53] `no_alias_needed` 没有任何代码引用（死配置），且与 `name_alias` 重复列出「君华雅筑」「天朗汇庭」，语义自相矛盾——要么删掉，要么注明用途。
- [data/housing.db, status_changes id=1] 残留一条时区修复前的 UTC 时间戳（`changed_at=2026-10-17 02:17:03`，同轮快照是 10:17:03）。代码已修（`mark_sold` 现传 `now_cn()`），但历史行无迁移；前端只显示月-日所以无感，仅数据层面的备注。
- [scripts/serve.py:2 vs 29] 文档字符串写「http.server 单线程足够」，实际用的是 `ThreadingHTTPServer`——注释与实现不符（并发本身无害，每请求独立连接）。

## 测试缺口

- `db_upsert`：已售房源重新出现的「复活」场景零覆盖——正是严重#2（现有 `test_upsert_*` 只测插入/幂等/改价）。
- `mark_sold`：`--max-pages` 截断、`共N页` 正则失配、query 参数变化三种场景零覆盖——正是严重#1（现有测试全部假设 seen 集完整）。
- `serve.api_trend`（alive=0 历史行过滤）与 `api_projects` 的聚合数值（均价/去化率）无任何单测。
- `serve.py` HTTP 层零测试：POST 参数校验分支、`days` 非法值、404 分支全靠 E2E 间接覆盖，而 E2E 又只跑静态模式。
- `webforms_crawl.http()` / `fetch_page()` 的重试边界、VIEWSTATE 链式更新、空结果误判——爬虫健壮性核心完全无测试（可以 mock `subprocess.run` 做纯逻辑测试，零依赖可行）。
- E2E 只断言详情页「能打开/能返回」，未断言数据内容（sparkline 点数、alive_list 行数、均价数值），详情页数据回归测不出。
- [tests/test_frontend_lint.py:117] `mergeSubs` 检查只要求函数体出现 `"MODE"` 和 `"return L"` 两个字符串——把逻辑写反（如 `MODE === 'api'` 才 return）也能通过，属弱断言/假通过风险。
- [tests/test_unit.py:213-218] `test_alias_file_only_maps_real_projects` 注释说「key 必须是真实备案名」，实际只验正则格式，未对照 DB/真实楼盘名校验。

## 已知历史 bug 复核（确认已修，不重复报）

- 控件名前缀：`qualify()` + 4 个测试 ✓（webforms_crawl.py:116-127）
- 空串参数过滤：webforms_crawl.py:146-147 ✓
- VIEWSTATE 32KB：body 走临时文件 `--data-binary @file` ✓（webforms_crawl.py:62-68）
- ALTER 默认 0 假暴跌：`api_trend`/`api_stats` 均过滤 `alive in (None, 0)` ✓（注意副作用：真的清盘为 0 的快照也会被剔除，注释已声明是有意取舍）
- 时区统一：`now_cn()` 全链路 + 快照/变化同轮同时间戳 ✓（仅残留上文那条历史行）
- 删除按钮 id 引号：`data-del` 事件委托 ✓
- 星标误跳详情：捕获阶段 + 卡片改 JS 绑定 ✓（E2E test_61 实测）
- 项目曲线串全站：`project_snapshots` 按项目反推 ✓（E2E + 单测双覆盖）
- `mergeSubs` 静态注入：静态模式直接 `return L` 不落 localStorage ✓（E2E test_52 间接覆盖）
