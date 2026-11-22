"""Payment-slip reconciliation for one store-day.

Background
----------
The old paper payment slips lost their on-the-hour cut-off times.  What is left
is an *ordered* list of batch numbers and their net amounts.  Given a store, a
business date and the inclusive hour range the slips cover, the task is to cut
the selected hours into one contiguous segment per batch, in order, so that the
sum of that segment's sales equals the batch net amount.

Rules encoded here
------------------
* Every selected hour must already be reported as complete.  If any hour is
  missing, reconciliation stops and the missing hours are listed.
* A complete hour with no sales (0 cents) is a normal hour and participates.
* Each batch covers at least one hour; each hour belongs to exactly one batch.
* For each batch the *possible last hours* across all complete plans are
  reported.  A plan is only saveable when it is the unique complete plan.
* A known cut-off hour for one batch (``pinned``) can be supplied to narrow the
  calculation down; if nothing fits, the answer is "无解".

All amounts are integer cents and may be negative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import db, services
from .errors import NotFound, ReconciliationRejected, ReportChanged, ReportNotFound
from .receipts import MAX_AMOUNT_CENTS, format_hours

MAX_BATCHES = 100
_BATCH_NO_RE = re.compile(r"^\S{1,40}$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_HOUR_RE = re.compile(r"^\d{1,2}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchInput:
    position: int  # 1-based, matches the order the finance user entered
    batch_no: str
    amount_cents: int


@dataclass(frozen=True)
class ReconRequest:
    store_code: str
    business_date: str
    start_hour: int
    end_hour: int
    batches: tuple[BatchInput, ...]
    pinned_batch: int | None = None
    pinned_hour: int | None = None
    batches_text: str = ""

    @property
    def selected_hours(self) -> tuple[int, ...]:
        return tuple(range(self.start_hour, self.end_hour + 1))

    @property
    def day(self) -> date:
        return date.fromisoformat(self.business_date)

    @property
    def weekday(self) -> str:
        return services.weekday_name(self.day)


def _parse_hour(raw: str, label: str) -> int:
    text = str(raw or "").strip()
    if not _HOUR_RE.fullmatch(text):
        raise ReconciliationRejected(f"{label}必须是 0–23 的整点。")
    hour = int(text)
    if not 0 <= hour <= 23:
        raise ReconciliationRejected(f"{label}超出 0–23。")
    return hour


def parse_batches_text(text: str) -> tuple[BatchInput, ...]:
    """Parse one ``批次号,净额(分)`` pair per line, preserving line order."""

    source = str(text or "")
    batches: list[BatchInput] = []
    for line_no, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        cells = [cell.strip() for cell in re.split(r"[,，\t]+", line)]
        if len(cells) != 2:
            raise ReconciliationRejected(
                f"批次第 {line_no} 行应有两列（批次号,净额分），实际有 {len(cells)} 列。"
            )
        batch_no, amount_text = cells
        if not _BATCH_NO_RE.fullmatch(batch_no):
            raise ReconciliationRejected(
                f"批次第 {line_no} 行的批次号不能为空、不能含空白或分隔符，最长 40 个字符。"
            )
        if not _INT_RE.fullmatch(amount_text):
            raise ReconciliationRejected(
                f"批次第 {line_no} 行的净额必须是整数分（可为负）：{amount_text!r}。"
            )
        amount = int(amount_text)
        if abs(amount) > MAX_AMOUNT_CENTS:
            raise ReconciliationRejected(f"批次第 {line_no} 行的净额超出允许范围。")
        batches.append(
            BatchInput(position=len(batches) + 1, batch_no=batch_no, amount_cents=amount)
        )
        if len(batches) > MAX_BATCHES:
            raise ReconciliationRejected(f"批次数量超过 {MAX_BATCHES} 个上限。")

    if not batches:
        raise ReconciliationRejected("请至少录入一个批次的批次号和净额。")
    return tuple(batches)


def parse_request(
    *,
    store_code: str,
    business_date: str,
    start_hour: str,
    end_hour: str,
    batches_text: str,
    pinned_batch: str = "",
    pinned_hour: str = "",
) -> ReconRequest:
    code = services.validate_store_code(store_code)
    day = services.parse_date(business_date)
    start = _parse_hour(start_hour, "起始小时")
    end = _parse_hour(end_hour, "结束小时")
    if start > end:
        raise ReconciliationRejected("起始小时不能晚于结束小时。")
    batches = parse_batches_text(batches_text)

    pin_batch: int | None = None
    pin_value: int | None = None
    pin_batch_text = str(pinned_batch or "").strip()
    pin_hour_text = str(pinned_hour or "").strip()
    if bool(pin_batch_text) != bool(pin_hour_text):
        raise ReconciliationRejected("请同时填写已查实截止的批次序号与小时，或两者都留空。")
    if pin_batch_text:
        if not pin_batch_text.isdigit():
            raise ReconciliationRejected("已查实截止的批次序号必须是正整数。")
        pin_batch = int(pin_batch_text)
        if not 1 <= pin_batch <= len(batches):
            raise ReconciliationRejected(
                f"已查实截止的批次序号应在 1–{len(batches)} 之间。"
            )
        pin_value = _parse_hour(pin_hour_text, "已查实截止小时")
        if not start <= pin_value <= end:
            raise ReconciliationRejected(
                f"已查实截止小时 {pin_value} 不在所选小时 {start}–{end} 内。"
            )

    return ReconRequest(
        store_code=code,
        business_date=day.isoformat(),
        start_hour=start,
        end_hour=end,
        batches=batches,
        pinned_batch=pin_batch,
        pinned_hour=pin_value,
        batches_text=batches_text.strip(),
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchCandidates:
    position: int
    batch_no: str
    amount_cents: int
    end_hours: tuple[int, ...]

    @property
    def end_hours_text(self) -> str:
        return format_hours(self.end_hours)


@dataclass(frozen=True)
class PlanSegment:
    position: int
    batch_no: str
    amount_cents: int
    start_hour: int
    end_hour: int
    sales_cents: int

    @property
    def hour_count(self) -> int:
        return self.end_hour - self.start_hour + 1


@dataclass(frozen=True)
class Analysis:
    selected_hours: tuple[int, ...]
    missing_hours: tuple[int, ...]
    plan_count: int  # capped at 2; 2 means "more than one"
    candidates: tuple[BatchCandidates, ...]
    plan: tuple[PlanSegment, ...] | None
    pinned_batch: int | None = None
    pinned_hour: int | None = None

    @property
    def stopped_for_missing(self) -> bool:
        return bool(self.missing_hours)

    @property
    def has_solution(self) -> bool:
        return self.plan_count > 0

    @property
    def unique(self) -> bool:
        return self.plan_count == 1 and self.plan is not None

    @property
    def can_save(self) -> bool:
        return self.unique and not self.stopped_for_missing


def _build_candidates(
    start_hour: int,
    amounts: list[int],
    batches: tuple[BatchInput, ...],
    prefix: list[int],
    reachable: list[list[bool]],
    suffix_ok: list[list[bool]],
    hour_count: int,
    pinned_batch: int | None,
    pinned_index: int | None,
) -> tuple[BatchCandidates, ...]:
    result: list[BatchCandidates] = []
    for index, batch in enumerate(batches):
        amount = amounts[index]
        ends: list[int] = []
        for j in range(1, hour_count + 1):
            if pinned_batch is not None and (index + 1) == pinned_batch and j != pinned_index:
                continue
            starts_ok = any(
                reachable[index][k] and prefix[j] - prefix[k] == amount
                for k in range(j)
            )
            if starts_ok and suffix_ok[index + 1][j]:
                ends.append(start_hour + j - 1)
        result.append(
            BatchCandidates(
                position=batch.position,
                batch_no=batch.batch_no,
                amount_cents=batch.amount_cents,
                end_hours=tuple(ends),
            )
        )
    return tuple(result)


def _unique_plan(
    *,
    start_hour: int,
    amounts: list[int],
    batches: tuple[BatchInput, ...],
    prefix: list[int],
    reachable: list[list[bool]],
    hour_count: int,
    pinned_batch: int | None,
    pinned_index: int | None,
) -> tuple[PlanSegment, ...] | None:
    boundaries = [hour_count]
    j = hour_count
    for index in range(len(amounts) - 1, -1, -1):
        # A pinned batch must end on the supplied cut-off hour.  ``reachable``
        # already excludes other endings for the prefix, but the backtracking
        # itself has to reject them too.
        if pinned_batch is not None and (index + 1) == pinned_batch and j != pinned_index:
            return None
        amount = amounts[index]
        boundary = next(
            (
                k
                for k in range(j)
                if reachable[index][k] and prefix[j] - prefix[k] == amount
            ),
            None,
        )
        if boundary is None:  # pragma: no cover - count==1 guarantees a path
            return None
        boundaries.append(boundary)
        j = boundary
    boundaries.reverse()  # b_0 .. b_B

    segments: list[PlanSegment] = []
    for index, batch in enumerate(batches):
        begin, finish = boundaries[index], boundaries[index + 1]
        segments.append(
            PlanSegment(
                position=batch.position,
                batch_no=batch.batch_no,
                amount_cents=batch.amount_cents,
                start_hour=start_hour + begin,
                end_hour=start_hour + finish - 1,
                sales_cents=prefix[finish] - prefix[begin],
            )
        )
    return tuple(segments)


def analyse(
    *,
    start_hour: int,
    end_hour: int,
    hours_sales: dict[int, int],
    complete_hours: frozenset[int],
    batches: tuple[BatchInput, ...],
    pinned_batch: int | None = None,
    pinned_hour: int | None = None,
) -> Analysis:
    """Cut ``start_hour..end_hour`` into one contiguous segment per batch.

    ``hours_sales`` only contains complete hours; an hour missing from it is
    "未报齐" and stops the reconciliation before any DP runs.
    """

    selected = tuple(range(start_hour, end_hour + 1))
    missing = tuple(h for h in selected if h not in complete_hours or h not in hours_sales)
    batches = tuple(batches)
    if missing:
        return Analysis(
            selected_hours=selected,
            missing_hours=missing,
            plan_count=0,
            candidates=tuple(
                BatchCandidates(b.position, b.batch_no, b.amount_cents, ())
                for b in batches
            ),
            plan=None,
            pinned_batch=pinned_batch,
            pinned_hour=pinned_hour,
        )

    amounts = [batch.amount_cents for batch in batches]
    hour_count = len(selected)
    prefix = [0]
    for hour in selected:
        prefix.append(prefix[-1] + hours_sales[hour])

    batch_count = len(amounts)
    # reachable[i][j]: the first i batches exactly cover the first j hours.
    reachable = [[False] * (hour_count + 1) for _ in range(batch_count + 1)]
    counts = [[0] * (hour_count + 1) for _ in range(batch_count + 1)]
    reachable[0][0] = True
    counts[0][0] = 1

    pinned_index = None
    if pinned_batch is not None and pinned_hour is not None:
        pinned_index = pinned_hour - start_hour + 1  # boundary index

    for i in range(1, batch_count + 1):
        amount = amounts[i - 1]
        for j in range(1, hour_count + 1):
            if pinned_index is not None and i == pinned_batch and j != pinned_index:
                continue
            total = 0
            for k in range(j):
                if reachable[i - 1][k] and prefix[j] - prefix[k] == amount:
                    total += counts[i - 1][k]
            if total:
                reachable[i][j] = True
                counts[i][j] = min(2, total)

    # suffix_ok[i][j]: batches i+1..B can cover hours j..N.
    suffix_ok = [[False] * (hour_count + 1) for _ in range(batch_count + 1)]
    suffix_ok[batch_count][hour_count] = True
    for i in range(batch_count - 1, -1, -1):
        amount = amounts[i]
        for j in range(hour_count):
            for k in range(j + 1, hour_count + 1):
                if not suffix_ok[i + 1][k] or prefix[k] - prefix[j] != amount:
                    continue
                if pinned_index is not None and (i + 1) == pinned_batch and k != pinned_index:
                    continue
                suffix_ok[i][j] = True
                break

    plan_count = counts[batch_count][hour_count]
    candidates = _build_candidates(
        start_hour,
        amounts,
        batches,
        prefix,
        reachable,
        suffix_ok,
        hour_count,
        pinned_batch,
        pinned_index,
    )
    plan = None
    if plan_count == 1:
        plan = _unique_plan(
            start_hour=start_hour,
            amounts=amounts,
            batches=batches,
            prefix=prefix,
            reachable=reachable,
            hour_count=hour_count,
            pinned_batch=pinned_batch,
            pinned_index=pinned_index,
        )

    return Analysis(
        selected_hours=selected,
        missing_hours=(),
        plan_count=plan_count,
        candidates=candidates,
        plan=plan,
        pinned_batch=pinned_batch,
        pinned_hour=pinned_hour,
    )


def analyse_report(
    database: str,
    request: ReconRequest,
) -> tuple[services.CurrentReport, dict[int, int], frozenset[int], Analysis]:
    """Load the current report and analyse one request against it."""

    found = services.get_report_with_hours(
        database, request.store_code, request.business_date
    )
    if found is None:
        raise NotFound(
            f"没有找到 {request.store_code} 在 {request.business_date} 的已生效日报，"
            "无法对账。"
        )
    report, hour_rows = found
    hours_sales = {hour: cents for hour, cents in hour_rows}
    complete_hours = frozenset(hours_sales)
    analysis = analyse(
        start_hour=request.start_hour,
        end_hour=request.end_hour,
        hours_sales=hours_sales,
        complete_hours=complete_hours,
        batches=request.batches,
        pinned_batch=request.pinned_batch,
        pinned_hour=request.pinned_hour,
    )
    return report, hours_sales, complete_hours, analysis


# ---------------------------------------------------------------------------
# Persistence and the 待复核 state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredBatch:
    position: int
    batch_no: str
    amount_cents: int
    start_hour: int
    end_hour: int

    @property
    def hour_count(self) -> int:
        return self.end_hour - self.start_hour + 1


@dataclass(frozen=True)
class StoredReconciliation:
    id: int
    store_id: int
    store_code: str
    store_name: str
    business_date: str
    report_version_id: int
    report_revision: int
    current_report_version_id: int | None
    current_revision: int | None
    start_hour: int
    end_hour: int
    created_at: str
    updated_at: str
    batches: tuple[StoredBatch, ...]

    @property
    def needs_review(self) -> bool:
        """The daily report was corrected after this reconciliation was saved."""

        return (
            self.current_report_version_id is None
            or self.current_report_version_id != self.report_version_id
        )

    @property
    def status_text(self) -> str:
        return "待复核" if self.needs_review else "与当前日报一致"

    @property
    def weekday(self) -> str:
        return services.weekday_name(date.fromisoformat(self.business_date))


_RECON_HEADER_SQL = """
    SELECT rec.id, rec.store_id, rec.business_date, rec.report_version_id,
           rec.start_hour, rec.end_hour, rec.created_at, rec.updated_at,
           s.code AS store_code, s.name AS store_name,
           rv.revision AS report_revision,
           cr.report_version_id AS current_report_version_id,
           crv.revision AS current_revision
    FROM reconciliations rec
    JOIN stores s ON s.id = rec.store_id
    JOIN report_versions rv ON rv.id = rec.report_version_id
    LEFT JOIN current_reports cr
           ON cr.store_id = rec.store_id AND cr.business_date = rec.business_date
    LEFT JOIN report_versions crv ON crv.id = cr.report_version_id
"""


def _stored_from_row(row, batches: tuple[StoredBatch, ...]) -> StoredReconciliation:
    return StoredReconciliation(
        id=row["id"],
        store_id=row["store_id"],
        store_code=row["store_code"],
        store_name=row["store_name"],
        business_date=row["business_date"],
        report_version_id=row["report_version_id"],
        report_revision=row["report_revision"],
        current_report_version_id=row["current_report_version_id"],
        current_revision=row["current_revision"],
        start_hour=row["start_hour"],
        end_hour=row["end_hour"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        batches=batches,
    )


def _fetch_batches(conn, reconciliation_id: int) -> tuple[StoredBatch, ...]:
    rows = conn.execute(
        """
        SELECT position, batch_no, amount_cents, start_hour, end_hour
        FROM reconciliation_batches
        WHERE reconciliation_id = ?
        ORDER BY position
        """,
        (reconciliation_id,),
    ).fetchall()
    return tuple(
        StoredBatch(
            position=r["position"],
            batch_no=r["batch_no"],
            amount_cents=r["amount_cents"],
            start_hour=r["start_hour"],
            end_hour=r["end_hour"],
        )
        for r in rows
    )


def get_reconciliation(
    database: str, store_code: str, business_date: str
) -> StoredReconciliation | None:
    with db.read_snapshot(database) as conn:
        row = conn.execute(
            _RECON_HEADER_SQL
            + " WHERE s.code = ? COLLATE NOCASE AND rec.business_date = ?",
            (store_code.strip(), business_date),
        ).fetchone()
        if row is None:
            return None
        batches = _fetch_batches(conn, row["id"])
    return _stored_from_row(row, batches)


def list_reconciliations(
    database: str, *, limit: int = 100
) -> list[StoredReconciliation]:
    with db.read_snapshot(database) as conn:
        rows = conn.execute(
            _RECON_HEADER_SQL
            + " ORDER BY rec.business_date DESC, s.code ASC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        results = [
            _stored_from_row(row, _fetch_batches(conn, row["id"])) for row in rows
        ]
    return results


def save_reconciliation(
    database: str,
    request: ReconRequest,
    *,
    expected_revision: int,
) -> StoredReconciliation:
    """Save/replace the store-day result iff it is unique and still current.

    Everything (revision check, missing-hours check, DP, write) happens inside
    one ``BEGIN IMMEDIATE`` transaction.  A report correction that commits after
    the page was rendered therefore either wins the race and makes this call
    fail with :class:`ReportChanged`, or happens after this save.  The previous
    reconciliation is never deleted on failure.
    """

    with db.write_transaction(database) as conn:
        store_row = conn.execute(
            "SELECT id, code, name FROM stores WHERE code = ? COLLATE NOCASE",
            (request.store_code,),
        ).fetchone()
        if store_row is None:
            raise NotFound("门店不存在。")

        current = conn.execute(
            """
            SELECT cr.report_version_id, rv.revision
            FROM current_reports cr
            JOIN report_versions rv ON rv.id = cr.report_version_id
            WHERE cr.store_id = ? AND cr.business_date = ?
            """,
            (store_row["id"], request.business_date),
        ).fetchone()
        if current is None:
            raise ReportNotFound(
                f"没有找到 {request.store_code} 在 {request.business_date} 的已生效日报，"
                "无法保存对账结果。"
            )
        if current["revision"] != expected_revision:
            raise ReportChanged(
                f"{request.store_code} {request.business_date} 的日报已更新为修订号 "
                f"{current['revision']}（页面显示的是修订号 {expected_revision}）。"
                "请重新计算后再保存；原对账记录保持不变。"
            )

        hour_rows = conn.execute(
            "SELECT hour, sales_cents FROM report_hours WHERE report_version_id = ?",
            (current["report_version_id"],),
        ).fetchall()
        hours_sales = {row["hour"]: row["sales_cents"] for row in hour_rows}
        complete_hours = frozenset(hours_sales)
        analysis = analyse(
            start_hour=request.start_hour,
            end_hour=request.end_hour,
            hours_sales=hours_sales,
            complete_hours=complete_hours,
            batches=request.batches,
            pinned_batch=request.pinned_batch,
            pinned_hour=request.pinned_hour,
        )
        if analysis.stopped_for_missing:
            raise ReconciliationRejected(
                "以下所选小时尚未报齐，不能保存对账结果："
                + format_hours(analysis.missing_hours)
                + "。"
            )
        if not analysis.unique or analysis.plan is None:
            raise ReconciliationRejected(
                "切分不唯一（或无解），不能保存；请补充已查实的截止小时后重算。"
            )

        now = _now_iso()
        conn.execute(
            """
            INSERT INTO reconciliations
                (store_id, business_date, report_version_id,
                 start_hour, end_hour, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (store_id, business_date) DO UPDATE SET
                report_version_id = excluded.report_version_id,
                start_hour = excluded.start_hour,
                end_hour = excluded.end_hour,
                updated_at = excluded.updated_at
            """,
            (
                store_row["id"],
                request.business_date,
                current["report_version_id"],
                request.start_hour,
                request.end_hour,
                now,
                now,
            ),
        )
        reconciliation_id = conn.execute(
            "SELECT id FROM reconciliations WHERE store_id = ? AND business_date = ?",
            (store_row["id"], request.business_date),
        ).fetchone()["id"]
        conn.execute(
            "DELETE FROM reconciliation_batches WHERE reconciliation_id = ?",
            (reconciliation_id,),
        )
        conn.executemany(
            """
            INSERT INTO reconciliation_batches
                (reconciliation_id, position, batch_no, amount_cents,
                 start_hour, end_hour)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    reconciliation_id,
                    segment.position,
                    segment.batch_no,
                    segment.amount_cents,
                    segment.start_hour,
                    segment.end_hour,
                )
                for segment in analysis.plan
            ],
        )

    stored = get_reconciliation(database, request.store_code, request.business_date)
    assert stored is not None  # just committed in the same database file
    return stored
