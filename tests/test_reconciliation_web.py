"""HTTP tests for the payment-slip reconciliation pages."""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from weekly_compare import create_app, reconciliation, services

DAY = "2026-11-03"


class ReconWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": os.path.join(self.tmp.name, "web-recon.db"),
                "SECRET_KEY": "test-secret",
            }
        )
        self.client = self.app.test_client()
        self.database = self.app.config["DATABASE"]

    def token(self):
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def import_report(
        self,
        *,
        revision="1",
        complete_hours="9-12",
        receipts=(9, 100, 10, 200, 11, 300, 12, 400),
    ):
        pairs = list(zip(receipts[0::2], receipts[1::2]))
        payload = ("小时,金额分\n" + "".join(f"{h},{a}\n" for h, a in pairs)).encode()
        services.import_daily_report(
            self.database,
            store_code="S001",
            store_name="甲店",
            business_date=DAY,
            revision=revision,
            complete_hours=complete_hours,
            receipts_csv=payload,
        )

    def submit(self, *, batches_text, action="calculate", expected_revision="1", **overrides):
        self.client.get("/reconcile")
        data = {
            "csrf_token": self.token(),
            "store_code": "S001",
            "business_date": DAY,
            "start_hour": "9",
            "end_hour": "12",
            "batches_text": batches_text,
            "pinned_batch": "",
            "pinned_hour": "",
            "action": action,
            "expected_revision": expected_revision,
        }
        data.update(overrides)
        return self.client.post("/reconcile", data=data)

    def test_reconcile_page_renders(self):
        response = self.client.get("/reconcile")
        self.assertEqual(response.status_code, 200)
        self.assertIn("交款单对账", response.get_data(as_text=True))

    def test_unique_plan_is_shown_with_last_hours(self):
        self.import_report()
        response = self.submit(batches_text="A,300\nB,700")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("完整对账方案（唯一）", body)
        self.assertIn("可能的最后一个小时", body)
        self.assertIn("A", body)
        self.assertIn("B", body)
        self.assertIn("保存该门店日的对账结果", body)

    def test_missing_hours_are_listed_and_stop(self):
        self.import_report(complete_hours="9-10", receipts=(9, 100, 10, 200))
        response = self.submit(batches_text="A,300")
        body = response.get_data(as_text=True)
        self.assertIn("尚未报齐", body)
        self.assertIn("11-12", body)
        self.assertNotIn("保存该门店日的对账结果", body)

    def test_no_solution_message(self):
        self.import_report()
        response = self.submit(batches_text="A,999\nB,1")
        self.assertIn("无解", response.get_data(as_text=True))

    def test_ambiguous_plan_cannot_be_saved(self):
        self.import_report(receipts=(9, 0, 10, 0, 11, 0))
        services.import_daily_report(
            self.database,
            store_code="S001",
            store_name="甲店",
            business_date=DAY,
            revision="2",
            complete_hours="9-11",
            receipts_csv=b"9,0\n10,0\n11,0\n",
        )
        response = self.submit(
            batches_text="A,0\nB,0", start_hour="9", end_hour="11",
            action="save", expected_revision="2",
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("不唯一", response.get_data(as_text=True))
        self.assertIsNone(reconciliation.get_reconciliation(self.database, "S001", DAY))

    def test_save_then_list_shows_revision_and_hours(self):
        self.import_report()
        response = self.submit(batches_text="A,300\nB,700", action="save")
        self.assertEqual(response.status_code, 302)
        stored = reconciliation.get_reconciliation(self.database, "S001", DAY)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.report_revision, 1)

        page = self.client.get("/reconciliations").get_data(as_text=True)
        self.assertIn("S001", page)
        self.assertIn("9:00–12:00", page)
        self.assertIn("一致", page)
        detail = self.client.get(
            f"/reconcile?store_code=S001&business_date={DAY}"
        ).get_data(as_text=True)
        self.assertIn("9:00–10:00", detail)
        self.assertIn("11:00–12:00", detail)

    def test_correction_marks_saved_result_for_review_on_page(self):
        self.import_report()
        self.submit(batches_text="A,300\nB,700", action="save")
        services.import_daily_report(
            self.database,
            store_code="S001",
            store_name="甲店",
            business_date=DAY,
            revision="2",
            complete_hours="9-12",
            receipts_csv=b"9,100\n10,200\n11,300\n12,400\n",
        )
        page = self.client.get(f"/reconcile?store_code=S001&business_date={DAY}")
        body = page.get_data(as_text=True)
        self.assertIn("待复核", body)
        self.assertIn("保存时的日报修订号", body)
        self.assertIn("9:00–10:00", body)

    def test_stale_save_is_refused_and_old_record_kept(self):
        self.import_report()
        self.submit(batches_text="A,300\nB,700", action="save")
        services.import_daily_report(
            self.database,
            store_code="S001",
            store_name="甲店",
            business_date=DAY,
            revision="2",
            complete_hours="9-12",
            receipts_csv=b"9,100\n10,200\n11,300\n12,500\n",
        )
        response = self.submit(
            batches_text="A,300\nB,700", action="save", expected_revision="1"
        )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 409)
        self.assertIn("重新计算", body)
        # No save button for numbers the reviewer did not calculate.
        self.assertNotIn("保存该门店日的对账结果", body)
        stored = reconciliation.get_reconciliation(self.database, "S001", DAY)
        self.assertEqual(stored.report_revision, 1)
        self.assertTrue(stored.needs_review)

    def test_csrf_required_for_calculate(self):
        self.import_report()
        self.client.get("/reconcile")
        response = self.client.post(
            "/reconcile",
            data={
                "csrf_token": "wrong",
                "store_code": "S001",
                "business_date": DAY,
                "start_hour": "9",
                "end_hour": "12",
                "batches_text": "A,300\nB,700",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_api_lists_reconciliations(self):
        self.import_report()
        self.submit(batches_text="A,300\nB,700", action="save")
        payload = self.client.get("/api/reconciliations").get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["reconciliations"][0]["store_code"], "S001")
        self.assertEqual(
            [b["batch_no"] for b in payload["reconciliations"][0]["batches"]],
            ["A", "B"],
        )


if __name__ == "__main__":
    unittest.main()
