"""HTTP-level tests: pages, form import, CSRF, JSON API and error rendering."""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from weekly_compare import create_app
from weekly_compare import services

MON_A = "2026-11-02"
MON_B = "2026-11-09"


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": os.path.join(self.tmp.name, "web.db"),
                "SECRET_KEY": "test-secret",
                "MAX_CONTENT_LENGTH": 1024 * 1024,
            }
        )
        self.client = self.app.test_client()
        self.database = self.app.config["DATABASE"]

    def token(self):
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def open_import_page(self):
        response = self.client.get("/import")
        self.assertEqual(response.status_code, 200)
        return self.token()

    def post_import(
        self,
        *,
        store_code="S001",
        store_name="甲店",
        business_date=MON_A,
        revision="1",
        complete_hours="9-10",
        csv_text="小时,金额分\n9,1000\n10,2000\n",
        token=None,
        as_file=True,
    ):
        data = {
            "csrf_token": token if token is not None else self.open_import_page(),
            "store_code": store_code,
            "store_name": store_name,
            "business_date": business_date,
            "revision": revision,
            "complete_hours": complete_hours,
        }
        if as_file:
            data["receipts"] = (io.BytesIO(csv_text.encode()), "receipts.csv")
        else:
            data["receipt_text"] = csv_text
        return self.client.post("/import", data=data, content_type="multipart/form-data")


class PageTests(WebTestCase):
    def test_empty_pages_render(self):
        for path in ("/", "/import", "/compare"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_index_lists_current_report(self):
        self.post_import()
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertIn("S001", body)
        self.assertIn("30.00", body)  # 9h 1000 + 10h 2000 cents
        self.assertIn("甲店", body)

    def test_report_detail_shows_hours(self):
        self.post_import()
        response = self.client.get(f"/reports/S001/{MON_A}")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("9:00", body)
        self.assertIn("10:00", body)
        self.assertIn("2", body)  # complete hour count

    def test_missing_report_is_404(self):
        response = self.client.get("/reports/S999/2026-11-02")
        self.assertEqual(response.status_code, 404)

    def test_compare_page_renders_rows_and_total(self):
        self.post_import()
        self.post_import(business_date=MON_B, revision="1", csv_text="9,1500\n10,2500\n")
        response = self.client.get(f"/compare?date_a={MON_A}&date_b={MON_B}")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("40.00", body)  # 1500 + 2500 cents
        self.assertIn("30.00", body)
        self.assertIn("+10.00", body)
        self.assertIn("全店合计", body)

    def test_compare_rejects_non_mondays(self):
        response = self.client.get(f"/compare?date_a={MON_A}&date_b=2026-11-03")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("周一", body)

    def test_import_page_prefills_next_revision(self):
        self.post_import(revision="4")
        response = self.client.get(
            f"/import?store_code=S001&business_date={MON_A}"
        )
        self.assertIn('value="5"', response.get_data(as_text=True))


class FormImportTests(WebTestCase):
    def test_form_import_redirects_to_detail(self):
        response = self.post_import()
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/reports/S001/{MON_A}", response.headers["Location"])
        self.assertEqual(
            services.get_current_report(self.database, "S001", MON_A).revision, 1
        )

    def test_pasted_csv_is_accepted(self):
        response = self.post_import(as_file=False, csv_text="9,100\n")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            services.get_current_report(self.database, "S001", MON_A).total_sales_cents,
            100,  # hour 10 is complete but had no receipt -> 0
        )

    def test_import_failure_keeps_old_report_and_returns_422(self):
        self.post_import()
        response = self.post_import(revision="2", csv_text="9,bad\n")
        self.assertEqual(response.status_code, 422)
        body = response.get_data(as_text=True)
        self.assertIn("原日报保持不变", body)
        self.assertEqual(
            services.get_current_report(self.database, "S001", MON_A).revision, 1
        )

    def test_lower_revision_returns_409(self):
        self.post_import(revision="3")
        response = self.post_import(revision="3")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            services.get_current_report(self.database, "S001", MON_A).revision, 3
        )

    def test_csrf_token_is_required(self):
        response = self.post_import(token="wrong-token")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(services.list_stores(self.database), [])

    def test_oversized_upload_returns_413(self):
        # Small limit (and a body below werkzeug's 500 KB multipart spooling
        # threshold) keeps this test fast and avoids temp-file noise.
        small_app = create_app(
            {
                "TESTING": True,
                "DATABASE": self.database,
                "SECRET_KEY": "test-secret",
                "MAX_CONTENT_LENGTH": 64 * 1024,
            }
        )
        client = small_app.test_client()
        client.get("/import")
        with client.session_transaction() as session:
            token = session["_csrf_token"]
        response = client.post(
            "/import",
            data={
                "csrf_token": token,
                "store_code": "S001",
                "store_name": "甲店",
                "business_date": MON_A,
                "revision": "1",
                "complete_hours": "9-10",
                "receipts": (io.BytesIO(b"x" * (128 * 1024)), "big.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(services.list_stores(self.database), [])


class ApiTests(WebTestCase):
    def test_compare_api_shape(self):
        self.post_import()
        self.post_import(business_date=MON_B, csv_text="9,1500\n10,2500\n")
        response = self.client.get(f"/api/compare?date_a={MON_A}&date_b={MON_B}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["weekday"], "周一")
        self.assertEqual(payload["total"]["comparable_hours"], 2)
        self.assertEqual(payload["total"]["sales_a_cents"], 3000)
        self.assertEqual(payload["total"]["sales_b_cents"], 4000)
        self.assertEqual(payload["total"]["difference_cents"], 1000)
        store = payload["stores"][0]
        self.assertEqual(store["store_code"], "S001")
        self.assertEqual(store["revision_a"], 1)
        self.assertEqual(store["revision_b"], 1)

    def test_api_import_returns_201(self):
        token = self.open_import_page()
        response = self.client.post(
            "/api/import",
            data={
                "csrf_token": token,
                "store_code": "S001",
                "store_name": "甲店",
                "business_date": MON_A,
                "revision": "1",
                "complete_hours": "9-10",
                "receipts": (io.BytesIO(b"9,100\n"), "r.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["total_sales_cents"], 100)

    def test_api_errors_are_json(self):
        token = self.open_import_page()
        response = self.client.post(
            "/api/import",
            data={
                "csrf_token": token,
                "store_code": "S001",
                "store_name": "甲店",
                "business_date": MON_A,
                "revision": "1",
                "complete_hours": "9-10",
                "receipts": (io.BytesIO(b"9,oops\n"), "r.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 422)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "import_rejected")

    def test_reports_api_lists_current(self):
        self.post_import()
        payload = self.client.get("/api/reports").get_json()
        self.assertEqual(payload["reports"][0]["store_code"], "S001")
        self.assertEqual(payload["reports"][0]["complete_hour_count"], 2)

    def test_scripted_client_can_import_with_header_token(self):
        client = self.app.test_client()
        token = client.get("/api/csrf").get_json()["csrf_token"]
        response = client.post(
            "/api/import",
            headers={"X-CSRF-Token": token},
            data={
                "store_code": "S001",
                "store_name": "甲店",
                "business_date": MON_A,
                "revision": "1",
                "complete_hours": "9-10",
                "receipts": (io.BytesIO(b"9,100\n"), "r.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        # A wrong header token from the same session is still refused.
        bad = client.post(
            "/api/import",
            headers={"X-CSRF-Token": "nope"},
            data={"store_code": "S001", "business_date": MON_B, "revision": "1",
                  "complete_hours": "9-10"},
        )
        self.assertEqual(bad.status_code, 400)

    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
