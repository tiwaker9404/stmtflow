"""Show row-level differences for given seeds in a bench dir: python bench/diffone.py DIR SEED..."""
import sys, json, os
from decimal import Decimal
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from stmtflow import extract
D = sys.argv[1]
for n in map(int, sys.argv[2:]):
    p = os.path.join(D, f"stmt_{n:05d}.pdf"); t = json.load(open(p.replace(".pdf", ".truth.json")))
    st = extract(p).to_json()
    s = t["style"]
    print("=== ", n, st["status"], [(f["code"]) for f in st.get("findings", [])][:4], {k: s[k] for k in ("mode","header_idx","multiline","centered","opening","closing","reverse","totals","watermark")})
    for i, (g, e) in enumerate(zip(st["transactions"], t["transactions"])):
        D_ = lambda v: None if v is None else Decimal(v)
        if (g["date"], D_(g["amount"]), D_(g["balance"]), " ".join(g["description"].split())) != (e["date"], D_(e["amount"]), D_(e["balance"]), " ".join(e["description"].split())):
            print(" row", i, "GOT ", g["date"], g["amount"], g["balance"], g["sign_source"], repr(g["description"]))
            print("       WANT", e["date"], e["amount"], e["balance"], repr(e["description"]))
