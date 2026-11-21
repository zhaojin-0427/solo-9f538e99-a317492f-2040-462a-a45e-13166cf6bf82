"""Concurrency / atomicity tests.

These cover the two guarantees that are easy to get subtly wrong:

* readers keep seeing the previous committed report while an import transaction
  is still open (SQLite WAL), and
* a failure anywhere inside the import rolls back *every* table, not just the
  last one written.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest

from weekly_compare import comparison, db, services

MON_A = "2026-11-02"
MON_B = "2026-11-09"


def csv_bytes(*rows):
    return ("小时,金额分\n" + "".join(f"{h},{a}\n" for h, a in rows)).encode()


class AtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = os.path.join(self.tmp.name, "atomic.db")
        db.init_db(self.database)

    def import_report(self, **overrides):
        kwargs = dict(
            store_code="S001",
            store_name="甲店",
            business_date=MON_A,
            revision="1",
            complete_hours="9-10",
            receipts_csv=csv_bytes((9, 1000), (10, 2000)),
        )
        kwargs.update(overrides)
        return services.import_daily_report(self.database, **kwargs)

    def test_db_error_mid_import_rolls_back_every_table(self):
        self.import_report()

        conn = db.connect(self.database)
        try:
            conn.execute(
                """
                CREATE TRIGGER fail_receipt_lines
                BEFORE INSERT ON receipt_lines
                BEGIN
                    SELECT RAISE(ABORT, 'simulated disk failure');
                END
                """
            )
        finally:
            conn.close()

        with self.assertRaises(Exception):
            self.import_report(revision="2", receipts_csv=csv_bytes((9, 9999), (10, 9999)))

        report = services.get_current_report(self.database, "S001", MON_A)
        self.assertEqual(report.revision, 1)
        self.assertEqual(report.total_sales_cents, 3000)

        conn = db.connect(self.database)
        try:
            # No half-written version, no orphan hours, no stray pointer.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM report_hours").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM receipt_lines").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM current_reports").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_revision_conflict_leaves_no_new_rows(self):
        self.import_report(revision="5")
        with self.assertRaises(Exception):
            self.import_report(revision="5")
        conn = db.connect(self.database)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_failed_first_import_of_new_store_creates_nothing(self):
        conn = db.connect(self.database)
        try:
            conn.execute(
                """
                CREATE TRIGGER fail_hours
                BEFORE INSERT ON report_hours
                BEGIN
                    SELECT RAISE(ABORT, 'simulated failure');
                END
                """
            )
        finally:
            conn.close()

        with self.assertRaises(Exception):
            services.import_daily_report(
                self.database,
                store_code="NEW1",
                store_name="新店",
                business_date=MON_A,
                revision="1",
                complete_hours="9-9",
                receipts_csv=csv_bytes((9, 100)),
            )

        # The store row created earlier in the same transaction is rolled back too.
        self.assertEqual(services.list_stores(self.database), [])


class SnapshotVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = os.path.join(self.tmp.name, "snapshot.db")
        db.init_db(self.database)
        for code, name, amount in (("S001", "甲店", 1000), ("S002", "乙店", 2000)):
            for day, value in ((MON_A, amount), (MON_B, amount + 500)):
                services.import_daily_report(
                    self.database,
                    store_code=code,
                    store_name=name,
                    business_date=day,
                    revision="1",
                    complete_hours="9-9",
                    receipts_csv=csv_bytes((9, value)),
                )

    def test_reader_sees_old_values_while_writer_transaction_is_open(self):
        ready = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def writer():
            try:
                with db.write_transaction(self.database) as conn:
                    row = conn.execute(
                        "SELECT id FROM stores WHERE code = 'S001'"
                    ).fetchone()
                    cursor = conn.execute(
                        """
                        INSERT INTO report_versions
                            (store_id, business_date, revision, complete_hour_count,
                             total_sales_cents, receipt_line_count, created_at)
                        VALUES (?, ?, 2, 1, 999999, 0, '2026-01-01T00:00:00+00:00')
                        """,
                        (row["id"], MON_A),
                    )
                    conn.execute(
                        "INSERT INTO report_hours (report_version_id, hour, sales_cents) "
                        "VALUES (?, 9, 999999)",
                        (cursor.lastrowid,),
                    )
                    conn.execute(
                        """
                        UPDATE current_reports SET report_version_id = ?
                        WHERE store_id = ? AND business_date = ?
                        """,
                        (cursor.lastrowid, row["id"], MON_A),
                    )
                    ready.set()
                    # Hold the transaction open while the reader queries.
                    release.wait(timeout=10)
                    raise RuntimeError("simulated failure -> rollback")
            except RuntimeError:
                pass
            except BaseException as exc:  # pragma: no cover - surfaced to main thread
                errors.append(exc)
                ready.set()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(timeout=10), "writer never reached its pause point")

            # Reader (main thread) must see the previous committed report ...
            report = services.get_current_report(self.database, "S001", MON_A)
            self.assertEqual(report.revision, 1)
            self.assertEqual(report.total_sales_cents, 1000)

            # ... and a comparison run right now must be self-consistent.
            result = comparison.compare(self.database, MON_A, MON_B)
            self.assertEqual(result.rows[0].sales_a_cents, 1000)
            self.assertEqual(
                result.total.sales_a_cents, sum(r.sales_a_cents for r in result.rows)
            )
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertEqual(errors, [])

        # After the rollback the report is unchanged.
        report = services.get_current_report(self.database, "S001", MON_A)
        self.assertEqual(report.revision, 1)
        self.assertEqual(report.total_sales_cents, 1000)

    def test_reader_sees_new_values_after_commit(self):
        services.import_daily_report(
            self.database,
            store_code="S001",
            store_name="甲店",
            business_date=MON_A,
            revision="2",
            complete_hours="9-10",
            receipts_csv=csv_bytes((9, 4000), (10, 1)),
        )
        report = services.get_current_report(self.database, "S001", MON_A)
        self.assertEqual(report.revision, 2)
        self.assertEqual(report.complete_hour_count, 2)
        result = comparison.compare(self.database, MON_A, MON_B)
        row = next(r for r in result.rows if r.store_code == "S001")
        self.assertEqual(row.revision_a, 2)
        # Day B only reported hour 9, so hour 10 is not comparable.
        self.assertEqual(row.comparable_hours, 1)
        self.assertEqual(row.sales_a_cents, 4000)
        self.assertEqual(result.total.sales_a_cents, 4000 + 2000)


if __name__ == "__main__":
    unittest.main()
