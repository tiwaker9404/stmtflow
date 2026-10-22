"""Find the transaction table header and turn it into column roles.

The header row is the only per-bank knowledge the parser needs, and it is read from the document itself:
no X thresholds to tune. Header words are mapped to roles (date, desc, debit, credit, amount, balance).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

NUMERIC_ROLES = ("debit", "credit", "amount", "balance")

_DATE = {"date", "dt"}
_DESC = {"description", "particulars", "narration", "details", "transaction", "memo", "remarks", "payee"}
_DEBIT = {"debit", "debits", "withdrawal", "withdrawals", "withdrawn", "dr", "payments", "payment", "charges"}
_CREDIT = {"credit", "credits", "deposit", "deposits", "cr", "receipts", "receipt"}
_AMOUNT = {"amount", "amt"}
_BALANCE = {"balance", "bal"}
_MODIFIERS = {"inr", "usd", "eur", "gbp", "cad", "rs", "amt", "amount", "dr", "cr"}


def _norm(t: str) -> str:
    return re.sub(r"[^a-z]", "", t.lower())


@dataclass
class Col:
    role: str
    x0: float
    x1: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Layout:
    cols: list
    page_width: float
    header_yc: float
    page: int
    extra: dict = field(default_factory=dict)

    def col(self, role: str) -> Optional[Col]:
        return next((c for c in self.cols if c.role == role), None)

    @property
    def roles(self) -> list:
        return [c.role for c in self.cols]

    @property
    def numeric(self) -> list:
        return [c for c in self.cols if c.role in NUMERIC_ROLES]

    def fingerprint(self) -> str:
        """Stable id for the layout: role order plus header centres in 2%-of-page-width bins."""
        parts = [f"{c.role}:{round(c.xc / self.page_width * 50)}" for c in self.cols]
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def label_words(words: list) -> list:
    """Map header words to (role, x0, x1) entries, merging multi-word headers like 'Withdrawal Amt.'"""
    ws = sorted(words, key=lambda w: w.x0)
    toks = [(_norm(w.text), w) for w in ws]
    out: list = []
    i = 0
    while i < len(toks):
        n, w = toks[i]
        nxt = toks[i + 1][0] if i + 1 < len(toks) else ""
        role, end, step = None, w.x1, 1
        if n == "value" and nxt in _DATE:
            role, end, step = "valuedate", toks[i + 1][1].x1, 2
        elif n in ("transaction", "txn", "trans", "posting", "post") and nxt in _DATE:
            role, end, step = "date", toks[i + 1][1].x1, 2
        elif n in ("money", "paid") and nxt in ("out", "in"):
            role, end, step = ("debit" if nxt == "out" else "credit"), toks[i + 1][1].x1, 2
        elif n in _DATE:
            role = "date"
        elif n in _DEBIT:
            role = "debit"
        elif n in _CREDIT:
            role = "credit"
        elif n in _BALANCE:
            role = "balance"
        elif n in _DESC:
            role = "desc"
        elif n in _AMOUNT:
            prev = out[-1] if out else None
            if prev and prev[0] in ("debit", "credit", "balance") and w.x0 - prev[2] < 25:
                out[-1] = (prev[0], prev[1], w.x1)
                i += 1
                continue
            role = "amount"
        elif n in _MODIFIERS:
            prev = out[-1] if out else None
            if prev and prev[0] in ("debit", "credit", "balance", "amount") and w.x0 - prev[2] < 30:
                out[-1] = (prev[0], prev[1], w.x1)
            i += 1
            continue
        if role is None:
            i += 1
            continue
        if out and out[-1][0] == role and w.x0 - out[-1][2] < 40:
            out[-1] = (role, out[-1][1], end)
        else:
            out.append((role, w.x0, end))
        i += step
    return out


def _qualifies(labelled: list) -> bool:
    roles = {r for r, _, _ in labelled if r != "valuedate"}
    return "date" in roles and bool(roles & set(NUMERIC_ROLES)) and len(roles) >= 3


def _to_cols(labelled: list) -> list:
    seen, cols = set(), []
    for role, x0, x1 in sorted(labelled, key=lambda t: t[1]):
        if role in seen and role != "valuedate":
            continue
        seen.add(role)
        cols.append(Col(role, x0, x1))
    return cols


def header_at(lines: list, i: int):
    """If a table header starts at lines[i] (optionally wrapped onto lines[i+1]) return (cols, n_lines)."""
    lab = label_words(lines[i].words)
    if _qualifies(lab):
        return _to_cols(lab), 1
    if i + 1 < len(lines) and 0 < lines[i + 1].yc - lines[i].yc < 18:
        lab2 = label_words(lines[i].words + lines[i + 1].words)
        if _qualifies(lab2):
            return _to_cols(lab2), 2
    return None


def is_header_line(lines: list, i: int) -> int:
    """Number of lines consumed if lines[i] is a (repeated) header, else 0."""
    h = header_at(lines, i)
    return h[1] if h else 0


def find_layout(pages: list) -> Optional[tuple]:
    """First header in the document: (Layout, page_index, line_index, n_lines)."""
    for pi, page in enumerate(pages):
        for li in range(len(page.lines)):
            h = header_at(page.lines, li)
            if h:
                cols, n = h
                yc = sum(page.lines[li + k].yc for k in range(n)) / n
                return Layout(cols, page.width, yc, page.number), pi, li, n
    return None
