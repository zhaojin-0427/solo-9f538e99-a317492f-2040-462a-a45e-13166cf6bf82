"""Same-store, same-weekday, same-hour comparison.

Business rules implemented here:

* **Scope** — every store that has a current report on *either* Monday of the
  comparison.  A store missing one side still appears (with ``—`` in the
  missing column) so it cannot silently vanish from the report.
* **Pairing** — a store's two reports are paired hour by hour.  An hour counts
  only when it is 报齐 in **both** reports.
* **Zero receipts** — hours are taken from ``report_hours``, which import writes
  for every 报齐 hour (missing receipt rows contribute 0).  No receipt rescan.
* **Consistency** — one SQL statement returns both the per-store rows and the
  all-store total, executed inside one read snapshot, so the store figures and
  the total are guaranteed to be from the same commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import db
from .errors import ImportRejected
from .services import parse_date, weekday_name

# A single statement -> a single SQLite snapshot for rows *and* total.
# Only report_hours is read: receipt_lines are never rescanned.
_COMPARISON_SQL = """
WITH a AS (
    SELECT cr.store_id, rv.id AS version_id, rv.revision
    FROM current_reports cr
    JOIN report_versions rv ON rv.id = cr.report_version_id
    WHERE cr.business_date = :date_a
),
b AS (
    SELECT cr.store_id, rv.id AS version_id, rv.revision
    FROM current_reports cr
    JOIN report_versions rv ON rv.id = cr.report_version_id
    WHERE cr.business_date = :date_b
),
scope AS (
    SELECT store_id FROM a
    UNION
    SELECT store_id FROM b
),
matched AS (
    SELECT a.store_id,
           ha.hour,
           ha.sales_cents AS sales_a,
           hb.sales_cents AS sales_b
    FROM a
    JOIN b ON b.store_id = a.store_id
    JOIN report_hours ha ON ha.report_version_id = a.version_id
    JOIN report_hours hb
      ON hb.report_version_id = b.version_id
     AND hb.hour = ha.hour
),
per_store_matched AS (
    SELECT store_id,
           COUNT(*) AS comparable_hours,
           COALESCE(SUM(sales_a), 0) AS sales_a,
           COALESCE(SUM(sales_b), 0) AS sales_b
    FROM matched
    GROUP BY store_id
),
per_store AS (
    SELECT s.id AS store_id,
           s.code AS store_code,
           s.name AS store_name,
           a.revision AS revision_a,
           b.revision AS revision_b,
           COALESCE(pm.comparable_hours, 0) AS comparable_hours,
           COALESCE(pm.sales_a, 0) AS sales_a,
           COALESCE(pm.sales_b, 0) AS sales_b
    FROM scope
    JOIN stores s ON s.id = scope.store_id
    LEFT JOIN a ON a.store_id = scope.store_id
    LEFT JOIN b ON b.store_id = scope.store_id
    LEFT JOIN per_store_matched pm ON pm.store_id = scope.store_id
)
SELECT 0 AS is_total, store_id, store_code, store_name,
       revision_a, revision_b, comparable_hours, sales_a, sales_b
FROM per_store
UNION ALL
SELECT 1 AS is_total, NULL, NULL, '全店合计',
       NULL, NULL,
       COALESCE(SUM(comparable_hours), 0),
       COALESCE(SUM(sales_a), 0),
       COALESCE(SUM(sales_b), 0)
FROM per_store
ORDER BY is_total ASC, store_code ASC
"""


@dataclass(frozen=True)
class ComparisonRow:
    store_id: int
    store_code: str
    store_name: str
    revision_a: int | None
    revision_b: int | None
    comparable_hours: int
    sales_a_cents: int
    sales_b_cents: int

    @property
    def difference_cents(self) -> int:
        return self.sales_b_cents - self.sales_a_cents

    @property
    def has_both_reports(self) -> bool:
        return self.revision_a is not None and self.revision_b is not None


@dataclass(frozen=True)
class ComparisonTotal:
    comparable_hours: int
    sales_a_cents: int
    sales_b_cents: int

    @property
    def difference_cents(self) -> int:
        return self.sales_b_cents - self.sales_a_cents


@dataclass(frozen=True)
class Comparison:
    date_a: date
    date_b: date
    rows: tuple[ComparisonRow, ...]
    total: ComparisonTotal

    @property
    def weekday(self) -> str:
        return weekday_name(self.date_a)

    @property
    def has_rows(self) -> bool:
        return bool(self.rows)

    @property
    def total_has_comparable_hours(self) -> bool:
        return self.total.comparable_hours > 0


def validate_comparison_dates(date_a: str, date_b: str) -> tuple[date, date]:
    """Both dates must be Mondays and must differ; they are returned sorted."""

    day_a = parse_date(date_a, "第一个周一")
    day_b = parse_date(date_b, "第二个周一")
    if day_a.weekday() != 0 or day_b.weekday() != 0:
        raise ImportRejected("同店对比只接受两个周一（所选日期必须都是周一）。")
    if day_a == day_b:
        raise ImportRejected("请选择两个不同的周一。")
    if day_a > day_b:
        day_a, day_b = day_b, day_a
    return day_a, day_b


def compare(database: str, date_a: str, date_b: str) -> Comparison:
    day_a, day_b = validate_comparison_dates(date_a, date_b)

    # One read transaction pins one committed snapshot for the whole statement.
    with db.read_snapshot(database) as conn:
        result_rows = conn.execute(
            _COMPARISON_SQL,
            {"date_a": day_a.isoformat(), "date_b": day_b.isoformat()},
        ).fetchall()

    rows: list[ComparisonRow] = []
    total = ComparisonTotal(0, 0, 0)
    for row in result_rows:
        if row["is_total"]:
            total = ComparisonTotal(
                comparable_hours=row["comparable_hours"],
                sales_a_cents=row["sales_a"],
                sales_b_cents=row["sales_b"],
            )
            continue
        rows.append(
            ComparisonRow(
                store_id=row["store_id"],
                store_code=row["store_code"],
                store_name=row["store_name"],
                revision_a=row["revision_a"],
                revision_b=row["revision_b"],
                comparable_hours=row["comparable_hours"],
                sales_a_cents=row["sales_a"],
                sales_b_cents=row["sales_b"],
            )
        )

    return Comparison(
        date_a=day_a,
        date_b=day_b,
        rows=tuple(rows),
        total=total,
    )
