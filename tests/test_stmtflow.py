"""pytest tests/ -q   (needs reportlab, pdfplumber, pytest)"""
import json
import os
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import stmtflow
from stmtflow.amounts import parse_amount, parse_date, looks_like_date
from stmtflow.evaluate import compare
from stmtflow.models import Config
from stmtflow.synth import generate_corpus

FIX = Path(__file__).parent / "fixtures" / "upstream"


# ------------------------------------------------------------------ amounts and dates
@pytest.mark.parametrize("text,value,marker", [
    ("1,234.56", "1234.56", None), ("(1,234.56)", "1234.56", "-"), ("-1,234.56", "1234.56", "-"),
    ("1,234.56-", "1234.56", "-"), ("$1,234.56", "1234.56", None), ("1,23,456.78", "123456.78", None),
])
def test_amount_forms(text, value, marker):
    a = parse_amount(text)
    assert a is not None and a.value == Decimal(value) and a.marker == marker


@pytest.mark.parametrize("text", ["2025", "10234", "UTR", "12/05/2025", "1043"])
def test_ids_and_dates_are_not_money(text):
    assert parse_amount(text) is None     # years, check numbers, references must never become amounts


def test_dates():
    assert parse_date("03-01-2026", "dmy").isoformat() == "2026-01-03"
    assert parse_date("03-01-2026", "mdy").isoformat() == "2026-03-01"
    assert parse_date("08-Jul-25").isoformat() == "2025-07-08"
    assert looks_like_date("13 Mar 2025") and not looks_like_date("PAYROLL ACME")


# ------------------------------------------------------------------ real-shaped fixtures from the upstream project
@pytest.mark.parametrize("name", ["astra-premier-checking-jan-2026", "meridian-salary-account-feb-2026",
                                  "northstar-business-current-mar-2026"])
def test_upstream_samples_match_their_published_answers(name):
    st = stmtflow.extract(str(FIX / f"{name}.pdf")).to_json()
    truth = json.load(open(FIX / f"{name}.json"))
    assert st["status"] == "accepted" and st["verification"]["verified_by"]
    assert len(st["transactions"]) == len(truth)
    for got, exp in zip(st["transactions"], truth):
        dep, wd = Decimal(exp["deposit"] or 0), Decimal(exp["withdrawal"] or 0)
        assert Decimal(got["amount"]) == dep - wd
        assert Decimal(got["balance"]) == Decimal(exp["balance"])


# ------------------------------------------------------------------ synthetic corpus: the core safety property
@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    d = tmp_path_factory.mktemp("corpus")
    return generate_corpus(str(d), range(9000, 9040))


def test_accepted_means_correct(corpus):
    """The property that matters: no statement is accepted with a wrong amount, sign, date or balance."""
    accepted = 0
    for p in corpus:
        truth = json.load(open(p.replace(".pdf", ".truth.json")))
        r = stmtflow.extract(p).to_json()
        c = compare(r, truth)
        if r["status"] == "accepted":
            accepted += 1
            assert c["statement_money_exact"], f"{os.path.basename(p)} accepted with wrong money: {c}"
    assert accepted >= 30      # and it is not trivially rejecting everything


def _tamper(src, dst, nth):
    """Change one digit of the nth printed money value in the page text (what a mis-read or a bad OCR would do)."""
    import re
    import pypdf
    from pypdf.generic import DecodedStreamObject, NameObject
    w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(src))
    page = w.pages[0]
    data = page.get_contents().get_data()
    ms = [m for m in re.finditer(rb"\(([^()]*\d[\d,]*\.\d\d[^()]*)\)\s*Tj", data)
          if re.search(rb"(?<![\d.])\d[\d,]*\.\d\d(?![\d.])", m.group(1))]
    m = ms[nth % len(ms)]
    txt = m.group(1)
    esc = re.match(rb"\\\d{3}", txt)                  # an octal escape such as \243 (a currency sign) is not a digit to flip
    tok = re.search(rb"(?<![\d.])\d[\d,]*\.\d\d(?![\d.])", txt[esc.end():] if esc else txt)   # money, not a 17.08.2025 date
    i = tok.start() + (esc.end() if esc else 0)
    new = txt[:i] + (b"1" if txt[i:i + 1] != b"1" else b"2") + txt[i + 1:]
    s = DecodedStreamObject(); s.set_data(data[:m.start(1)] + new + data[m.end(1):])
    page[NameObject("/Contents")] = w._add_object(s)
    w.write(dst)


def test_tampered_values_never_accepted(tmp_path, corpus):
    """(Dates are out of scope here: a statement has no arithmetic redundancy for dates, see README limits.)
    Alter one printed money value in statements that were accepted. Each altered file must NOT be accepted
    unchanged: the balance chain / opening-closing identity must notice."""
    tested = silent = 0
    for p in corpus:
        r0 = stmtflow.extract(p)
        if r0.status != "accepted":
            continue
        for nth in (7, 23):
            dst = str(tmp_path / f"t_{nth}_{os.path.basename(p)}")
            _tamper(p, dst, nth)
            r1 = stmtflow.extract(dst)
            key = lambda r: [(t.date, t.amount) for t in r.txns]
            if key(r1) == key(r0):
                continue          # the edit did not change any extracted amount or date (balance cell, header box, ...)
            tested += 1
            flagged = any(f.code == "first_row_amount_unverified" for f in r1.findings)   # known blind spot, reported in the output
            silent += r1.status == "accepted" and not flagged
        if tested >= 40:
            break
    assert tested >= 20
    assert silent == 0, f"{silent} of {tested} tampered statements were accepted"


# ------------------------------------------------------------------ failure handling
def test_corrupt_pdf_fails_cleanly(tmp_path):
    p = tmp_path / "junk.pdf"
    p.write_bytes(b"%PDF-1.4 not really a pdf")
    st = stmtflow.extract(str(p)).to_json()
    assert st["status"] == "failed" and st["findings"]


def test_empty_text_layer_is_reported_as_scanned(tmp_path):
    p = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(p)); c.rect(50, 50, 400, 400); c.showPage(); c.save()
    st = stmtflow.extract(str(p)).to_json()
    assert st["status"] == "failed" and any(f["code"] == "no_text_layer" for f in st["findings"])


def test_no_balance_statement_goes_to_review_unless_allowed(tmp_path):
    ps = generate_corpus(str(tmp_path), [1], no_balance=True, opening="none", closing="none", totals=False)
    assert stmtflow.extract(ps[0]).status == "review"
    assert stmtflow.extract(ps[0], Config(allow_unverified=True)).status == "accepted_unverified"


# ------------------------------------------------------------------ batch behaviour
def test_batch_cache_and_isolation(tmp_path, corpus):
    d = tmp_path / "in"; d.mkdir()
    for p in corpus[:6]:
        shutil.copy(p, d)
    (d / "broken.pdf").write_bytes(b"garbage")
    out = tmp_path / "out"
    s1 = stmtflow.process_batch([str(d)], str(out), workers=2)
    assert s1["files"] == 7 and s1["by_status"].get("failed") == 1      # one bad file does not stop the run
    s2 = stmtflow.process_batch([str(d)], str(out), workers=2)
    assert s2["cached"] == 7                                            # second run does no extraction
    assert (out / "layouts.json").exists() and (out / "run_log.jsonl").exists()
