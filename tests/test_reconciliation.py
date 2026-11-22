"""Tests for payment-slip reconciliation (calculation, saving and review state)."""

from __future__ import annotations

import os
import tempfile
import unittest

from weekly_compare import db, reconciliation, services
from weekly_compare.errors import (
    NotFound,
    ReconciliationRejected,
    ReportChanged,
    ReportNotFound,
)

DAY = "2026-11-03"  # a Tuesday; reconciliation is not tied to Mondays
OTHER_DAY = "2026-11-04"


def csv_bytes(*rows):
    return ("小时,金额分\n" + "".join(f"{h},{a}\n" for h, a in rows)).encode()


class ReconTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = os.path.join(self.tmp.name, "recon.db")
        db.init_db(self.database)

    def import_report(
        self,
        *,
        store_code="S001",
        store_name="甲店",
        business_date=DAY,
        revision="1",
        complete_hours="9-12",
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

    def request(self, batches_text, **overrides):
        values = dict(
            store_code="S001",
            business_date=DAY,
            start_hour="9",
            end_hour="12",
            batches_text=batches_text,
        )
        values.update(overrides)
        return reconciliation.parse_request(**values)

    def analyse(self, batches_text, **overrides):
        request = self.request(batches_text, **overrides)
        return request, reconciliation.analyse_report(self.database, request)


class ParsingTests(ReconTestCase):
    def test_batches_preserve_order_and_accept_negative_amounts(self):
        parsed = reconciliation.parse_batches_text("B1,100\nB2,-50\nB3,0")
        self.assertEqual([b.batch_no for b in parsed], ["B1", "B2", "B3"])
        self.assertEqual([b.amount_cents for b in parsed], [100, -50, 0])
        self.assertEqual([b.position for b in parsed], [1, 2, 3])

    def test_rejects_bad_batch_lines(self):
        for text in ("B1\n", "B1,1,2\n", "B1,1.5\n", "B1 B2,100\n", "\n"):
            with self.subTest(text=text):
                with self.assertRaises(ReconciliationRejected):
                    reconciliation.parse_batches_text(text)

    def test_rejects_reversed_hour_range(self):
        with self.assertRaises(ReconciliationRejected):
            self.request("B1,100", start_hour="12", end_hour="9")

    def test_pin_requires_both_fields_and_in_range(self):
        with self.assertRaises(ReconciliationRejected):
            self.request("B1,100", pinned_batch="1")
        with self.assertRaises(ReconciliationRejected):
            self.request("B1,100", pinned_batch="2", pinned_hour="10")
        with self.assertRaises(ReconciliationRejected):
            self.request("B1,100", pinned_batch="1", pinned_hour="15")


class AnalysisTests(ReconTestCase):
    def test_unique_plan(self):
        # hours 9..12 -> 100, 200, 300, 400; batches 300 then 700.
        self.import_report(receipts=[(9, 100), (10, 200), (11, 300), (12, 400)])
        request, (report, _, _, analysis) = self.analyse("A,300\nB,700")
        self.assertTrue(analysis.unique)
        self.assertEqual(
            [(s.batch_no, s.start_hour, s.end_hour) for s in analysis.plan],
            [("A", 9, 10), ("B", 11, 12)],
        )
        self.assertEqual(
            [(c.batch_no, c.end_hours) for c in analysis.candidates],
            [("A", (10,)), ("B", (12,))],
        )

    def test_zero_sales_hours_participate(self):
        # Hour 12 is complete but has zero sales; it is still assigned to C.
        self.import_report(receipts=[(9, 100), (10, 50), (11, 300)])
        _, (_, _, _, analysis) = self.analyse("A,100\nB,350\nC,0")
        self.assertTrue(analysis.unique)
        self.assertEqual(
            [(s.batch_no, s.start_hour, s.end_hour) for s in analysis.plan],
            [("A", 9, 9), ("B", 10, 11), ("C", 12, 12)],
        )

    def test_multiple_plans_lists_possible_end_hours_but_cannot_save(self):
        # 2 batches, 3 zero hours: first batch may end at 9 or 10; the last
        # batch always ends on the closing hour.
        self.import_report(receipts=[(9, 0), (10, 0), (11, 0)], complete_hours="9-11")
        _, (_, _, _, analysis) = self.analyse(
            "A,0\nB,0", start_hour="9", end_hour="11"
        )
        self.assertFalse(analysis.unique)
        self.assertTrue(analysis.has_solution)
        self.assertEqual(analysis.plan_count, 2)
        self.assertEqual(analysis.candidates[0].end_hours, (9, 10))
        self.assertEqual(analysis.candidates[1].end_hours, (11,))

    def test_pinned_hour_can_make_a_plan_unique(self):
        self.import_report(receipts=[(9, 0), (10, 0), (11, 0)], complete_hours="9-11")
        _, (_, _, _, analysis) = self.analyse(
            "A,0\nB,0", start_hour="9", end_hour="11", pinned_batch="1", pinned_hour="10"
        )
        self.assertTrue(analysis.unique)
        self.assertEqual(
            [(s.start_hour, s.end_hour) for s in analysis.plan], [(9, 10), (11, 11)]
        )
        # The candidate list must also honour the pin, not repeat the free choice.
        self.assertEqual(analysis.candidates[0].end_hours, (10,))

    def test_pinned_right_batch_keeps_the_other_batches_contiguous(self):
        self.import_report(
            complete_hours="9-12", receipts=[(9, 0), (10, 0), (11, 0), (12, 0)]
        )
        _, (_, _, _, analysis) = self.analyse(
            "A,0\nB,0\nC,0",
            start_hour="9",
            end_hour="12",
            pinned_batch="2",
            pinned_hour="10",
        )
        self.assertTrue(analysis.unique)
        self.assertEqual(
            [(s.batch_no, s.start_hour, s.end_hour) for s in analysis.plan],
            [("A", 9, 9), ("B", 10, 10), ("C", 11, 12)],
        )

    def test_pinned_hour_with_no_solution(self):
        self.import_report(receipts=[(9, 0), (10, 0), (11, 0)], complete_hours="9-11")
        _, (_, _, _, analysis) = self.analyse(
            "A,0\nB,0", start_hour="9", end_hour="11", pinned_batch="1", pinned_hour="11"
        )
        self.assertEqual(analysis.plan_count, 0)
        self.assertFalse(analysis.has_solution)

    def test_no_solution_when_amounts_do_not_match(self):
        self.import_report(receipts=[(9, 100), (10, 200)])
        _, (_, _, _, analysis) = self.analyse("A,999\nB,1")
        self.assertFalse(analysis.has_solution)
        self.assertTrue(all(c.end_hours == () for c in analysis.candidates))

    def test_missing_hours_stop_reconciliation(self):
        self.import_report(complete_hours="9-10", receipts=[(9, 100), (10, 100)])
        _, (_, _, _, analysis) = self.analyse("A,100\nB,100")
        self.assertTrue(analysis.stopped_for_missing)
        self.assertEqual(analysis.missing_hours, (11, 12))
        self.assertEqual(analysis.plan_count, 0)
        self.assertFalse(analysis.can_save)

    def test_negative_segment_matches(self):
        self.import_report(receipts=[(9, 500), (10, -200), (11, 100)])
        _, (_, _, _, analysis) = self.analyse(
            "A,300\nB,100", start_hour="9", end_hour="11"
        )
        self.assertTrue(analysis.unique)
        self.assertEqual(
            [(s.start_hour, s.end_hour, s.sales_cents) for s in analysis.plan],
            [(9, 10, 300), (11, 11, 100)],
        )

    def test_more_batches_than_hours_has_no_solution(self):
        self.import_report(complete_hours="9-10", receipts=[(9, 1), (10, 1)])
        _, (_, _, _, analysis) = self.analyse(
            "A,1\nB,1\nC,0", start_hour="9", end_hour="10"
        )
        self.assertEqual(analysis.plan_count, 0)

    def test_missing_report_is_not_found(self):
        request = self.request("A,1")
        with self.assertRaises(NotFound):
            reconciliation.analyse_report(self.database, request)


class SaveTests(ReconTestCase):
    def setUp(self):
        super().setUp()
        self.import_report(receipts=[(9, 100), (10, 200), (11, 300), (12, 400)])

    def test_saves_unique_plan_and_reads_it_back(self):
        request = self.request("A,300\nB,700")
        saved = reconciliation.save_reconciliation(
            self.database, request, expected_revision=1
        )
        self.assertEqual(saved.report_revision, 1)
        self.assertFalse(saved.needs_review)
        self.assertEqual(
            [(b.batch_no, b.start_hour, b.end_hour) for b in saved.batches],
            [("A", 9, 10), ("B", 11, 12)],
        )
        fetched = reconciliation.get_reconciliation(self.database, "S001", DAY)
        self.assertEqual([b.batch_no for b in fetched.batches], ["A", "B"])

    def test_overwrites_previous_result_for_same_store_day(self):
        reconciliation.save_reconciliation(
            self.database, self.request("A,300\nB,700"), expected_revision=1
        )
        reconciliation.save_reconciliation(
            self.database, self.request("A,100\nB,900"), expected_revision=1
        )
        records = reconciliation.list_reconciliations(self.database)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0].batches), 2)
        self.assertEqual(records[0].batches[0].end_hour, 9)

    def test_refuses_to_save_non_unique_plan(self):
        self.import_report(
            revision="2",
            complete_hours="9-11",
            receipts=[(9, 0), (10, 0), (11, 0)],
        )
        request = self.request("A,0\nB,0", start_hour="9", end_hour="11")
        with self.assertRaises(ReconciliationRejected):
            reconciliation.save_reconciliation(
                self.database, request, expected_revision=2
            )
        self.assertIsNone(reconciliation.get_reconciliation(self.database, "S001", DAY))

    def test_refuses_to_save_with_missing_hours(self):
        request = self.request("A,300\nB,700", start_hour="9", end_hour="13")
        with self.assertRaises(ReconciliationRejected):
            reconciliation.save_reconciliation(
                self.database, request, expected_revision=1
            )

    def test_stale_revision_is_refused_and_old_record_kept(self):
        reconciliation.save_reconciliation(
            self.database, self.request("A,300\nB,700"), expected_revision=1
        )
        # A correction arrives after the reconciliation page was rendered.
        self.import_report(
            revision="2",
            receipts=[(9, 100), (10, 200), (11, 300), (12, 500)],
        )
        with self.assertRaises(ReportChanged):
            reconciliation.save_reconciliation(
                self.database, self.request("A,300\nB,700"), expected_revision=1
            )
        stored = reconciliation.get_reconciliation(self.database, "S001", DAY)
        self.assertEqual(stored.report_revision, 1)
        self.assertTrue(stored.needs_review)
        self.assertEqual(stored.current_revision, 2)

    def test_correction_marks_existing_record_for_review(self):
        reconciliation.save_reconciliation(
            self.database, self.request("A,300\nB,700"), expected_revision=1
        )
        stored = reconciliation.get_reconciliation(self.database, "S001", DAY)
        self.assertFalse(stored.needs_review)

        self.import_report(
            revision="2", receipts=[(9, 100), (10, 200), (11, 300), (12, 400)]
        )
        stored = reconciliation.get_reconciliation(self.database, "S001", DAY)
        self.assertEqual(stored.report_revision, 1)
        self.assertEqual(stored.current_revision, 2)
        self.assertTrue(stored.needs_review)
        self.assertEqual(stored.status_text, "待复核")
        self.assertEqual([b.batch_no for b in stored.batches], ["A", "B"])

    def test_recomputing_on_new_revision_clears_review_state(self):
        reconciliation.save_reconciliation(
            self.database, self.request("A,300\nB,700"), expected_revision=1
        )
        self.import_report(
            revision="2", receipts=[(9, 100), (10, 200), (11, 300), (12, 400)]
        )
        self.assertTrue(
            reconciliation.get_reconciliation(self.database, "S001", DAY).needs_review
        )
        saved = reconciliation.save_reconciliation(
            self.database, self.request("A,300\nB,700"), expected_revision=2
        )
        self.assertEqual(saved.report_revision, 2)
        self.assertFalse(saved.needs_review)
        self.assertEqual(saved.status_text, "与当前日报一致")

    def test_records_are_per_store(self):
        self.import_report(
            store_code="S002", store_name="乙店", receipts=[(9, 100), (10, 200)]
        )
        reconciliation.save_reconciliation(
            self.database, self.request("A,300\nB,700"), expected_revision=1
        )
        other = reconciliation.get_reconciliation(self.database, "S002", DAY)
        self.assertIsNone(other)

    def test_saving_a_store_without_current_report_is_rejected(self):
        request = self.request("A,1", business_date=OTHER_DAY)
        with self.assertRaises(ReportNotFound):
            reconciliation.save_reconciliation(
                self.database, request, expected_revision=1
            )


class BruteForceCrossCheckTests(unittest.TestCase):
    """Small randomised cases checked against an exhaustive solver."""

    def test_dp_matches_exhaustive_enumeration(self):
        import itertools
        import random

        rng = random.Random(20261103)
        for _ in range(600):
            hour_count = rng.randint(1, 5)
            hours = [rng.randint(-3, 3) for _ in range(hour_count)]
            batch_count = rng.randint(1, min(3, hour_count))
            amounts = [rng.randint(-5, 5) for _ in range(batch_count)]
            pin = rng.random() < 0.35
            pin_batch = rng.randint(1, batch_count) if pin else None
            pin_offset = rng.randint(0, hour_count - 1) if pin else None

            start = 10
            sales = {start + i: hours[i] for i in range(hour_count)}
            batches = tuple(
                reconciliation.BatchInput(i + 1, f"b{i + 1}", amounts[i])
                for i in range(batch_count)
            )
            analysis = reconciliation.analyse(
                start_hour=start,
                end_hour=start + hour_count - 1,
                hours_sales=sales,
                complete_hours=frozenset(sales),
                batches=batches,
                pinned_batch=pin_batch,
                pinned_hour=(start + pin_offset) if pin else None,
            )

            plans = []
            for cuts in itertools.combinations(range(1, hour_count), batch_count - 1):
                bounds = (0,) + cuts + (hour_count,)
                if any(
                    sum(hours[bounds[i]:bounds[i + 1]]) != amounts[i]
                    for i in range(batch_count)
                ):
                    continue
                if pin and bounds[pin_batch] != pin_offset + 1:
                    continue
                plans.append(bounds)

            with self.subTest(
                hours=hours, amounts=amounts, pin_batch=pin_batch, pin=pin_offset
            ):
                self.assertEqual(
                    analysis.plan_count, min(2, len(plans))
                )
                for index in range(batch_count):
                    expected = {start + b[index + 1] - 1 for b in plans}
                    self.assertEqual(set(analysis.candidates[index].end_hours), expected)
                if len(plans) == 1 and analysis.plan:
                    self.assertEqual(
                        [(s.start_hour, s.end_hour) for s in analysis.plan],
                        [
                            (start + plans[0][i], start + plans[0][i + 1] - 1)
                            for i in range(batch_count)
                        ],
                    )


if __name__ == "__main__":
    unittest.main()
