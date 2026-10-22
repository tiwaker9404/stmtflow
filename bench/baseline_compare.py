"""Run the original MIT project (bank_statement_extract.py, vendored for comparison) on the synthetic corpus.

Fair-play notes: the baseline is run with its DEFAULT settings, because that is how a new layout arrives in a
pipeline (nobody has tuned it yet). It exposes --amount-columns for hand-tuning per layout; that tuning is exactly
the per-layout manual work stmtflow removes. "Row correct" here = right amount (signed from deposit/withdrawal) and
right balance, compared in order. Dates and descriptions are not scored for either tool in this table.

python bench/baseline_compare.py CORPUS_DIR
"""
import csv, glob, json, os, subprocess, sys, tempfile
from decimal import Decimal, InvalidOperation
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import stmtflow


def D(x):
    try:
        return Decimal(str(x).replace(",", "").strip())
    except InvalidOperation:
        return None


def base_rows(pdf):
    out = os.path.join(tempfile.mkdtemp(), "o.csv")
    subprocess.run([sys.executable, os.path.join(HERE, "bank_statement_extract.py"), pdf, "--csv", out],
                   capture_output=True, timeout=120)
    if not os.path.exists(out):
        return []
    rows = []
    for r in csv.DictReader(open(out, encoding="utf-8")):
        dep, wd = D(r.get("deposit") or ""), D(r.get("withdrawal") or "")
        amt = (dep or Decimal(0)) - (wd or Decimal(0)) if (dep or wd) else None
        rows.append((amt, D(r.get("balance") or "")))
    return rows


def score(rows, truth):
    exp = [(D(t["amount"]), D(t["balance"])) for t in truth["transactions"]]
    ok = sum(1 for g, e in zip(rows, exp) if g[0] is not None and g[0] == e[0] and g[1] == e[1])
    return ok, len(exp), len(rows) == len(exp) and ok == len(exp)


d = sys.argv[1]
IN_SCOPE_ONLY = "--in-scope" in sys.argv   # dd-mm-yyyy dates + separate debit/credit columns: the shape of the baseline's own samples
pdfs = sorted(glob.glob(os.path.join(d, "stmt_*.pdf")))
tot = {"base": [0, 0, 0, 0, 0], "ours": [0, 0, 0, 0, 0]}  # rows_ok, rows, stmts_exact, stmts_returned_but_wrong, stmts
for p in pdfs:
    truth = json.load(open(p.replace(".pdf", ".truth.json")))
    if truth["style"]["no_balance"]:
        continue
    if IN_SCOPE_ONLY and not (truth["style"]["date_fmt"] == "%d-%m-%Y" and truth["style"]["mode"] == "split"):
        continue  # baseline needs a balance column; keep the comparison to statements it could in principle read
    b_ok, n, b_ex = score(base_rows(p), truth)
    r = stmtflow.extract(p).to_json()
    o_rows = [(D(t["amount"]), D(t["balance"])) for t in r["transactions"]]
    o_ok, _, o_ex = score(o_rows, truth)
    o_accepted = r["status"].startswith("accepted")
    for k, ok, ex, returned in (("base", b_ok, b_ex, True), ("ours", o_ok, o_ex, o_accepted)):
        t = tot[k]; t[0] += ok; t[1] += n; t[2] += ex; t[4] += 1
        t[3] += (returned and not ex)
print(f"{'':10}{'rows right':>14}{'stmts exact':>14}{'delivered wrong':>18}")
for k, name in (("base", "baseline"), ("ours", "stmtflow")):
    t = tot[k]
    print(f"{name:10}{t[0]:>7}/{t[1]:<6}{t[2]:>8}/{t[4]:<5}{t[3]:>12}")
print("delivered wrong = statements handed to the user as results although some row is wrong "
      "(baseline has no verification, so everything it returns counts as delivered; stmtflow counts only 'accepted')")
