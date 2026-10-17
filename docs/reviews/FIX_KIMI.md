# Kimi K3 修复报告

## 已修复
- 严重#1 消失判定前提可被打破：`scripts/webforms_crawl.py:336` 新增 `disappearance_guard()`，`--max-pages` 截断、失败页、`共 N 页` 失配、抓到行数≠站点总数、0 行结果、查询条件与上轮不一致一律跳过 `mark_sold`；`scripts/webforms_crawl.py:45/273/296/311/476` 增加 `query_key` + `listings.query` 列（含 `init_db()` 补列）并把消失判定限定到同一 query，避免换 `--param` 误杀条件外房源。补测：`tests/test_unit.py:364`（query 隔离）、`tests/test_unit.py:550`（6 个安全门场景）。
- 严重#2 已售房源复活不清 `sold_at`：`scripts/webforms_crawl.py:245-255` 在 UPDATE 前显式置 `sold_at=NULL` 并写 `availability: 已售/下架 -> 可售`。补测：`tests/test_unit.py:350`。
- 中等#1 代理兜底死代码：`scripts/webforms_crawl.py:369` 新增 `fetch_first_page()`，直连失败后逐个 `site.fetch_page(..., proxy=p)`。补测：`tests/test_unit.py:535`。
- 中等#2 push 重试假绿：`.github/workflows/daily-crawl.yml:102-111` 改显式 `ok` 标志，3 次失败 `exit 1`（未动 cron）。补测：`tests/test_workflow_guard.py:154`。
- 中等#3 UTC/北京时区偏差：`scripts/serve.py:35` 新增 `now_cn()`，`project_snapshots/api_project/api_trend/api_stats` 与订阅写入全部改走 UTC+8；`scripts/export_static.py:70` 的 `generated_at` 同步。补测：`tests/test_unit.py:657`。
- 中等#4 curl 输出按 locale 解码：`scripts/webforms_crawl.py:54` 新增 `decode_page()`，`http()` 改收 bytes 并按 `<meta charset>`（默认 UTF-8）显式解码。补测：`tests/test_unit.py:449`（TestDecodePage）、`tests/test_unit.py:465`。
- 中等#5 成交动态 XSS：`web/index.html:498` 改为 `${esc(c.old)}→${esc(c.new)}`。补测：`tests/test_frontend_lint.py:128`。
- 轻微#1 拼音表重复 key：删 `web/index.html` 里后写的 `'尚':'z'`，并清掉另一处重复 `'天':'t'`；`tests/test_frontend_lint.py:139-145` 新增重复 key 检查。
- 轻微#2 HTTP 参数校验：`scripts/serve.py:45` 新增 `parse_days()`，`/api/project`、`/api/trend` 非法 days 返回 400；`scripts/serve.py:399` 删除订阅缺 `id` 返回 400。补测：`tests/test_unit.py:668`（真实起 HTTP 服务）。
- 轻微#3 死配置：删 `scripts/sites/suzhou.json` 的 `no_alias_needed`（无代码引用且与 `name_alias` 冲突）。补测：`tests/test_unit.py:227` 别名 key 对照真实库项目名。
- 轻微#5 注释不符：`scripts/serve.py:18` 文档字符串改为 ThreadingHTTPServer 并发模型。
- 测试缺口补强：`api_projects` 聚合与 `api_trend` alive=0/NULL 过滤（`tests/test_unit.py:629/639`）；爬虫重试/空结果/VIEWSTATE 链式（`tests/test_unit.py:474-510`）；`mergeSubs` 静态守卫由弱断言改精确匹配（`tests/test_frontend_lint.py:114-123`）；详情页数据内容 E2E（`tests/test_e2e.py:326`）。

## 已驳回（误报）
- 无。本轮未发现纯误报；唯一未动的是下面这条历史数据残留。

## 测试结果
- 单元测试：`python -m unittest discover -s tests -p 'test_unit.py'` → `Ran 66 tests in 8.307s` / `OK`（输出仅有 ResourceWarning 与代理兜底用例的告警打印，不影响结果）。
- 工作流守卫：`python tests/test_workflow_guard.py` → `Ran 12 tests in 0.752s` / `OK`。
- 前端静态检查：`python tests/test_frontend_lint.py` → 20 项全 `✓`，末尾 `✓ 前端静态检查全部通过`。
- 浏览器 E2E：`CHROME_PATH=... chromium-1208 ... python tests/test_e2e.py` → `Ran 27 tests in 10.497s` / `OK`。
- 全量门禁：`CHROME_PATH='C:\Users\Kevinzz224\AppData\Local\hermes\tools\chromium-1208\chrome-win64\chrome.exe' bash tests/run_all.sh` → 末尾 `✓ 全部测试通过，可以部署`。
- 额外验证：`python scripts/export_static.py --db data/housing.db --out $TMPDIR/data_check.json` → `[export] ... 项目 30 个 | 详情 30 份 | 快照点 7 | 变化 1 | 订阅 1 / 在售 454 套 / 已售 1 套`。

## 遗留
- `data/housing.db` 里 `status_changes id=1` 那条时区修复前的 UTC 历史行未做迁移：代码链路已统一 `now_cn()`，改历史数据属于数据订正而非代码修复，且会让 `data/housing.db` 出现与本次代码修复无关的 diff；建议由主控在单独的数据订正任务里处理。前端只显示月-日，当前无用户可见影响。
