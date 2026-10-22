"""Extraction orchestration and the batch runner.

extract()        one PDF -> Statement. Pure function of (file bytes, config, priors): no hidden state.
process_batch()  many PDFs -> accepted/ review/ failed/ folders, a run log, a per-layout health report.

Scale features: content-hash cache (re-running a folder costs nothing), process pool, one-file-per-failure
isolation, per-file deadline, atomic writes, and a layout registry that makes new or drifting layouts visible.
"""
from __future__ import annotations

import csv
import json
import os
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .amounts import infer_order, looks_like_date, parse_date
from .chain import run_chain
from .extract import extract_rows, initial_models, refine_models, scan_labels, tokenise
from .layout import find_layout
from .models import ACCEPTED, ACCEPTED_UNVERIFIED, FAILED, PARSER_VERSION, REVIEW, Config, Statement, Txn
from .reader import read_pages, sha256_file, word_count
from .validate import validate


def extract(path: str, cfg: Optional[Config] = None, priors: Optional[dict] = None) -> Statement:
    cfg = cfg or Config()
    priors = priors or {}
    t0 = time.monotonic()
    st = Statement(file=os.path.basename(path), sha256=sha256_file(path))
    try:
        pages = read_pages(path, cfg)
    except TimeoutError as e:
        st.add("timeout", "error", str(e))
        st.status = FAILED
        return st
    except Exception as e:  # corrupt / encrypted / not a PDF
        st.add("unreadable", "error", f"{type(e).__name__}: {e}")
        st.status = FAILED
        return st

    st.stats = {"pages": len(pages), "words": word_count(pages), "dropped_glyphs": sum(p.dropped for p in pages)}
    if st.stats["words"] < 10 * max(1, len(pages)):
        st.add("no_text_layer", "error", "almost no text found: scanned PDF? OCR is out of scope for this extractor")
        st.status = FAILED
        return st

    found = find_layout(pages)
    if not found:
        st.add("no_table_header", "error", "no transaction table header (date + balance/amount columns) found")
        st.status = FAILED
        return st
    layout, pi, li, n = found
    st.fingerprint = layout.fingerprint()
    st.roles = layout.roles

    models = initial_models(layout)
    start = (pi, li + n)
    _, cells1, _, _, _ = extract_rows(pages, layout, start, cfg, models)
    models = refine_models(models, cells1)
    raw, cells, anchor_ids, orphans, dropped = extract_rows(pages, layout, start, cfg, models)

    # day-first vs month-first, decided from the whole statement
    order = infer_order(r.date_text for r in raw)
    prior = priors.get(st.fingerprint, {}).get("date_order")
    if order is None and prior:
        order = prior
    if order is None and any(parse_date(r.date_text, "dmy") != parse_date(r.date_text, "mdy") for r in raw):
        st.add("date_order_ambiguous", "warn", "every numeric date is valid day-first and month-first; assumed day-first")
    st.date_order = order or "dmy"

    for r in raw:
        for note in r.notes:
            st.add("row_note", "warn", note, r.page)
        st.txns.append(Txn(parse_date(r.date_text, st.date_order), r.date_text, r.description, r.amount,
                           r.balance, r.page, r.top, r.sign_source))

    for page_no, dtext, d in dropped[:20]:
        st.add("date_without_amount", "error", f"dated line '{dtext}' ({d}) has no readable amount; a row may be missing", page_no)

    labels = scan_labels(pages, cfg, anchor_ids)
    st.opening_balance, st.closing_balance = labels["opening"], labels["closing"]
    st.declared_debits, st.declared_credits = labels["debits"], labels["credits"]

    for page_no, c in orphans[:20]:
        st.add("orphan_numeric_line", "warn", f"numbers on a line with no date, not used: '{c.line.text[:70]}'", page_no)

    chain = run_chain(st.txns, st.opening_balance)
    validate(st, chain, cfg)
    st.stats["rows"] = len(st.txns)
    st.stats["seconds"] = round(time.monotonic() - t0, 3)
    return st


# ---------------------------------------------------------------------------------------------------- batch


def _worker(args):
    path, cfg, priors = args
    try:
        return extract(path, cfg, priors).to_json()
    except Exception:
        st = Statement(file=os.path.basename(path), sha256=sha256_file(path))
        st.add("crash", "error", traceback.format_exc(limit=3))
        st.status = FAILED
        return st.to_json()


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def write_csv(result: dict, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "description", "debit", "credit", "balance", "page"])
        for t in result["transactions"]:
            w.writerow([t["date"], t["description"], t["debit"] or "", t["credit"] or "", t["balance"] or "", t["page"]])


class LayoutRegistry:
    """Per-layout health, persisted between runs. New or changed layouts show up here before they hurt."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def seen(self, fp: Optional[str]) -> bool:
        return fp in self.data

    def update(self, result: dict) -> None:
        fp = result["layout"]["fingerprint"]
        if not fp:
            return
        e = self.data.setdefault(fp, {"roles": result["layout"]["roles"], "files": 0, "accepted": 0, "review": 0,
                                      "failed": 0, "date_order": None, "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      "example": result["file"]})
        e["files"] += 1
        status = result["status"]
        key = "accepted" if status in (ACCEPTED, ACCEPTED_UNVERIFIED) else status
        e[key] = e.get(key, 0) + 1
        if status == ACCEPTED and result["layout"]["date_order"]:
            e["date_order"] = result["layout"]["date_order"]

    def priors(self) -> dict:
        return {fp: {"date_order": e.get("date_order")} for fp, e in self.data.items() if e.get("date_order")}

    def save(self) -> None:
        _atomic_write(self.path, self.data)


def collect_pdfs(inputs: list) -> list:
    out = []
    for p in inputs:
        p = Path(p)
        out.extend(sorted(p.rglob("*.pdf")) if p.is_dir() else [p])
    return [str(p) for p in out]


def process_batch(inputs: list, out_dir: str, cfg: Optional[Config] = None, workers: int = 2,
                  csv_out: bool = False, use_cache: bool = True, progress=None) -> dict:
    cfg = cfg or Config()
    out = Path(out_dir)
    for sub in ("accepted", "review", "failed", "cache"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    registry = LayoutRegistry(out / "layouts.json")
    priors = registry.priors()
    paths = collect_pdfs(inputs)
    t0 = time.monotonic()

    results, todo, cached = [], [], 0
    for p in paths:
        key = f"{sha256_file(p)}-{PARSER_VERSION}.json"
        cp = out / "cache" / key
        if use_cache and cp.exists():
            results.append((p, json.loads(cp.read_text()), True))
            cached += 1
        else:
            todo.append((p, cp))

    def finish(p, res, from_cache):
        res["file"] = os.path.basename(p)
        folder = {ACCEPTED: "accepted", ACCEPTED_UNVERIFIED: "accepted", REVIEW: "review"}.get(res["status"], "failed")
        _atomic_write(out / folder / (Path(p).stem + ".json"), res)
        if csv_out and folder == "accepted":
            write_csv(res, out / folder / (Path(p).stem + ".csv"))
        results.append((p, res, from_cache))
        if progress:
            progress(len(results), len(paths), res)

    for p, res, from_cache in list(results):
        results.remove((p, res, from_cache))
        finish(p, res, True)

    if workers <= 1:
        for p, cp in todo:
            res = _worker((p, cfg, priors))
            _atomic_write(cp, res)
            finish(p, res, False)
    elif todo:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_worker, (p, cfg, priors)): (p, cp) for p, cp in todo}
            for fut in as_completed(futs):
                p, cp = futs[fut]
                res = fut.result()
                _atomic_write(cp, res)
                finish(p, res, False)

    # run log + layout registry (written by the parent only: no races)
    new_layouts = 0
    with open(out / "run_log.jsonl", "a", encoding="utf-8") as log:
        for p, res, from_cache in sorted(results, key=lambda r: r[0]):
            fp = res["layout"]["fingerprint"]
            is_new = bool(fp) and not registry.seen(fp)
            new_layouts += is_new
            registry.update(res)
            log.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "file": res["file"], "sha256": res["sha256"],
                "status": res["status"], "layout": fp, "new_layout": is_new, "cached": from_cache,
                "errors": [f["code"] for f in res["findings"] if f["severity"] == "error"],
                "rows": res["stats"].get("rows"), "seconds": res["stats"].get("seconds"),
            }) + "\n")
    registry.save()

    statuses = Counter(r["status"] for _, r, _ in results)
    codes = Counter(f["code"] for _, r, _ in results for f in r["findings"] if f["severity"] == "error")
    pages = sum(r["stats"].get("pages", 0) for _, r, c in results if not c)
    wall = time.monotonic() - t0
    summary = {
        "files": len(paths), "by_status": dict(statuses), "cached": cached, "new_layouts": new_layouts,
        "top_error_codes": codes.most_common(8), "pages_parsed": pages, "wall_seconds": round(wall, 2),
        "pages_per_second": round(pages / wall, 1) if wall > 0 and pages else None,
        "layouts": {fp: {k: v for k, v in e.items() if k in ("roles", "files", "accepted", "review", "failed")} for fp, e in registry.data.items()},
    }
    _atomic_write(out / "summary.json", summary)
    with open(out / "index.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "status", "layout", "rows", "errors"])
        for p, r, _ in sorted(results, key=lambda r: r[0]):
            w.writerow([r["file"], r["status"], r["layout"]["fingerprint"] or "", r["stats"].get("rows", ""),
                        ";".join(sorted({f["code"] for f in r["findings"] if f["severity"] == "error"}))])
    return summary
