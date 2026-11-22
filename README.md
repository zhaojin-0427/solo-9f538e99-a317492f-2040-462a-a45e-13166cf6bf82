# 门店周报 · 同店对比

Flask + SQLite 的单店日报导入与「两个周一」同店对比工具。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 按门店 + 日期导入日报，填写修订号与已报齐的营业小时 | 导入页 / `POST /import`、`POST /api/import` |
| 小票 CSV 两列：`小时,金额分`（表头可省） | `weekly_compare/receipts.py` |
| 只接受**更高修订号**整日替换 | `services.import_daily_report` + `report_versions` 唯一约束 |
| 更正可缩减已报齐的营业小时（整日替换，不做增量合并） | 新修订写入新的 `report_hours` 集合 |
| 对比只接受两个周一，按门店 + 星期 + 小时配对 | `comparison.validate_comparison_dates` |
| 只统计两周**都报齐**的营业小时 | `comparison` 的 `matched` CTE（小时 inner join） |
| 报齐但无小票的小时按 0 计 | 导入时为每个报齐小时写一行 `report_hours`（无小票则 0） |
| 列出各店与全店合计的可比小时数、两周销售额、差额 | 对比页 / `GET /api/compare` |
| 导入期间仍可查旧结果 | WAL + 导入只在最后一步切换 `current_reports` 指针 |
| 覆盖时营业小时与销售汇总一同生效 | 同一事务内写 `report_hours` / `report_versions` / 指针 |
| 失败保留原日报与结果 | 输入全部校验在事务前；事务内任何异常整体回滚 |
| 对比与更正都不重扫旧小票 | 对比只读 `report_hours`；更正写新版本 |
| 一次查询中的各店数字与全店合计取自同一提交状态 | 单条 SQL（`UNION ALL` 出合计）+ 单个读事务 |

## 交款单对账

旧交款单丢了整点截账时间，只剩按顺序排列的批次号和净额。`/reconcile` 页面：

| 规则 | 实现位置 |
| --- | --- |
| 选门店、日期、交款单覆盖的起止小时（两端都计入） | `/reconcile` 表单 → `reconciliation.parse_request` |
| 按顺序录入批次号和净额（整数分，可为负） | 每行 `批次号,净额分`，行序即批次顺序 |
| 所选小时依次划给各批：每小时只归一批，每批占一段连续小时 | `reconciliation.analyse` 的动态规划 |
| 段内销售额之和必须等于批次净额 | 前缀和 + `reachable` 表 |
| 有未报齐的小时：列出缺失小时并停止对账 | `Analysis.missing_hours` / `stopped_for_missing` |
| 已报齐的零销售小时照常参与 | `report_hours` 里的 0 与其它小时一视同仁 |
| 逐批列出所有完整方案中可能的最后小时 | `_build_candidates`（正反两向可达性） |
| 可填已查实的截止小时（第几批、该批最后小时）后再算，无解时提示 | `pinned_batch` / `pinned_hour`，`plan_count == 0` 显示「无解」 |
| 只有切分唯一且日报仍是页面所示修订号时才可保存/覆盖 | `save_reconciliation` 在 `BEGIN IMMEDIATE` 内复核修订号 |
| 页面列出已存结果的日报修订号和各批覆盖小时 | `/reconcile` 的「已存对账结果」、`/reconciliations` |
| 日报更正后原结果保留并标为待复核 | `StoredReconciliation.needs_review`（比较 `report_version_id`） |
| 保存时日报已换版：提示重算，原记录照旧 | `ReportChanged`（409），不写入任何行 |

每个门店每天最多一条对账结果（`reconciliations` 唯一约束）；保存会整条覆盖旧结果。
日报一旦被更高修订号替换，已存结果自动显示为「待复核」，但仍按保存时的报表修订号保留。

对账不依赖「周一」限制，任意营业日均可使用；日期与门店必须在日报中已存在。

## 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py          # http://127.0.0.1:5000
```

数据库默认 `./data/reports.db`，可用 `DATABASE_PATH` 覆盖。首次启动自动建表
（WAL 模式）。

## Docker Compose

```bash
docker compose up --build -d
# 访问 http://127.0.0.1:8000
docker compose ps         # web 为 healthy
docker compose logs -f
```

- 端口：`APP_PORT`（默认 8000）。
- 数据：命名卷 `report-data` 挂在 `/app/data`；`docker compose down` 保留数据，
  `docker compose down -v` 才删除。
- 生产部署请设置 `SECRET_KEY`：
  `SECRET_KEY=$(openssl rand -hex 32) docker compose up -d`。
- 容器以 uid 10001 运行；如需改用宿主机目录 bind mount，请确保该目录对该 uid 可写
  （SQLite 需要目录写权限，不只是文件写权限）。
- 健康检查/`HEALTHCHECK` 走 `GET /api/health`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 当前生效日报列表（可筛选日期/门店编码） |
| GET | `/import` | 导入表单（带现有修订号则预填 +1） |
| POST | `/import` | 表单导入（文件或粘贴） |
| GET | `/reports/<store_code>/<date>` | 单份日报的各小时金额 |
| GET | `/reconcile` | 交款单对账表单（可带 `store_code` / `business_date` 查看已存结果） |
| POST | `/reconcile` | 计算（`action=calculate`）或保存（`action=save` + `expected_revision`） |
| GET | `/reconciliations` | 已存对账结果列表（修订号、状态、各批覆盖小时） |
| GET | `/compare?date_a=&date_b=` | 同店对比页 |
| GET | `/api/compare?date_a=&date_b=` | 对比 JSON（含 `total`） |
| GET | `/api/reconciliations` | 已存对账结果 JSON |
| GET | `/api/reports?business_date=&store_code=` | 当前日报 JSON |
| POST | `/api/import` | 导入 JSON 响应 |
| GET | `/api/csrf` | 取 CSRF token（同时下发会话 cookie） |
| GET | `/api/health` | 存活/数据库检查 |

脚本调用示例：

```bash
TOKEN=$(curl -s -c jar -b jar http://127.0.0.1:8000/api/csrf | python3 -c 'import json,sys;print(json.load(sys.stdin)["csrf_token"])')
curl -s -c jar -b jar -X POST http://127.0.0.1:8000/api/import \
  -H "X-CSRF-Token: $TOKEN" \
  -F store_code=S001 -F store_name=甲店 -F business_date=2026-11-02 \
  -F revision=1 -F complete_hours=9-11 -F receipts=@receipts.csv
```

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

覆盖：CSV/小时解析、修订替换与拒绝、失败回滚（含事务中途数据库报错）、
可比小时配对、报齐无小票计零、各店合计等于全店合计、单条 SQL 快照、
WAL 下导入未提交时读旧值、HTTP 页面/API/CSRF/413；交款单对账的批次解析、
唯一/多解/无解、缺失小时停止、零销售小时参与、已查实截止小时、负数净额、
保存前置条件、日报更正后的待复核与换版拒存（HTTP 与 JSON）。

## 已知限制

- 门店名称只能通过导入时更新；没有独立门店管理页。
- CSRF token 存在 Flask 签名 cookie 会话里；多实例部署需共享 `SECRET_KEY`。
- 对比一次只处理两个日期；跨更多周的趋势需多次查询。
- 历史修订版本保留在库中供追溯，但页面上只能看当前版本。
