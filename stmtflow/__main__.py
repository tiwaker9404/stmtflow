"""Command line: python -m stmtflow extract|batch|synth|bench ..."""
from __future__ import annotations

import argparse
import json
import sys

from .models import Config
from .pipeline import extract, process_batch


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stmtflow", description="Self-verifying extraction from digital PDF bank statements")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="one PDF -> JSON on stdout (exit 0 accepted, 3 review, 4 failed)")
    e.add_argument("pdf")
    e.add_argument("--allow-unverified", action="store_true", help="accept statements with nothing to check against")

    b = sub.add_parser("batch", help="many PDFs -> accepted/ review/ failed/ folders, run log, layout report")
    b.add_argument("inputs", nargs="+", help="PDF files or folders (searched recursively)")
    b.add_argument("-o", "--out", required=True)
    b.add_argument("-w", "--workers", type=int, default=2)
    b.add_argument("--csv", action="store_true", help="also write a CSV next to each accepted JSON")
    b.add_argument("--no-cache", action="store_true")
    b.add_argument("--allow-unverified", action="store_true")

    s = sub.add_parser("synth", help="generate N synthetic statements with exact ground truth")
    s.add_argument("out")
    s.add_argument("-n", type=int, default=50)
    s.add_argument("--first", type=int, default=1)

    a = ap.parse_args(argv)
    if a.cmd == "extract":
        st = extract(a.pdf, Config(allow_unverified=a.allow_unverified))
        print(json.dumps(st.to_json(), indent=2, ensure_ascii=False))
        return {"accepted": 0, "accepted_unverified": 0, "review": 3}.get(st.status, 4)
    if a.cmd == "batch":
        def prog(i, n, r):
            print(f"[{i}/{n}] {r['file']}: {r['status']}", file=sys.stderr)
        summary = process_batch(a.inputs, a.out, Config(allow_unverified=a.allow_unverified), workers=a.workers,
                                csv_out=a.csv, use_cache=not a.no_cache, progress=prog)
        print(json.dumps({k: v for k, v in summary.items() if k != "layouts"}, indent=2))
        return 0
    if a.cmd == "synth":
        from .synth import generate_corpus
        paths = generate_corpus(a.out, range(a.first, a.first + a.n))
        print(f"wrote {len(paths)} PDFs with .truth.json files to {a.out}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
