"""Benchmark on the synthetic corpus.  python bench/run_bench.py [N] [--first SEED] [--dir DIR]

For each seed: generate a PDF with exact ground truth, extract, compare. Reports exact-statement rate and, more
importantly, how many WRONG statements were accepted (silent errors) versus sent to review.
"""
import argparse, json, os, sys, tempfile, time
from collections import Counter
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from stmtflow import extract
from stmtflow.evaluate import compare
from stmtflow.synth import generate_corpus

ap = argparse.ArgumentParser()
ap.add_argument("n", nargs="?", type=int, default=200)
ap.add_argument("--first", type=int, default=1000)
ap.add_argument("--dir", default=None)
ap.add_argument("--verbose", action="store_true")
a = ap.parse_args()
d = a.dir or tempfile.mkdtemp(prefix="stmtflow_bench_")
seeds = range(a.first, a.first + a.n)
t0 = time.time()
paths = generate_corpus(d, seeds)
tg = time.time() - t0
t0 = time.time()
tally = Counter(); silent = []; rev_bad = 0; rows = ok_rows = 0
for p in paths:
    truth = json.load(open(p.replace(".pdf", ".truth.json")))
    res = extract(p).to_json()
    c = compare(res, truth)
    acc = res["status"].startswith("accepted")
    exact = c["statement_exact"]
    tally[(res["status"], "exact" if exact else "wrong")] += 1
    rows += c["rows_expected"]; ok_rows += c["rows_exact"]
    if acc and not c["statement_money_exact"]:
        silent.append(os.path.basename(p))
    if a.verbose and not exact:
        print(os.path.basename(p), res["status"], c)
te = time.time() - t0
print(f"corpus {len(paths)} statements, {rows} rows (generated in {tg:.1f}s, extracted in {te:.1f}s)")
for k, v in sorted(tally.items()):
    print(f"  {k[0]:<22} {k[1]:<6} {v}")
accepted = sum(v for k, v in tally.items() if k[0].startswith("accepted"))
print(f"accepted {accepted}/{len(paths)}; SILENT ERRORS (accepted but money wrong): {len(silent)} {silent[:10]}")
print(f"row-exact overall {ok_rows}/{rows} = {ok_rows/rows:.4f}")
