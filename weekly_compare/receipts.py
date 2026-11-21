"""Parsing and validation for the daily-report import format.

Two user-supplied inputs live here:

``业务日期 / 修订号`` are plain scalars validated in :mod:`weekly_compare.services`.
``已报齐的营业小时`` is a range string such as ``9-13,15-17`` (endpoints
inclusive).
``小票 CSV`` has exactly two columns: ``小时,金额分``.  A header row is optional.
Hourly totals are aggregated here so the import transaction stays simple.

Everything is validated *before* the database transaction starts, so a bad file
can never touch the previous report.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass

from .errors import ImportRejected

MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_ROWS = 100_000
MAX_AMOUNT_CENTS = 10**12  # 10 billion yuan per receipt line; keeps sums in int64
MAX_TOTAL_CENTS = 2**63 - 1

_INT_RE = re.compile(r"^[+-]?\d+$")
_HEADER_CHARS = re.compile(r"[A-Za-z一-鿿]")


@dataclass(frozen=True)
class ParsedReceipts:
    """A validated CSV payload."""

    lines: tuple[tuple[int, int], ...]  # (hour, amount_cents), original order
    hourly_totals: dict[int, int]


def _looks_like_header(cells: list[str]) -> bool:
    """Both cells non-numeric and containing letters/CJK -> header row."""

    return all(
        cell
        and not _INT_RE.fullmatch(cell)
        and _HEADER_CHARS.search(cell)
        for cell in cells
    )


def _decode(raw: bytes) -> str:
    if not raw.strip():
        # No receipts at all is a legitimate state: a day can be fully reported
        # with zero receipts, in which case every 报齐 hour counts as 0.
        return ""
    if len(raw) > MAX_CSV_BYTES:
        raise ImportRejected(
            f"CSV 文件过大（上限 {MAX_CSV_BYTES // 1024 // 1024} MB）。",
        )
    if b"\x00" in raw:
        raise ImportRejected("CSV 文件包含二进制内容，无法解析。")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportRejected("CSV 编码无法识别，请使用 UTF-8 或 GB18030。")


def parse_receipts_csv(raw: bytes) -> ParsedReceipts:
    """Parse ``小时,金额分`` rows.

    Empty input is allowed on purpose: a day can be fully reported with no
    receipts, in which case every complete hour counts as zero sales.
    """

    text = _decode(raw)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    lines: list[tuple[int, int]] = []
    hourly: dict[int, int] = {}
    first_record = True

    try:
        for record_no, row in enumerate(reader, start=1):
            cells = [cell.strip() for cell in row]
            if not cells or all(not cell for cell in cells):
                continue
            if len(cells) != 2:
                raise ImportRejected(
                    f"CSV 第 {record_no} 行应有两列（小时,金额分），实际有 {len(cells)} 列。"
                )
            if first_record:
                first_record = False
                if _looks_like_header(cells):
                    continue
            hour_text, amount_text = cells
            if not _INT_RE.fullmatch(hour_text):
                raise ImportRejected(
                    f"CSV 第 {record_no} 行的小时不是整数：{hour_text!r}。"
                )
            if not _INT_RE.fullmatch(amount_text):
                raise ImportRejected(
                    f"CSV 第 {record_no} 行的金额必须是整数分：{amount_text!r}。"
                )
            hour = int(hour_text)
            amount = int(amount_text)
            if not 0 <= hour <= 23:
                raise ImportRejected(
                    f"CSV 第 {record_no} 行的小时 {hour} 超出 0–23。"
                )
            if abs(amount) > MAX_AMOUNT_CENTS:
                raise ImportRejected(
                    f"CSV 第 {record_no} 行的金额超出允许范围。"
                )
            lines.append((hour, amount))
            hourly[hour] = hourly.get(hour, 0) + amount
            if len(lines) > MAX_RECEIPT_ROWS:
                raise ImportRejected(
                    f"CSV 小票行数超过 {MAX_RECEIPT_ROWS} 行上限。"
                )
    except csv.Error as exc:
        raise ImportRejected(f"CSV 格式错误：{exc}") from exc

    if any(abs(total) > MAX_TOTAL_CENTS for total in hourly.values()):
        raise ImportRejected("CSV 金额合计超出允许范围。")

    return ParsedReceipts(lines=tuple(lines), hourly_totals=hourly)


def parse_complete_hours(value: str) -> frozenset[int]:
    """Parse the "已报齐营业小时" field into a set of integers 0–23.

    Accepted syntax (full-width punctuation is normalised)::

        9-12,14        -> {9, 10, 11, 12, 14}   (endpoints inclusive)
        9-12、14；15   -> same
        0-23           -> every hour
    """

    if value is None or not str(value).strip():
        raise ImportRejected("请填写已报齐的营业小时，例如 9-17 或 9-12,14-17。")

    normalised = unicodedata.normalize("NFKC", str(value))
    normalised = re.sub(r"[~～—–−至]", "-", normalised)
    # Spaces around a hyphen are part of the range ("9 - 12"), not a separator.
    normalised = re.sub(r"\s*-\s*", "-", normalised)
    normalised = re.sub(r"[、;；，\s]+", ",", normalised)

    hours: set[int] = set()
    for token in normalised.split(","):
        if not token:
            continue
        parts = token.split("-")
        if len(parts) == 1:
            start = end = _parse_hour_token(token)
        elif len(parts) == 2:
            start = _parse_hour_token(parts[0])
            end = _parse_hour_token(parts[1])
            if start > end:
                raise ImportRejected(
                    f"营业小时范围 {token!r} 的起点大于终点。"
                )
        else:
            raise ImportRejected(f"无法识别营业小时范围 {token!r}。")
        hours.update(range(start, end + 1))

    if not hours:
        raise ImportRejected("请至少填写一个已报齐的营业小时。")
    return frozenset(hours)


def _parse_hour_token(token: str) -> int:
    if not re.fullmatch(r"\d{1,2}", token):
        raise ImportRejected(
            f"无法识别营业小时 {token!r}；请使用 9-17 这样的整点写法。"
        )
    hour = int(token)
    if not 0 <= hour <= 23:
        raise ImportRejected(f"营业小时 {hour} 超出 0–23。")
    return hour


def format_hours(hours: int | frozenset[int] | set[int] | list[int] | tuple[int, ...]) -> str:
    """Compact display helper: ``{9,10,11,14} -> '9-11, 14'``."""

    if isinstance(hours, int):
        values = [hours]
    else:
        values = sorted(hours)
    if not values:
        return "—"
    groups: list[str] = []
    start = prev = values[0]
    for hour in values[1:]:
        if hour == prev + 1:
            prev = hour
            continue
        groups.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = hour
    groups.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(groups)
