"""Synthetic statement generator with exact ground truth.

Every statement is drawn from a seeded random style: column mode, column order, header wording, date format,
number alignment, font, multi-line descriptions (date line at the top or vertically centred), page breaks with or
without a repeated header, newest-first ordering, opening/closing placement, printed totals, watermarks, and
decimal-looking numbers inside descriptions. This lets the extractor be measured on cases where the right answer is
known exactly, and lets us break it on purpose. It is a regression and stress corpus, NOT a substitute for real
bank PDFs: layouts are my guess at what banks do.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4, LETTER
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

MERCHANTS = [
    "AMAZON MKTP US", "WHOLE FOODS MKT 10234", "SHELL OIL 57444", "UBER TRIP HELP.UBER.COM", "NETFLIX.COM",
    "STARBUCKS STORE 11922", "TRADER JOES #552", "CVS PHARMACY 04411", "PAYROLL ACME CORP", "ZELLE FROM J SMITH",
    "ACH WITHDRAWAL RENT APT 4B", "ELECTRIC CO AUTOPAY", "ATM WITHDRAWAL MAIN ST", "INTEREST PAID", "MONTHLY SERVICE FEE",
    "WIRE TRANSFER IN", "CHECK 1043", "VENMO CASHOUT", "GROCERY OUTLET", "LYFT RIDE", "UPI/DR/SWIGGY BANGALORE",
    "NEFT CR SALARY", "IMPS/DR/ZOMATO", "POS PURCHASE MEDIPLUS", "SIP/DR/FLEXI CAP FUND", "BILLPAY WATER UTILITY",
]
EXTRAS = ["", "", "", " REF {n7}", " UTR {n12}", " TXN#{n9}", " FEE 2.50", " FX RATE 83.25", " AUTH {n6}", " CARD x{n4}"]

HEADERS_SPLIT = [
    ("Date", "Description", "Withdrawals", "Deposits", "Balance"),
    ("Txn Date", "Narration", "Debit", "Credit", "Balance"),
    ("Date", "Particulars", "Debits", "Credits", "Balance"),
    ("Posting Date", "Transaction Details", "Money Out", "Money In", "Balance"),
    ("Date", "Description", "Dr", "Cr", "Balance"),
]
HEADERS_AMOUNT = [
    ("Date", "Description", "Amount", "Balance"),
    ("Date", "Details", "Amt", "Balance"),
    ("Transaction Date", "Narration", "Amount (USD)", "Running Balance"),
]
DATE_FORMATS = ["%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%d-%b-%y", "%Y-%m-%d", "%b %d, %Y", "%d.%m.%Y"]
MODES = ["split", "split", "split", "signed", "cr_dr", "paren", "trail", "unsigned"]


@dataclass
class Style:
    mode: str = "split"
    credit_first: bool = False
    header_idx: int = 0
    date_fmt: str = "%d-%m-%Y"
    num_align: str = "right"
    font: str = "Helvetica"
    size: float = 9.0
    letter: bool = False
    currency: str = ""
    n_rows: int = 40
    multiline: float = 0.3
    centered: bool = False
    repeat_header: bool = True
    reverse: bool = False
    opening: str = "table"        # table | summary | none
    closing: str = "table"        # table | summary | none
    totals: bool = False
    watermark: str = "none"       # none | big | rotated
    no_balance: bool = False
    pad: float = 5.0
    col_w: tuple = (72, 200, 76)


def sample_style(seed: int, **over) -> Style:
    r = random.Random(seed)
    mode = r.choice(MODES)
    st = Style(
        mode=mode,
        credit_first=r.random() < 0.3,
        header_idx=r.randrange(len(HEADERS_SPLIT if mode == "split" else HEADERS_AMOUNT)),
        date_fmt=r.choice(DATE_FORMATS),
        num_align=r.choice(["right", "right", "right", "left", "center"]),
        font=r.choice(["Helvetica", "Helvetica", "Times-Roman", "Courier"]),
        size=r.choice([8, 8.5, 9, 10]),
        letter=r.random() < 0.4,
        currency=r.choice(["", "", "$", "£"]),
        n_rows=r.choice([12, 25, 40, 60, 90]),
        multiline=r.choice([0, 0, 0.3, 0.6]),
        centered=r.random() < 0.35,
        repeat_header=r.random() < 0.6,
        reverse=r.random() < 0.2,
        opening=r.choice(["table", "summary", "summary", "none"]),
        closing=r.choice(["table", "summary", "none"]),
        totals=r.random() < 0.3,
        watermark=r.choice(["none", "none", "none", "big", "rotated"]),
        no_balance=r.random() < 0.06,
        pad=r.choice([3, 5, 8]),
        col_w=(r.choice([66, 72, 80]), r.choice([180, 200, 215]), r.choice([70, 76, 82])),
    )
    for k, v in over.items():
        setattr(st, k, v)
    if st.font == "Courier":
        st.size = min(st.size, 8.5)
        st.col_w = (st.col_w[0] + 10, st.col_w[1], st.col_w[2])
    return st


def _money(v: Decimal) -> str:
    return f"{v:,.2f}"


def _make_rows(r: random.Random, st: Style):
    opening = Decimal(r.randrange(200000, 3000000)) / 100
    d = date(2025, r.randrange(1, 12), r.randrange(1, 20))
    bal = opening
    rows = []
    for _ in range(st.n_rows):
        d += timedelta(days=r.choice([0, 0, 1, 1, 2, 3]))
        amt = Decimal(r.randrange(250, 250000)) / 100
        if r.random() < 0.55:
            amt = -amt
        if bal + amt < 0:
            amt = abs(amt)
        bal += amt
        n = lambda k: "".join(r.choice("0123456789") for _ in range(k))
        extra = r.choice(EXTRAS).format(n4=n(4), n6=n(6), n7=n(7), n9=n(9), n12=n(12))
        rows.append({"date": d, "description": r.choice(MERCHANTS) + extra, "amount": amt, "balance": bal})
    return opening, bal, rows


def _wrap(text: str, width: float, font: str, size: float) -> list:
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if stringWidth(t, font, size) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + [cur] if cur else lines


def make_statement(seed: int, path: str, **over) -> dict:
    """Draw one statement PDF at `path`. Returns the ground truth in PRINTED order."""
    r = random.Random(seed * 7919 + 13)
    st = sample_style(seed, **over)
    opening, closing, rows = _make_rows(r, st)
    printed = rows[::-1] if st.reverse else rows
    size, font = st.size, st.font
    pw, ph = LETTER if st.letter else A4
    margin, line_h = 30.0, size * 1.35
    dw, descw, nw = st.col_w
    hdr = (HEADERS_SPLIT if st.mode == "split" else HEADERS_AMOUNT)[st.header_idx]
    bold = lambda t: stringWidth(t, "Helvetica-Bold", size)
    dw = max(dw, bold(hdr[0]) + 12)                      # headers must not overrun their column
    nw = max(nw, max(bold(h) for h in hdr[2:]) + 12)

    # column boxes
    x = margin
    boxes = {}
    boxes["date"] = (x, x + dw); x += dw
    boxes["desc"] = (x, x + descw); x += descw
    if st.mode == "split":
        order = ["credit", "debit"] if st.credit_first else ["debit", "credit"]
    else:
        order = ["amount"]
    for role in order:
        boxes[role] = (x, x + nw); x += nw
    if not st.no_balance:
        boxes["balance"] = (x, x + nw)
    names = {"date": hdr[0], "desc": hdr[1]}
    if st.mode == "split":
        names["debit"], names["credit"], names["balance"] = hdr[2], hdr[3], hdr[4]
    else:
        names["amount"], names["balance"] = hdr[2], hdr[3]

    c = canvas.Canvas(path, pagesize=(pw, ph))
    c.setTitle(f"synthetic statement {seed}")
    top_y = ph - 50.0
    bottom = 60.0
    page = {"n": 1}
    pages_pdf = []

    def num_x(role, text):
        x0, x1 = boxes[role]
        return {"right": x1 - 4, "left": x0 + 4, "center": (x0 + x1) / 2}[st.num_align]

    def put_num(role, text, y):
        c.setFont(font, size)
        px = num_x(role, text)
        {"right": c.drawRightString, "left": c.drawString, "center": c.drawCentredString}[st.num_align](px, y, text)

    def furniture():
        c.setFont("Helvetica-Bold", 13)
        c.drawString(margin, ph - 30, "Synthetic Bank")
        c.setFont("Helvetica", 8)
        c.drawString(margin, ph - 42, f"Statement period 01-01-2025 to 31-12-2025   Account XXXXXX{seed % 10000:04d}")

    def footer(n_pages_hint="?"):
        c.setFont("Helvetica", 7.5)
        c.drawString(margin, 36, "This is a computer generated statement. Generated on 05-02-2026.")
        c.drawCentredString(pw / 2, 24, f"Page {page['n']}")

    def draw_header(y):
        c.setFont("Helvetica-Bold", size)
        for role, (x0, x1) in boxes.items():
            if role in ("date", "desc"):
                c.drawString(x0 + 4, y, names[role])
            else:
                px = num_x(role, "")
                {"right": c.drawRightString, "left": c.drawString, "center": c.drawCentredString}[st.num_align](px, y, names[role])
        return y - line_h * 1.8

    def watermark():
        if st.watermark == "big":
            c.saveState(); c.setFillGray(0.85); c.setFont("Helvetica-Bold", size * 5)
            c.drawString(pw * 0.55, ph * 0.45, "COPY"); c.restoreState()
        elif st.watermark == "rotated":
            c.saveState(); c.setFillGray(0.85); c.setFont("Helvetica-Bold", 60); c.translate(pw * 0.3, ph * 0.3)
            c.rotate(45); c.drawString(0, 0, "SAMPLE"); c.restoreState()

    def amount_cells(amt):
        mag = _money(abs(amt))
        cur = st.currency
        if st.mode == "split":
            return {"debit": cur + mag} if amt < 0 else {"credit": cur + mag}
        if st.mode == "signed":
            return {"amount": ("-" if amt < 0 else "") + cur + mag}
        if st.mode == "cr_dr":
            return {"amount": f"{cur}{mag} {'DR' if amt < 0 else 'CR'}"}
        if st.mode == "paren":
            return {"amount": f"({cur}{mag})" if amt < 0 else cur + mag}
        if st.mode == "trail":
            return {"amount": f"{cur}{mag}-" if amt < 0 else cur + mag}
        return {"amount": cur + mag}

    def label_row(text, value, y):
        c.setFont(font, size)
        c.drawString(boxes["desc"][0] + 4, y, text)
        if "balance" in boxes:
            put_num("balance", st.currency + _money(value), y)
        else:
            put_num(order[0], st.currency + _money(value), y)

    furniture(); watermark()
    y = top_y
    if st.opening == "summary" or st.closing == "summary":
        c.setFont(font, size)
        xs = pw - margin - 200
        if st.opening == "summary":
            c.drawString(xs, y, "OPENING BALANCE"); c.drawString(xs + 100, y, st.currency + _money(opening)); y -= line_h
        if st.closing == "summary":
            c.drawString(xs, y, "CLOSING BALANCE"); c.drawString(xs + 100, y, st.currency + _money(closing)); y -= line_h
        y -= line_h
    y = draw_header(y - line_h)
    if st.opening == "table" and not st.no_balance:
        label_row("Opening Balance", opening, y); y -= line_h + st.pad

    for row in printed:
        desc = row["description"]
        lines = _wrap(desc, descw - 10, font, size) or [desc]
        if len(lines) == 1 and r.random() < st.multiline and len(desc.split()) >= 3:
            ws = desc.split()
            cut = len(ws) // 2
            lines = [" ".join(ws[:cut]), " ".join(ws[cut:])]
        h = len(lines) * line_h + st.pad
        if y - h < bottom:
            footer(); c.showPage(); page["n"] += 1; furniture(); watermark()
            y = top_y
            if st.repeat_header:
                y = draw_header(y - line_h)
            else:
                c.setFont("Helvetica", 8); c.drawString(margin, y, "Statement continued"); y -= line_h * 2
        k = (len(lines) - 1) // 2 if st.centered else 0
        yy = y
        for i, ln in enumerate(lines):
            c.setFont(font, size)
            c.drawString(boxes["desc"][0] + 4, yy - i * line_h, ln)
        ya = yy - k * line_h
        c.setFont(font, size)
        c.drawString(boxes["date"][0] + 4, ya, row["date"].strftime(st.date_fmt))
        for role, text in amount_cells(row["amount"]).items():
            put_num(role, text, ya)
        if "balance" in boxes:
            put_num("balance", st.currency + _money(row["balance"]) + (" CR" if st.mode == "cr_dr" else ""), ya)
        y -= h

    if st.closing == "table" and not st.no_balance:
        if y - line_h * 3 < bottom:
            footer(); c.showPage(); page["n"] += 1; furniture(); y = top_y
        y -= line_h * 0.5
        label_row("Closing Balance", closing, y); y -= line_h
    debits = sum((-x["amount"] for x in rows if x["amount"] < 0), Decimal("0"))
    credits = sum((x["amount"] for x in rows if x["amount"] > 0), Decimal("0"))
    if st.totals:
        if y - line_h * 3 < bottom:
            footer(); c.showPage(); page["n"] += 1; furniture(); y = top_y
        c.setFont(font, size)
        c.drawString(boxes["desc"][0] + 4, y - line_h, f"Total Debits: {_money(debits)}")
        c.drawString(boxes["desc"][0] + 4, y - 2 * line_h, f"Total Credits: {_money(credits)}")
    footer()
    c.showPage()
    c.save()

    total_pages = page["n"]

    verifiable = (not st.no_balance) or (st.totals and st.mode in ("split", "signed", "cr_dr", "paren", "trail"))
    truth = {
        "seed": seed,
        "style": asdict(st),
        "opening": str(opening) if st.opening != "none" and not st.no_balance else None,
        "closing": str(closing) if st.closing != "none" and not st.no_balance else None,
        "pages": total_pages,
        "expect_verifiable": verifiable,
        "transactions": [
            {"date": x["date"].isoformat(), "description": x["description"], "amount": str(x["amount"]),
             "balance": None if st.no_balance else str(x["balance"])} for x in printed
        ],
    }
    return truth


def generate_corpus(out_dir: str, seeds, **over) -> list:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for s in seeds:
        p = out / f"stmt_{s:05d}.pdf"
        truth = make_statement(s, str(p), **over)
        (out / f"stmt_{s:05d}.truth.json").write_text(json.dumps(truth, indent=1))
        paths.append(str(p))
    return paths
