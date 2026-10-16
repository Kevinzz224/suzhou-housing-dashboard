---
name: gov-housing-crawler
description: "Crawl 住建局 housing sites: WebForms + proxy pool + SQLite."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [crawler, spider, housing, real-estate, proxy, 住建局, 爬虫]
    category: research
---

# gov-housing-crawler

爬政府房产网站（住建局/房管局）的通用套路：这些站几乎都是**老 ASP.NET
WebForms**（__VIEWSTATE / __EVENTVALIDATION / GridView 分页），curl/requests 直连
大概率被限流或连不上（政务外网 IPv6 不全、屏蔽境外 IP），所以核心是三件事：
**代理池轮换 + WebForms 会话续期 + 增量快照**。

## When to Use
- 用户要爬某市住建局"可售房源/商品房网签/每日成交"，要定期增量、看去化速度
- 要订阅特定楼盘的销售状态变化（可售→已售），变化时推送
- 同类需求（任何 WebForms 政府站）也按本套路改选择器即可

**站点配置先行**：目标站的 URL、控件名、分页参数写进 `scripts/sites/<城市>.json`，
不要改主程序。字段含义见 `scripts/sites/suzhou.json` 注释。

## 工作流

### 1. 先摸清站点结构（不要一上来就写死）
用 curl 抓首页，确认三件套：
- `__VIEWSTATE` / `__VIEWSTATEGENERATOR` / `__EVENTVALIDATION` 三个 hidden input
- 查询控件的 `name`（`ctl00$MainContent$xxx`，注意 POST 时 `$` 要转义成 `\$`）
- 分页器：通常是 `__doPostBack('ctl00$MainContent$OraclePager1$ctl12$PageList','')`，
  POST 时带 `__EVENTTARGET` + 下拉框值（页码索引，从 0 开始）

政务站响应慢（5~30s）很正常，curl `--max-time` 给到 60~90s。

### 2. 免费代理池（按需）
`scripts/proxy_pool.py`：
- 源：proxyscrape API、TheSpeedX/PROXY-List、openproxylist.xyz、geonode
- 拉取后**先并发验证**（只测到目标域名的连通性，不要只测 8.8.8.8）
- 失败的丢进冷却名单，冷却期内不重复试
- 关键：免费代理 90% 不通，验证要并发（20~50 线程）+ 短超时（3~5s）
- **国内政务站优先直连**：代理兜底路径已接好（`--use-proxy`），但实测家宽直连
  更稳更快，代理只是境外 VPS 跑时的必需品

### 3. 抓取入库（SQLite，增量）
`scripts/webforms_crawl.py`：
- 每次跑新建 session（拿最新 VIEWSTATE，**不要跨请求复用旧 VIEWSTATE**）
- 每条记录主键 = 站点天然 ID（房源编号/项目 ID），`INSERT ... ON CONFLICT DO NOTHING`
- 同一主键第二次抓到 -> UPDATE 状态字段 + 记 `last_seen_at`，
  状态变化记一条 `status_changes`（可售→签约中→已售），这就是"去化速度"的原料
- 每次抓取记一次 `snapshots`（日期、区域、条数），用于算周/月去化

### 4. 订阅与推送
- 订阅表 `subscriptions`（项目名关键词 / 区域 / 面积区间）
- 状态变化走 `notify.py`：默认企业微信 webhook（Markdown），可换 Server酱/Telegram
- 推送文案模板见 `scripts/notify.py`，直接可用

### 5. 定时增量
cron / GitHub Actions 每天 1~2 次。**政务站半夜维护，选 9:00 和 20:00（北京时间）**。
GitHub Actions 跑要确认目标站不屏蔽 GitHub IP（厦门站就屏蔽，见 references 案例）。

## 已知案例
- `references/suzhou-szfcweb.md` — 苏州住建局 spf.zfcjj.suzhou.gov.cn:8089 全站可用端点
  （可售房源、即时成交、项目列表）+ 每个的 POST 参数与分页方式

## 坑
- VIEWSTATE 里有 base64，curl `--data-urlencode` 必须带 `=` 和 `/`，别用 `-d` 裸拼
- 查询条件是"AND"关系，面积/总价是闭区间；空条件要传空串而不是省略字段
- 站点偶发连不上（Connection timed out）不是封 IP，是它自己抽风，重试即可
- 免费代理对中国政务站基本无用（路由绕远更慢），**优先直连，代理只作为直连失败时的兜底**
- 导出按钮（`Button1` -> `dcbg()`）是 JS 导 Excel，能白嫖就不要逐页解析 HTML
