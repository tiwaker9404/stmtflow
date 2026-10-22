"""Fault injection: change one digit of one printed money value in each ACCEPTED statement and re-extract.
A tool that is safe to automate must not accept the altered statement. python bench/tamper_bench.py CORPUS_DIR"""
import glob, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..")); sys.path.insert(0, os.path.join(HERE, "..", "tests"))
import stmtflow
from test_stmtflow import _tamper

tested = caught = silent = flagged = 0
tmp = tempfile.mkdtemp()
key = lambda r: [(t.date, t.amount) for t in r.txns]
for p in sorted(glob.glob(os.path.join(sys.argv[1], "stmt_*.pdf"))):
    r0 = stmtflow.extract(p)
    if r0.status != "accepted":
        continue
    for nth in (3, 11, 29, 47):
        dst = os.path.join(tmp, f"t{nth}.pdf")
        try:
            _tamper(p, dst, nth)
        except Exception:
            continue
        r1 = stmtflow.extract(dst)
        if key(r1) == key(r0):
            continue          # edit did not change any amount/date that was extracted
        tested += 1
        if r1.status != "accepted":
            caught += 1
        elif any(f.code == "first_row_amount_unverified" for f in r1.findings):
            flagged += 1          # accepted, but the file says in plain words that this row has no independent check
        else:
            silent += 1; print("ACCEPTED AFTER TAMPER:", os.path.basename(p), nth)
print(f"tampered statements: {tested}   sent to review: {caught}   accepted but flagged first-row-unverified: {flagged}   silently accepted: {silent}")
