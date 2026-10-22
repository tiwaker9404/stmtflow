"""Turn positioned lines into transaction rows.

Design points (each one exists because a real layout broke the naive version):

* Column positions come from the header, then are REFINED from the data: numbers in a column are right-,
  left- or centre-aligned, whichever has the smallest spread. Headers and numbers are often aligned differently.
* A transaction is a BLOCK of lines, not a line. The date/amount line may sit at the top, middle or bottom of the
  block (HTML-to-PDF tables centre it vertically). Blocks are split at the largest vertical gap between date lines.
* CR/DR suffixes can be on the same line as the amount or on the line below it.
* Opening/closing balances and printed totals are collected from anywhere in the document (summary boxes
  above the table are common), because they feed the independent checks later.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .amounts import Amount, is_marker_word, looks_like_date, parse_amount
from .layout import NUMERIC_ROLES, Layout, is_header_line
from .models import Config
from .reader import Line, Word

OPEN_RE = re.compile(r"\b(opening|beginning|previous|brought\s+forward)\b.*\bbalance\b|\bbalance\b.*\b(brought\s+forward|b/f)\b|^\s*b/f\b", re.I)
CLOSE_RE = re.compile(r"\b(closing|ending|carried\s+forward)\b.*\bbalance\b|\bbalance\b.*\b(carried\s+forward|c/f)\b|^\s*c/f\b", re.I)
TOTAL_RE = re.compile(r"^\s*(grand\s+)?totals?\b", re.I)
TOTAL_DEBIT_RE = re.compile(r"\btotal\s+(debits?|withdrawals?|payments?)\b", re.I)
TOTAL_CREDIT_RE = re.compile(r"\btotal\s+(credits?|deposits?|receipts?)\b", re.I)
CURRENCY_WORDS = {"inr", "usd", "eur", "gbp", "cad", "rs", "rs.", "$", "£", "€", "₹"}


@dataclass
class NumTok:
    role: str
    amount: Amount
    x0: float
    x1: float
    yc: float
    text: str
    widx: int = -1


@dataclass
class Cells:
    line: Line
    date_text: str = ""
    desc: list = field(default_factory=list)
    nums: list = field(default_factory=list)
    markers: list = field(default_factory=list)   # (role, "CR"|"DR") found as stand-alone words

    @property
    def desc_text(self) -> str:
        return " ".join(w.text for w in self.desc)


@dataclass
class ColModel:
    role: str
    hx0: float
    hx1: float
    kind: str = "hdr"    # hdr = nearest of header left/centre/right; else x0|xc|x1 learned from data
    pos: float = 0.0

    def dist(self, w: Word) -> float:
        if self.kind == "hdr":
            return min(abs(w.x1 - self.hx1), abs(w.xc - (self.hx0 + self.hx1) / 2), abs(w.x0 - self.hx0))
        return abs(getattr(w, self.kind) - self.pos)

    @property
    def centre(self) -> float:
        return (self.hx0 + self.hx1) / 2 if self.kind == "hdr" else self.pos


def initial_models(layout: Layout) -> list:
    return [ColModel(c.role, c.x0, c.x1) for c in layout.numeric]


def _tol(models: list) -> float:
    if len(models) < 2:
        return 45.0
    cs = sorted(m.centre for m in models)
    gap = min(b - a for a, b in zip(cs, cs[1:]))
    return max(12.0, min(45.0, 0.5 * gap))


def _nearest(models: list, w: Word, tol: float) -> Optional[ColModel]:
    best = min(models, key=lambda m: m.dist(w))
    return best if best.dist(w) <= tol else None


def tokenise(line: Line, layout: Layout, models: list, cfg: Config) -> Cells:
    tol = _tol(models)
    date_col = layout.col("date")
    desc_col = layout.col("desc")
    # horizontal region of the date cell: from just left of the date header to the next column
    d_lo = d_hi = None
    if date_col:
        d_lo = date_col.x0 - 8
        right = [c.x0 for c in layout.cols if c.x0 > date_col.x0 + 1]
        d_hi = (min(right) - 1) if right else float("inf")
    value_col = layout.col("valuedate")

    cells = Cells(line)
    words = line.words
    consumed = set()
    for i, w in enumerate(words):
        mk = is_marker_word(w.text)
        if mk:
            prev = words[i - 1] if i else None
            if prev is not None and w.x0 - prev.x1 < 12 and parse_amount(prev.text, cfg.require_decimals) and i - 1 in consumed:
                # "1,234.56 CR": fold the marker into the amount it follows
                for t in cells.nums:
                    if t.x1 == prev.x1 and t.yc == prev.yc and t.amount.marker is None:
                        t.amount = Amount(t.amount.value, mk)
                consumed.add(i)
                continue
            m = _nearest(models, w, tol)
            if m is not None:
                cells.markers.append((m.role, mk))
                consumed.add(i)
            continue
        a = parse_amount(w.text, cfg.require_decimals)
        if a is not None and models:
            m = _nearest(models, w, tol)
            if m is not None:
                cells.nums.append(NumTok(m.role, a, w.x0, w.x1, w.yc, w.text, i))
                consumed.add(i)
    # one cell per column per line: if a decimal-looking token from the description ("FEE 2.50") competes with the
    # real amount for the same column, the one nearest the column wins and the other goes back to the description
    by_role: dict = {}
    for t in cells.nums:
        by_role.setdefault(t.role, []).append(t)
    for role, toks in by_role.items():
        if len(toks) > 1:
            m = next(m for m in models if m.role == role)
            keep = min(toks, key=lambda t: m.dist(words[t.widx]))
            for t in toks:
                if t is not keep:
                    cells.nums.remove(t)
                    consumed.discard(t.widx)
    rest = [w for i, w in enumerate(words) if i not in consumed]
    date_words, desc_words = [], []
    for w in rest:
        if date_col is not None and d_lo <= w.xc < d_hi and w.x0 < d_hi:
            date_words.append(w)
        elif value_col is not None and value_col.x0 - 8 <= w.xc <= value_col.x1 + 40 and (desc_col is None or w.xc < desc_col.x0 - 3):
            continue
        elif w.text.lower() in CURRENCY_WORDS and any(m.dist(w) <= 60 for m in models):
            continue
        elif desc_col is None or w.xc >= desc_col.x0 - 3:
            desc_words.append(w)
    cells.date_text = " ".join(w.text for w in date_words)
    cells.desc = desc_words
    return cells


def refine_models(models: list, all_cells: list) -> list:
    by_role: dict = {}
    for c in all_cells:
        for t in c.nums:
            by_role.setdefault(t.role, []).append(t)
    out = []
    for m in models:
        toks = by_role.get(m.role, [])
        if len(toks) < 3:
            out.append(m)
            continue
        best = None
        for kind, get in (("x1", lambda t: t.x1), ("xc", lambda t: (t.x0 + t.x1) / 2), ("x0", lambda t: t.x0)):
            vals = [get(t) for t in toks]
            med = statistics.median(vals)
            spread = statistics.median(abs(v - med) for v in vals)
            if best is None or spread < best[0] - 0.25:
                best = (spread, kind, med)
        out.append(ColModel(m.role, m.hx0, m.hx1, best[1], best[2]))
    return out


# ------------------------------------------------------------------------------------------ labels


def _amount_after(line: Line, x_min: float, cfg: Config) -> Optional[Amount]:
    ws = line.words
    for i, w in enumerate(ws):
        if w.x0 < x_min:
            continue
        a = parse_amount(w.text, cfg.require_decimals)
        if a is not None:
            if a.marker is None and i + 1 < len(ws) and is_marker_word(ws[i + 1].text) and ws[i + 1].x0 - w.x1 < 12:
                a = Amount(a.value, is_marker_word(ws[i + 1].text))
            return a
    return None


def _signed(a: Amount) -> Decimal:
    return -a.value if a.marker in ("-", "DR") else a.value


def scan_labels(pages: list, cfg: Config, anchor_ids: set) -> dict:
    """Opening / closing balances and printed totals, from anywhere in the document."""
    out = {"opening": None, "closing": None, "debits": None, "credits": None}
    for page in pages:
        lines = page.lines
        for i, ln in enumerate(lines):
            if id(ln) in anchor_ids:
                continue
            text = ln.text
            kind = "open" if OPEN_RE.search(text) else "close" if CLOSE_RE.search(text) else None
            if kind:
                # value to the right of the label on the same line, else on the next line under the label
                idx = max((k for k, w in enumerate(ln.words) if w.text.lower().strip(":") in ("balance", "forward", "b/f", "c/f")), default=None)
                x_after = ln.words[idx].x0 if idx is not None else ln.words[0].x0
                a = _amount_after(ln, x_after, cfg)
                if a is None and i + 1 < len(lines) and 0 < lines[i + 1].yc - ln.yc < 30:
                    lab_lo, lab_hi = ln.words[0].x0, ln.words[-1].x1
                    nxt = lines[i + 1]
                    a = _amount_after(Line(nxt.page, nxt.yc, [w for w in nxt.words if lab_lo - 60 <= w.xc <= lab_hi + 140]), 0, cfg)
                if a is not None:
                    if kind == "open" and out["opening"] is None:
                        out["opening"] = _signed(a)
                    elif kind == "close":
                        out["closing"] = _signed(a)
                continue
            if TOTAL_DEBIT_RE.search(text) or TOTAL_CREDIT_RE.search(text):
                x_after = ln.words[0].x0
                a = _amount_after(ln, x_after, cfg)
                if a is not None:
                    if TOTAL_DEBIT_RE.search(text):
                        out["debits"] = a.value
                    else:
                        out["credits"] = a.value
    return out


# --------------------------------------------------------------------------------------- segmentation


@dataclass
class RawRow:
    date_text: str
    description: str
    amount: Decimal
    sign_source: str
    balance: Optional[Decimal]
    page: int
    top: float
    notes: list = field(default_factory=list)


def _is_stop(cells: Cells) -> Optional[str]:
    text = cells.line.text
    if CLOSE_RE.search(text):
        return "close"
    if TOTAL_RE.search(text) or TOTAL_DEBIT_RE.search(text) or TOTAL_CREDIT_RE.search(text):
        return "total"
    return None


def segment_page(cells: list, headerless: bool = False):
    """Group a page's table lines into blocks, one per date line. Returns (blocks, orphans).

    On a page with no repeated header, everything above the first date line is page furniture (account box,
    summary boxes); only lines close enough to the first date line are attached to it, and nothing above it
    is reported as an orphan."""
    anchors = [i for i, c in enumerate(cells) if looks_like_date(c.date_text)]
    if not anchors:
        return [], ([] if headerless else [c for c in cells if c.nums])
    yc = [c.line.yc for c in cells]
    gaps = [yc[i + 1] - yc[i] for i in range(len(cells) - 1)]
    between = any(b - a > 1 for a, b in zip(anchors, anchors[1:]))
    multiline = between or (len(cells) > anchors[-1] + 1) or (len(anchors) == 1 and len(cells) > 1)
    intra = 0.0
    gs = gaps[anchors[0]:]        # spacing is learned from the table itself; furniture above the first row would skew it
    if gs and any(g > 0 for g in gs):
        g_min = min(g for g in gs if g > 0)
        small = [g for g in gs if 0 < g <= 1.25 * g_min]
        intra = statistics.median(small) if small else 0.0
    member = {a: [a] for a in anchors}
    if multiline and intra > 0:
        for a1, a2 in zip(anchors, anchors[1:]):
            if a2 - a1 <= 1:
                continue
            s = max(range(a1, a2), key=lambda j: gaps[j])
            if gaps[s] > 1.3 * intra:
                member[a1].extend(range(a1 + 1, s + 1))
                member[a2] = list(range(s + 1, a2)) + member[a2]
            else:
                member[a1].extend(range(a1 + 1, a2))
        j = anchors[0] - 1
        lead = []
        while j >= 0 and gaps[j] <= 1.3 * intra:
            lead.insert(0, j)
            j -= 1
        member[anchors[0]] = lead + member[anchors[0]]
        j = anchors[-1]
        while j + 1 < len(cells) and gaps[j] <= 1.3 * intra:
            member[anchors[-1]].append(j + 1)
            j += 1
    used = {i for v in member.values() for i in v}
    floor = anchors[0] if headerless else 0
    orphans = [cells[i] for i in range(len(cells)) if i not in used and i >= floor and cells[i].nums]
    return [(a, [cells[i] for i in member[a]]) for a in anchors], orphans


def block_to_row(anchor: Cells, lines: list, page: int, cfg: Config) -> Optional[RawRow]:
    nums: dict = {}
    markers: dict = {}
    for c in lines:
        for t in c.nums:
            nums.setdefault(t.role, []).append(t)
        for role, mk in c.markers:
            markers.setdefault(role, mk)
    notes = []
    for role, toks in nums.items():
        if len(toks) > 1:
            notes.append(f"{len(toks)} numbers in {role} column")
    first = lambda r: nums[r][0] if nums.get(r) else None
    deb, cre, amt, bal = first("debit"), first("credit"), first("amount"), first("balance")
    if deb is None and cre is None and amt is None:
        return None
    if deb is not None and cre is not None:
        amount, src = cre.amount.value - deb.amount.value, "column"
        notes.append("both debit and credit present")
    elif deb is not None:
        amount, src = -deb.amount.value, "column"
    elif cre is not None:
        amount, src = cre.amount.value, "column"
    else:
        mk = amt.amount.marker or markers.get("amount")
        if mk in ("-", "DR"):
            amount, src = -amt.amount.value, "marker"
        elif mk == "CR":
            amount, src = amt.amount.value, "marker"
        else:
            amount, src = amt.amount.value, "unresolved"
    balance = None
    if bal is not None:
        mk = bal.amount.marker or markers.get("balance")
        balance = -bal.amount.value if mk in ("-", "DR") else bal.amount.value
    desc = " ".join(c.desc_text for c in sorted(lines, key=lambda c: c.line.yc) if c.desc_text)
    desc = re.sub(r"\s+", " ", desc).strip()
    return RawRow(anchor.date_text.strip(), desc, amount, src, balance, page, anchor.line.yc, notes)


def extract_rows(pages: list, layout: Layout, start: tuple, cfg: Config, models: list):
    """Run tokenisation + segmentation over every page. Returns (rows, all_cells, anchor_ids, orphans, dropped).

    dropped = dated lines inside the table that yielded no amount. They must be reported: a row that silently
    vanishes is the one failure a balance chain cannot see when it is the first or last row."""
    rows: list = []
    dropped: list = []
    all_cells: list = []
    anchor_ids: set = set()
    orphans: list = []
    start_page, start_line = start
    for pi, page in enumerate(pages):
        lines = page.lines
        cells: list = []
        i = start_line if pi == start_page else 0
        if pi < start_page:
            continue
        has_header = pi == start_page
        while i < len(lines):
            n = is_header_line(lines, i)
            if n:
                has_header = True
                cells = []          # everything above a (repeated) header is page furniture
                i += n
                continue
            cells.append(tokenise(lines[i], layout, models, cfg))
            i += 1
        all_cells.extend(cells)
        # cut the table at the first closing / total line AFTER the first date line on the page
        first = next((k for k, c in enumerate(cells) if looks_like_date(c.date_text)), None)
        region = []
        for k, c in enumerate(cells):
            if first is not None and k > first and not looks_like_date(c.date_text) and _is_stop(c):
                break
            region.append(c)
        for c in region:
            if not looks_like_date(c.date_text) and (OPEN_RE.search(c.line.text) or CLOSE_RE.search(c.line.text)):
                c.nums, c.desc, c.markers = [], [], []   # a label line, not part of any transaction (scan_labels reads it)
        blocks, orph = segment_page(region, headerless=not has_header)
        orphans.extend((page.number, o) for o in orph)
        for a, members in blocks:
            anchor = region[a]
            anchor_ids.add(id(anchor.line))
            desc = anchor.desc_text
            if OPEN_RE.search(desc) or CLOSE_RE.search(desc):
                continue
            r = block_to_row(anchor, members, page.number, cfg)
            if r is not None:
                rows.append(r)
            else:
                dropped.append((page.number, anchor.date_text.strip(), desc[:60]))
    return rows, all_cells, anchor_ids, orphans, dropped
