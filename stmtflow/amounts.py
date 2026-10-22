"""Parsing of money amounts and dates as they are printed on statements.

Amount forms handled: 1,234.56  (1,234.56)  -1,234.56  1,234.56-  1,234.56 CR / DR  $1,234.56  1,23,456.78
Date forms handled:  03-01-2026  03/01/26  2026-01-03  03 Jan 2026  03-Jan-26  Jan 3, 2026
Day-first vs month-first is decided once per statement from the data (a day > 12 settles it), not guessed per row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

_AMOUNT_RE = re.compile(
    r"""^
    (?P<open>\()?
    (?P<neg>-)?
    [$£€₹¥]?
    (?P<num>\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)
    (?P<close>\))?
    (?P<trail>-)?
    \s?(?P<cd>CR|DR|Cr|Dr|cr|dr)?
    $""",
    re.X,
)


@dataclass(frozen=True)
class Amount:
    value: Decimal          # magnitude, never negative
    marker: Optional[str]   # "-" (minus / parentheses / trailing minus), "CR", "DR" or None


def parse_amount(text: str, require_decimals: bool = True) -> Optional[Amount]:
    t = text.strip().replace(" ", " ").replace("−", "-")
    m = _AMOUNT_RE.match(t)
    if not m:
        return None
    num = m.group("num")
    if require_decimals and "." not in num:
        return None
    if bool(m.group("open")) != bool(m.group("close")):
        return None
    marker = None
    if m.group("open") or m.group("neg") or m.group("trail"):
        marker = "-"
    cd = m.group("cd")
    if cd:
        marker = cd.upper()
    return Amount(Decimal(num.replace(",", "")), marker)


def is_marker_word(text: str) -> Optional[str]:
    t = text.strip().upper()
    return t if t in ("CR", "DR") else None


# ----------------------------------------------------------------------------------------------- dates

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_NUM = re.compile(r"^(\d{1,4})[-/.](\d{1,2})[-/.](\d{1,4})(?!\d)")
_DMY_NAMED = re.compile(r"^(\d{1,2})[-/ .]?([A-Za-z]{3,9})\.?[-/ ,.]*(\d{2,4})(?!\d)")
_MDY_NAMED = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})(?!\d)")


def _year(y: str) -> int:
    n = int(y)
    return n + 2000 if len(y) == 2 else n


def _month(name: str) -> Optional[int]:
    return _MONTHS.get(name[:3].lower())


def parse_date(text: str, order: str = "dmy") -> Optional[date]:
    """Parse a date at the START of text. order is "dmy" or "mdy" and only matters for all-numeric forms."""
    t = text.strip()
    try:
        m = _NUM.match(t)
        if m:
            a, b, c = m.groups()
            if len(a) == 4:
                return date(int(a), int(b), int(c))
            if len(c) not in (2, 4):
                return None
            d_, mo = (int(a), int(b)) if order == "dmy" else (int(b), int(a))
            return date(_year(c), mo, d_)
        m = _DMY_NAMED.match(t)
        if m and _month(m.group(2)):
            return date(_year(m.group(3)), _month(m.group(2)), int(m.group(1)))
        m = _MDY_NAMED.match(t)
        if m and _month(m.group(1)):
            return date(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return None


def looks_like_date(text: str) -> bool:
    """True if text starts with something date-shaped under EITHER day/month order."""
    return parse_date(text, "dmy") is not None or parse_date(text, "mdy") is not None


def infer_order(cells: Iterable[str]) -> Optional[str]:
    """Decide day-first vs month-first from the whole statement. None means the data cannot tell."""
    dmy = mdy = False
    for t in cells:
        m = _NUM.match(t.strip())
        if not m or len(m.group(1)) == 4:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            dmy = True
        if b > 12:
            mdy = True
    if dmy and not mdy:
        return "dmy"
    if mdy and not dmy:
        return "mdy"
    return None
