"""Rendering helpers shared by the HTML pages and the JSON API."""

from __future__ import annotations

from decimal import Decimal


def cents_to_yuan(cents: int) -> str:
    """``123456 -> '1,234.56'`` without floats and without a currency symbol."""

    value = Decimal(int(cents)) / Decimal(100)
    return f"{value:,.2f}"


def cents_to_yuan_signed(cents: int) -> str:
    """Signed variant used for the two-week difference column."""

    if cents > 0:
        return f"+{cents_to_yuan(cents)}"
    if cents < 0:
        return f"-{cents_to_yuan(-cents)}"
    return cents_to_yuan(0)


def pct_change(old_cents: int, new_cents: int) -> str:
    """Percentage change, or a dash when the base is zero.

    A zero base makes a percentage meaningless; showing ``—`` avoids implying
    infinite growth.
    """

    if old_cents == 0:
        return "—"
    ratio = (Decimal(new_cents) - Decimal(old_cents)) / Decimal(abs(old_cents)) * 100
    return f"{ratio:+.1f}%"


def mask_code(code: str) -> str:
    return code
