# 苏州楼市在售看板 🏙️

自动抓取苏州市区商品房销售网上管理系统（住建局）的**可售房源**数据，
每日 9:00（北京时间）自动更新，纯静态页面，**零服务器、零费用、不依赖 AI**。

## 📱 手机看板

部署后访问：`https://<你的用户名>.github.io/<仓库名>/`

功能：
- **项目列表**：在售套数 / 均价 / 面积 / 去化率，可按"在售最多/去化最快/均价最高/成交最多"排序
- **项目详情**：近 7 天 / 30 天成交套数与金额、去化速度（套/天）、预计清盘天数、存量趋势曲线、在售房源清单、最近成交记录
- **订阅**：收藏关注楼盘（静态托管下订阅写入 `web/subscriptions.json`）

## 🔧 本地运行

```bash
# 1. 抓一次数据
python scripts/webforms_crawl.py --site scripts/sites/suzhou.json --db data/housing.db \
  --param ddl_qy=RD002 --param txt_Area1=140 --param txt_Area2=180 \
  --param ddl_houseclass=1 --param rb_HF_CODE=-1

# 2A. 本地看板（带 API，订阅可写）
python scripts/serve.py --db data/housing.db --port 8899
#     打开 http://127.0.0.1:8899

# 2B. 或导出静态 JSON（部署用）
python scripts/export_static.py --db data/housing.db --out web/data.json
```

## 🎯 查询参数（苏州站）

| 参数 | 说明 |
|---|---|
| `ddl_qy` | `RD002`工业园区 / `RD003`吴中 / `RD004`相城 / `RD005`高新 / `RD001`姑苏 / `RD008`吴江 |
| `ddl_houseclass` | `1`住宅 / `2`非住宅 |
| `txt_Area1` / `txt_Area2` | 建筑面积区间（㎡）|
| `txt_Price1` / `txt_Price2` | 总价区间（元）|

> 默认已配置为 **工业园区 · 住宅 · 140~180㎡**，改工作流里的 `--param` 即可换条件。

## 📊 数据口径

- 站点只展示**未售**房源。某套房源从列表消失 = 已成交/下架，本工具据此记为"已售"，
  这是"去化速度"的数据来源。
- 每次抓取写一条快照（在售数、新增数、售出数），趋势曲线即存量变化。
- 有失败页时跳过"消失判定"，避免把"没爬到"误判为"已售"。

## ⚠️ 已知坑

1. **苏州站没有屏蔽海外 IP**（实测 GitHub Actions 的 IP 可正常抓取；
   注意隔壁厦门站是屏蔽的，不可照搬）。
2. WebForms 翻页必须用**上一页返回的 VIEWSTATE**，用首次 GET 的会静默失败。
3. 查询控件名必须带 `ctl00$MainContent$` 前缀，漏了会静默返回全量数据。
4. 传空串参数会让筛选失效，空值必须省略。

## 免责声明

数据来自政府部门公开页面，仅供个人研究参考，不构成投资建议。
