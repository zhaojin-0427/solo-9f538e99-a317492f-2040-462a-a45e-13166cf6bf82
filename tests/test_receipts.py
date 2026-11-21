"""Parsing-level tests for receipt CSV and complete-hour ranges."""

import unittest

from weekly_compare.errors import ImportRejected
from weekly_compare.receipts import (
    format_hours,
    parse_complete_hours,
    parse_receipts_csv,
)


class CompleteHoursTests(unittest.TestCase):
    def test_single_range_is_inclusive(self):
        self.assertEqual(parse_complete_hours("9-12"), frozenset({9, 10, 11, 12}))

    def test_multiple_ranges_and_singles(self):
        self.assertEqual(
            parse_complete_hours("9-11,14,20-21"), frozenset({9, 10, 11, 14, 20, 21})
        )

    def test_fullwidth_and_chinese_separators(self):
        self.assertEqual(
            parse_complete_hours("９－１１，１４、２０～２１"),
            frozenset({9, 10, 11, 14, 20, 21}),
        )

    def test_rejects_reversed_range(self):
        with self.assertRaises(ImportRejected):
            parse_complete_hours("17-9")

    def test_rejects_out_of_bounds(self):
        with self.assertRaises(ImportRejected):
            parse_complete_hours("9-24")

    def test_rejects_empty(self):
        with self.assertRaises(ImportRejected):
            parse_complete_hours("   ")

    def test_format_hours_roundtrip(self):
        self.assertEqual(format_hours(parse_complete_hours("9-11,14")), "9-11, 14")
        self.assertEqual(format_hours(frozenset()), "—")


class ReceiptCsvTests(unittest.TestCase):
    def test_header_is_optional(self):
        with_header = parse_receipts_csv("小时,金额分\n9,1200\n10,800\n".encode())
        without = parse_receipts_csv(b"9,1200\n10,800\n")
        self.assertEqual(with_header.lines, without.lines)
        self.assertEqual(with_header.hourly_totals, without.hourly_totals)

    def test_aggregates_per_hour(self):
        parsed = parse_receipts_csv(b"9,1200\n9,800\n10,0\n")
        self.assertEqual(parsed.hourly_totals, {9: 2000, 10: 0})
        self.assertEqual(len(parsed.lines), 3)

    def test_header_only_is_allowed_and_empty_data(self):
        parsed = parse_receipts_csv("小时,金额分\n".encode())
        self.assertEqual(parsed.lines, ())
        self.assertEqual(parsed.hourly_totals, {})

    def test_blank_lines_ignored(self):
        parsed = parse_receipts_csv(b"\n9,100\n\n")
        self.assertEqual(parsed.lines, ((9, 100),))

    def test_rejects_wrong_column_count(self):
        with self.assertRaises(ImportRejected):
            parse_receipts_csv(b"9,100,extra\n")

    def test_rejects_non_integer_amounts(self):
        with self.assertRaises(ImportRejected):
            parse_receipts_csv(b"9,12.00\n")

    def test_rejects_out_of_range_hour(self):
        with self.assertRaises(ImportRejected):
            parse_receipts_csv(b"24,100\n")

    def test_empty_file_means_no_receipts(self):
        parsed = parse_receipts_csv(b"")
        self.assertEqual(parsed.lines, ())
        self.assertEqual(parsed.hourly_totals, {})

    def test_accepts_gb18030(self):
        parsed = parse_receipts_csv("小时,金额分\n9,100\n".encode("gb18030"))
        self.assertEqual(parsed.lines, ((9, 100),))


if __name__ == "__main__":
    unittest.main()
