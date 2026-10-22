"""Score an extraction against ground truth (printed order)."""
from __future__ import annotations

import re
from decimal import Decimal


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def compare(result: dict, truth: dict) -> dict:
    """result is Statement.to_json(); truth is the generator's truth dict. Returns row/statement exactness."""
    got, exp = result["transactions"], truth["transactions"]
    money_ok = text_ok = exact = 0
    for i, e in enumerate(exp):
        if i >= len(got):
            break
        g = got[i]
        m = (g["date"] == e["date"] and Decimal(g["amount"]) == Decimal(e["amount"]) and
             (e["balance"] is None or (g["balance"] is not None and Decimal(g["balance"]) == Decimal(e["balance"]))))
        t = _ws(g["description"]) == _ws(e["description"])
        money_ok += m
        text_ok += t
        exact += m and t
    n = len(exp)
    same_len = len(got) == n
    return {
        "rows_expected": n, "rows_got": len(got), "rows_exact": exact, "rows_money_ok": money_ok,
        "statement_exact": same_len and exact == n,
        "statement_money_exact": same_len and money_ok == n,
        "status": result["status"],
    }
