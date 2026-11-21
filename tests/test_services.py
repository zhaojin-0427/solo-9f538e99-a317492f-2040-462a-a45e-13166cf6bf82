"""End-to-end tests for import, correction and comparison semantics."""

from __future__ import annotations

import os
import tempfile
import unittest

from weekly_compare import db
from weekly_compare import comparison, services
from weekly_compare.errors import ImportRejected, RevisionConflict

MON_A = "2026-11-02"
MON_B = "2026-11-09"
MON_C = "2026-11-16"
TUESDAY = "2026-11-03"


def csv_bytes(*rows: tuple[int, int]) -> bytes:
    return ("小时,金额分\n" + "".join(f"{h},{a}\n" for h, a in rows)).encode()


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.database)

    def import_report(
        self,
        *,
        store_code="S001",
        store_name="甲店",
        business_date=MON_A,
        revision="1",
        complete_hours="9-11",
        receipts=(),
        raw_csv=None,
    ):
        payload = raw_csv if raw_csv is not None else csv_bytes(*receipts)
        return services.import_daily_report(
            self.database,
            store_code=store_code,
            store_name=store_name,
            business_date=business_date,
            revision=revision,
            complete_hours=complete_hours,
            receipts_csv=payload,
        )


class ImportTests(ServiceTestCase):
    def test_first_import_stores_totals(self):
        result = self.import_report(receipts=[(9, 1000), (10, 2000), (10, 500)])
        self.assertEqual(result.revision, 1)
        self.assertIsNone(result.replaced_revision)
        self.assertEqual(result.total_sales_cents, 3500)
        report = services.get_current_report(self.database, "S001", MON_A)
        self.assertEqual(report.complete_hour_count, 3)
        self.assertEqual(report.total_sales_cents, 3500)
        self.assertEqual(report.receipt_line_count, 3)

    def test_hours_outside_complete_set_are_ignored(self):
        result = self.import_report(
            complete_hours="9-10", receipts=[(9, 100), (10, 200), (11, 9999)]
        )
        self.assertEqual(result.total_sales_cents, 300)

    def test_complete_hour_without_receipts_counts_zero(self):
        result = self.import_report(complete_hours="9-11", receipts=[(9, 500)])
        self.assertEqual(result.total_sales_cents, 500)
        self.assertEqual(result.complete_hours, frozenset({9, 10, 11}))

    def test_fully_reported_day_with_no_receipts(self):
        result = self.import_report(complete_hours="9-10", raw_csv=b"")
        self.assertEqual(result.total_sales_cents, 0)
        self.assertEqual(result.receipt_line_count, 0)

    def test_new_store_requires_name(self):
        with self.assertRaises(ImportRejected):
            self.import_report(store_name="")

    def test_second_store_day_can_reuse_code_without_name(self):
        self.import_report()
        result = self.import_report(business_date=MON_B, store_name="")
        self.assertEqual(result.store.name, "甲店")

    def test_higher_revision_replaces_whole_day(self):
        self.import_report(receipts=[(9, 1000), (10, 1000), (11, 1000)])
        result = self.import_report(
            revision="2", complete_hours="9-10", receipts=[(9, 50), (10, 60)]
        )
        self.assertEqual(result.replaced_revision, 1)
        self.assertEqual(result.total_sales_cents, 110)

        report = services.get_current_report(self.database, "S001", MON_A)
        self.assertEqual(report.revision, 2)
        self.assertEqual(report.complete_hour_count, 2)
        self.assertEqual(report.total_sales_cents, 110)
        # The reduced coverage replaces the old day entirely.
        found = services.get_report_with_hours(self.database, "S001", MON_A)
        self.assertIsNotNone(found)
        _, hours = found
        self.assertEqual(hours, [(9, 50), (10, 60)])

    def test_same_or_lower_revision_is_rejected(self):
        self.import_report(revision="3")
        for revision in ("3", "2"):
            with self.assertRaises(RevisionConflict):
                self.import_report(revision=revision, receipts=[(9, 999)])
        # Nothing changed.
        self.assertEqual(services.get_current_report(self.database, "S001", MON_A).revision, 3)
        self.assertEqual(
            services.get_current_report(self.database, "S001", MON_A).total_sales_cents, 0
        )

    def test_failed_import_keeps_previous_report(self):
        self.import_report(receipts=[(9, 1000)])
        with self.assertRaises(ImportRejected):
            self.import_report(revision="2", raw_csv=b"9,not-a-number\n")
        report = services.get_current_report(self.database, "S001", MON_A)
        self.assertEqual(report.revision, 1)
        self.assertEqual(report.total_sales_cents, 1000)

    def test_failed_validation_rolls_back_store_creation(self):
        with self.assertRaises(ImportRejected):
            self.import_report(raw_csv=b"99,100\n")
        self.assertEqual(services.list_stores(self.database), [])

    def test_amount_must_be_integer_cents(self):
        with self.assertRaises(ImportRejected):
            self.import_report(raw_csv=b"9,10.50\n")

    def test_store_codes_are_case_insensitive(self):
        self.import_report(store_code="s001", store_name="甲店")
        result = self.import_report(
            store_code="S001", store_name="", business_date=MON_B
        )
        self.assertEqual(result.store.code, "s001")
        self.assertEqual(result.store.name, "甲店")
        self.assertEqual(len(services.list_stores(self.database)), 1)


class ComparisonTests(ServiceTestCase):
    def test_hour_alignment_by_store_and_hour(self):
        # Store A: both days reported 9-11; only 10-11 overlap.
        self.import_report(receipts=[(9, 100), (10, 200), (11, 300)])
        self.import_report(
            business_date=MON_B, complete_hours="10-12", receipts=[(10, 500), (11, 700), (12, 900)]
        )
        result = comparison.compare(self.database, MON_A, MON_B)
        self.assertEqual(len(result.rows), 1)
        row = result.rows[0]
        self.assertEqual(row.comparable_hours, 2)
        self.assertEqual(row.sales_a_cents, 500)
        self.assertEqual(row.sales_b_cents, 1200)
        self.assertEqual(row.difference_cents, 700)
        self.assertEqual(result.total.comparable_hours, 2)
        self.assertEqual(result.total.difference_cents, 700)

    def test_complete_hour_without_receipt_is_zero_not_missing(self):
        # Day A: 9-10 reported, hour 10 had no receipts.
        self.import_report(complete_hours="9-10", receipts=[(9, 1000)])
        # Day B: 9-10 reported, both have receipts.
        self.import_report(
            business_date=MON_B, complete_hours="9-10", receipts=[(9, 1000), (10, 400)]
        )
        row = comparison.compare(self.database, MON_A, MON_B).rows[0]
        self.assertEqual(row.comparable_hours, 2)  # hour 10 still comparable
        self.assertEqual(row.sales_a_cents, 1000)
        self.assertEqual(row.sales_b_cents, 1400)
        self.assertEqual(row.difference_cents, 400)

    def test_total_equals_sum_of_stores(self):
        for code, name in [("S001", "甲店"), ("S002", "乙店"), ("S003", "丙店")]:
            self.import_report(
                store_code=code,
                store_name=name,
                complete_hours="9-10",
                receipts=[(9, 100), (10, 200)],
            )
            self.import_report(
                store_code=code,
                store_name=name,
                business_date=MON_B,
                complete_hours="9-10",
                receipts=[(9, 150), (10, 250)],
            )
        result = comparison.compare(self.database, MON_A, MON_B)
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.total.sales_a_cents, sum(r.sales_a_cents for r in result.rows))
        self.assertEqual(result.total.sales_b_cents, sum(r.sales_b_cents for r in result.rows))
        self.assertEqual(result.total.comparable_hours, sum(r.comparable_hours for r in result.rows))

    def test_store_missing_one_week_is_listed_but_not_comparable(self):
        self.import_report(store_code="S001", store_name="甲店")
        self.import_report(
            store_code="S002", store_name="乙店", business_date=MON_B
        )
        result = comparison.compare(self.database, MON_A, MON_B)
        by_code = {row.store_code: row for row in result.rows}
        self.assertEqual(set(by_code), {"S001", "S002"})
        self.assertEqual(by_code["S001"].comparable_hours, 0)
        self.assertFalse(by_code["S001"].has_both_reports)
        self.assertEqual(result.total.comparable_hours, 0)

    def test_stores_without_any_report_do_not_appear(self):
        result = comparison.compare(self.database, MON_A, MON_B)
        self.assertEqual(result.rows, ())
        self.assertFalse(result.has_rows)

    def test_only_mondays_are_accepted(self):
        with self.assertRaises(ImportRejected):
            comparison.compare(self.database, MON_A, TUESDAY)
        with self.assertRaises(ImportRejected):
            comparison.compare(self.database, MON_A, MON_A)

    def test_dates_are_sorted_when_given_in_reverse(self):
        self.import_report(receipts=[(9, 100)])
        self.import_report(business_date=MON_B, receipts=[(9, 300)])
        forward = comparison.compare(self.database, MON_A, MON_B)
        reverse = comparison.compare(self.database, MON_B, MON_A)
        self.assertEqual(forward.date_a, reverse.date_a)
        self.assertEqual(forward.rows[0].sales_a_cents, reverse.rows[0].sales_a_cents)

    def test_correction_changes_comparison_without_touching_other_day(self):
        self.import_report(receipts=[(9, 1000)])
        self.import_report(business_date=MON_B, receipts=[(9, 2000)])
        before = comparison.compare(self.database, MON_A, MON_B)
        self.assertEqual(before.rows[0].difference_cents, 1000)

        # Correct day A upward; day B's stored numbers must not move.
        self.import_report(revision="2", receipts=[(9, 1500)])
        after = comparison.compare(self.database, MON_A, MON_B)
        self.assertEqual(after.rows[0].sales_a_cents, 1500)
        self.assertEqual(after.rows[0].sales_b_cents, 2000)
        self.assertEqual(after.rows[0].difference_cents, 500)

    def test_comparison_ignores_superseded_versions(self):
        self.import_report(receipts=[(9, 1000)])
        self.import_report(revision="2", receipts=[(9, 1234)])
        self.import_report(business_date=MON_B, receipts=[(9, 1234)])
        row = comparison.compare(self.database, MON_A, MON_B).rows[0]
        self.assertEqual(row.sales_a_cents, 1234)
        self.assertEqual(row.revision_a, 2)

    def test_shrinking_reported_hours_reduces_comparable_hours(self):
        self.import_report(complete_hours="9-11", receipts=[(9, 1), (10, 1), (11, 1)])
        self.import_report(
            business_date=MON_B, complete_hours="9-11", receipts=[(9, 1), (10, 1), (11, 1)]
        )
        self.assertEqual(
            comparison.compare(self.database, MON_A, MON_B).rows[0].comparable_hours, 3
        )
        self.import_report(revision="2", complete_hours="9-9", receipts=[(9, 1)])
        after = comparison.compare(self.database, MON_A, MON_B).rows[0]
        self.assertEqual(after.comparable_hours, 1)
        self.assertEqual(after.sales_a_cents, 1)


class SnapshotTests(ServiceTestCase):
    def test_total_and_rows_come_from_one_statement(self):
        """The comparison must not issue per-store queries that could straddle commits."""

        for code in ("S001", "S002"):
            self.import_report(store_code=code, store_name=code, receipts=[(9, 100)])
            self.import_report(
                store_code=code, store_name=code, business_date=MON_B, receipts=[(9, 200)]
            )

        statements: list[str] = []

        original = db.connect

        def spying_connect(path):
            conn = original(path)
            conn.set_trace_callback(lambda s: statements.append(s))
            return conn

        db.connect = spying_connect  # type: ignore[assignment]
        try:
            comparison.compare(self.database, MON_A, MON_B)
        finally:
            db.connect = original  # type: ignore[assignment]

        selects = [
            s for s in statements
            if "current_reports" in s and not s.strip().upper().startswith("PRAGMA")
        ]
        self.assertEqual(len(selects), 1, selects)
        self.assertIn("WITH a AS", selects[0])


if __name__ == "__main__":
    unittest.main()
