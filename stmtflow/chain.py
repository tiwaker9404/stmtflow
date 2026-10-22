"""The balance chain: the independent check that makes extraction trustworthy.

Every statement that prints a running balance gives a free, exact, per-row test: previous balance + amount must
equal this row's balance, to the cent, in Decimal. That one identity

  * proves a row was read correctly (amount, sign and balance all agree),
  * pinpoints the first wrong row when one is not,
  * detects a dropped or duplicated row (the chain skips or repeats a step),
  * tells debits from credits when the layout gives no sign (the sign is whatever makes the chain close),
  * reveals whether the statement is listed oldest-first or newest-first.

It never edits an extracted value to make things fit. It only reports, and resolves a missing sign.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class Break:
    index: int           # position in the printed order
    kind: str            # balance_mismatch | sign_conflict | amount_mismatch
    expected: Optional[Decimal]
    actual: Optional[Decimal]
    page: int
    top: float


@dataclass
class ChainResult:
    direction: str = "forward"
    checked: int = 0
    breaks: list = field(default_factory=list)
    resolved: int = 0


def _walk(seq: list, opening: Optional[Decimal]):
    """seq is in chronological order. Returns (checked, breaks, resolved_signs{id: signed amount})."""
    prev = opening
    checked = 0
    breaks: list = []
    resolved: dict = {}
    for idx, t in seq:
        b = t.balance
        if b is None:
            prev = None
            continue
        if prev is None:
            prev = b           # first anchor point: nothing to compare against yet
            continue
        checked += 1
        delta = b - prev
        if t.sign_source == "unresolved":
            if abs(delta) == t.amount:
                resolved[idx] = delta
            else:
                breaks.append(Break(idx, "amount_mismatch", prev + t.amount, b, t.page, t.top))
        elif delta == t.amount:
            pass
        elif t.amount != 0 and delta == -t.amount:
            breaks.append(Break(idx, "sign_conflict", prev + t.amount, b, t.page, t.top))
        else:
            breaks.append(Break(idx, "balance_mismatch", prev + t.amount, b, t.page, t.top))
        prev = b
    return checked, breaks, resolved


def run_chain(txns: list, opening: Optional[Decimal]) -> ChainResult:
    """Check the chain in the better of the two directions and resolve unsigned amounts."""
    indexed = list(enumerate(txns))
    if not indexed:
        return ChainResult()
    fwd = _walk(indexed, opening)
    rev = _walk(indexed[::-1], opening)
    # fewer breaks wins; on a tie prefer forward
    use_rev = (len(rev[1]), -rev[0]) < (len(fwd[1]), -fwd[0])
    direction = "reverse" if use_rev else "forward"
    checked, breaks, resolved = rev if use_rev else fwd
    for idx, signed in resolved.items():
        txns[idx].amount = signed
        txns[idx].sign_source = "balance_delta"
    breaks.sort(key=lambda b: b.index)
    return ChainResult(direction, checked, breaks, len(resolved))
