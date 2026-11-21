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
| GET | `/compare?date_a=&date_b=` | 同店对比页 |
| GET | `/api/compare?date_a=&date_b=` | 对比 JSON（含 `total`） |
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
WAL 下导入未提交时读旧值、HTTP 页面/API/CSRF/413。

## 已知限制

- 门店名称只能通过导入时更新；没有独立门店管理页。
- CSRF token 存在 Flask 签名 cookie 会话里；多实例部署需共享 `SECRET_KEY`。
- 对比一次只处理两个日期；跨更多周的趋势需多次查询。
- 历史修订版本保留在库中供追溯，但页面上只能看当前版本。
