# stmtflow

Extract transactions from digital PDF bank statements, and **prove each result is right before anyone relies on it**.

Most extraction tools stop at "here are the rows". At scale that is the wrong place to stop, because the failure that
costs money is the row that is wrong and nobody noticed. stmtflow treats extraction as half the job. The other half
is an independent check, and anything that cannot pass the check goes to a review queue instead of into your data.

```
PDF ─► read words + positions ─► find table header ─► learn column positions from the data ─► group lines into rows
                                                                                                   │
        accepted/  ◄── all checks pass ◄── VALIDATE: balance chain, opening+rows=closing, printed totals, dates
        review/    ◄── a check failed or nothing could be checked (with the exact row and reason)
        failed/    ◄── unreadable, encrypted, scanned (no text layer), timed out
```

## Where this came from

I started from an existing open-source project, [AyonPal/bank-statement-pdf-parser](https://github.com/AyonPal/bank-statement-pdf-parser)
(MIT), and ran it on its own three sample statements. With default settings it returned dates, descriptions and
balances but **no deposit or withdrawal amounts on two of the three**, and it prints no warning about it. Reading the
code showed why: column positions are hard-coded x-coordinates (`deposit 350–420`, `withdrawal 420–500`), so a page
that is laid out differently silently loses its amounts, and nothing checks the output. I also read the README of
[invoice2data](https://github.com/m3nu/invoice2data) (MIT), which solves a neighbouring problem with one hand-written
template per supplier, and whose README describes no validation layer. Both are useful projects; the shared
weakness is that each new layout means new manual work and no signal when something is wrong.

stmtflow is a rewrite, not a patch: none of the upstream parsing code is used. What I kept from upstream is its three
sample PDFs and answer files as test fixtures (`tests/fixtures/upstream/`, with its MIT licence), and a copy of its
script in `bench/` so the comparison can be rerun. Everything else here is mine.

## What it does differently

| Problem | Upstream style | stmtflow |
|---|---|---|
| Where are the columns? | hard-coded x ranges | read from the table header, then **refined from the data** (numbers are right-, left- or centre-aligned; the tightest fit wins) |
| Is this row right? | no check | `previous balance + amount = this balance`, in `Decimal`, per row |
| Debit or credit? | column position only | column, `CR`/`DR` or `-`/`( )` markers, or whichever sign makes the balance chain close |
| Oldest-first or newest-first? | assumed | detected from the chain |
| Row split over several lines | breaks | rows are *blocks*; the date/amount line may be top, middle or bottom |
| `FEE 2.50` inside a description | read as an amount | one cell per column per line; the value nearest the column wins |
| A row that cannot be read | disappears | reported (`date_without_amount`) |
| New or drifting layout | invisible | fingerprinted and tracked in `layouts.json` |
| Many files | one at a time | process pool, content-hash cache, one bad file never stops a run, per-file deadline |

Values are never edited to make a check pass. The chain reports and, where a sign is missing, resolves it; it does not
"fix" amounts.

## Quick start

```bash
pip install -e ".[dev]"            # pdfplumber (+ reportlab, pytest, pypdf for tests and the synthetic generator)

python -m stmtflow extract statement.pdf                    # JSON on stdout; exit 0 accepted, 3 review, 4 failed
python -m stmtflow batch statements/ -o out/ -w 4 --csv     # folders in, accepted/ review/ failed/ out
python -m stmtflow synth corpus/ -n 200                     # synthetic statements + exact ground truth
python -m pytest tests -q                                   # 21 tests
python bench/run_bench.py 1000 --first 20000                # accuracy and silent-error count
python bench/tamper_bench.py corpus/                        # fault injection
```

`examples/` has three small PDFs and the output folder produced from them (one accepted, one multi-page newest-first
accepted, one in `review/` with the reason).

Output per statement (abridged):

```json
{"status": "accepted",
 "verification": {"verified_by": ["balance_chain", "opening_plus_rows_equals_closing"], "chain_checked": 59, "chain_breaks": 0},
 "findings": [],
 "transactions": [{"date": "2025-03-13", "description": "MONTHLY SERVICE FEE", "amount": "2193.87",
                   "balance": "24809.22", "page": 1, "top": 79.7, "sign_source": "column"}]}
```

`page` and `top` let a reviewer jump to the row. `sign_source` says how the sign was decided.

## Results (all on synthetic statements; read the limits)

Corpus: `stmtflow/synth.py` draws random layouts (column mode and order, header wording, 8 date formats, alignment,
fonts, wrapped descriptions, page breaks with or without a repeated header, newest-first, watermarks, decimal numbers
inside descriptions) and keeps the exact answer.

| Set | Statements | Accepted | Accepted and fully correct | Accepted with wrong money | Sent to review |
|---|---|---|---|---|---|
| Held-out (seeds 20000–20999, not looked at while developing) | 1,000 | 884 | 884 | **0** | 116 |

Of the 116 reviewed, 89 turned out to be extracted correctly (the checker could not prove it) and 27 contained at least one wrong row
that the review queue caught. Row-level exactness over all 44,203 rows: 99.69%.

**Fault injection** (`bench/tamper_bench.py`, run on a separate 300-statement set, 264 accepted): change one digit of
one printed amount in an accepted statement and re-run. On 481 altered statements, 479 went to review, 2 were accepted but carry a `first_row_amount_unverified`
warning (see limits), 0 were accepted silently.

**Against the upstream script** (default settings, my corpus, `bench/baseline_compare.py`): it returned 0 correct rows out of
4,300 across 97 statements. I do not treat that as a fair headline, because the corpus is deliberately more varied
than the three layouts the upstream script was written for. The fair version is its own samples: stmtflow reproduces
their published answers on all three (tests), and the upstream script at defaults drops the amounts on two.

Speed: about 0.1 s per 1–3 page statement per core on my two-core machine (1,000 statements in 103 s, sequential).

## Limits, stated plainly

- **Synthetic data.** The corpus is my model of what banks print. It is a regression and stress suite, not evidence
  about any real bank. Real statements will find things it does not. Fixtures from the upstream project are the only
  third-party layouts here.
- **Digital PDFs only.** No OCR. A scanned file is reported as `no_text_layer` and goes to `failed/`.
- **Transaction tables only.** No fee schedules, interest tables or credit-card reward summaries.
- **The earliest row with no opening balance cannot be verified.** Nothing in the document constrains it. It is flagged
  (`first_row_amount_unverified`); set `Config(strict_edges=True)` to send those files to review instead.
- **Dates have no redundancy.** A date can be wrong in a way that keeps the arithmetic intact. Only monotonic order is
  checked.
- **Unsigned amounts with no balance** (one column, no sign, no running balance) are ambiguous. They go to review.
- **Day-first vs month-first** is inferred from the whole statement; a statement where every date is valid both ways
  is assumed day-first with a warning.
- **Conservative by design.** 89 of the 116 reviewed statements were right. That is the price of not accepting a wrong
  one; `allow_unverified` and `strict_edges` move the line.
- **No LLM or OCR fallback is built.** The review queue is where one would attach: re-run only the files that failed
  with a different extractor, and accept its output only if it passes the *same* validator. The validator does not
  care which extractor produced the rows. That design is deliberate but untested here.

## Layout

```
stmtflow/reader.py     pdfplumber words → lines (drops oversize watermark glyphs, deadline guard)
stmtflow/layout.py     finds the table header, assigns column roles, fingerprints the layout
stmtflow/extract.py    column models, line tokenising, row segmentation, opening/closing/total labels
stmtflow/amounts.py    money and date parsing as printed (parentheses, trailing minus, CR/DR, 1,23,456.78)
stmtflow/chain.py      balance-chain walk: direction, sign resolution, breaks
stmtflow/validate.py   statement-level checks and the accept / review decision
stmtflow/pipeline.py   extract() and process_batch(): cache, pool, folders, run log, layout registry
stmtflow/synth.py      synthetic statements with exact ground truth
bench/                 run_bench, tamper_bench, baseline_compare (+ vendored upstream script, MIT)
```

## Licence

MIT. The upstream project's licence is kept next to the files taken from it
(`tests/fixtures/upstream/UPSTREAM_LICENSE.txt`, `bench/UPSTREAM_LICENSE.txt`).
