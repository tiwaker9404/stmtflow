"""Statement-level checks and the accept / review decision.

Checks are independent of how the rows were extracted, so they work for any extractor (this parser, an LLM
fallback, a future OCR path). A statement is ACCEPTED only if nothing failed AND at least one independent check
actually ran and passed. "No errors" with nothing checked is not the same as verified.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .chain import ChainResult
from .models import ACCEPTED, ACCEPTED_UNVERIFIED, REVIEW, Config, Statement

MAX_REPORTED_BREAKS = 25


def validate(st: Statement, chain: ChainResult, cfg: Config) -> None:
    txns = st.txns
    if not txns:
        st.add("no_rows", "error", "no transaction rows were extracted")
        st.status = REVIEW
        return

    # --- balance chain -----------------------------------------------------------------------------
    st.chain_direction = chain.direction
    st.chain_checked = chain.checked
    st.chain_breaks = len(chain.breaks)
    for b in chain.breaks[:MAX_REPORTED_BREAKS]:
        exp = "?" if b.expected is None else str(b.expected)
        st.add(b.kind, "error", f"balance {b.actual} but previous balance and amount give {exp}", b.page, b.index)
    if len(chain.breaks) > MAX_REPORTED_BREAKS:
        st.add("chain_breaks_truncated", "warn", f"{len(chain.breaks) - MAX_REPORTED_BREAKS} more breaks not listed")
    sign_flips = sum(1 for b in chain.breaks if b.kind == "sign_conflict")
    if chain.checked >= 4 and sign_flips >= 0.8 * chain.checked:
        st.add("columns_swapped", "error", "debit/credit look swapped: flipping the sign fits nearly every row")

    unresolved = [i for i, t in enumerate(txns) if t.sign_source == "unresolved"]
    for i in unresolved[:MAX_REPORTED_BREAKS]:
        st.add("sign_unresolved", "error", "amount has no sign and no balance to infer it from", txns[i].page, i)

    with_balance = sum(1 for t in txns if t.balance is not None)
    coverage = chain.checked / len(txns)
    if with_balance < len(txns):
        st.add("missing_balance", "warn", f"{len(txns) - with_balance} of {len(txns)} rows have no balance")
    if chain.checked and not chain.breaks and coverage >= cfg.min_chain_coverage:
        st.verified_by.append("balance_chain")
    elif chain.checked and coverage < cfg.min_chain_coverage:
        st.add("low_chain_coverage", "warn", f"only {chain.checked} of {len(txns)} rows could be chained")

    # --- opening / closing -------------------------------------------------------------------------
    resolved_all = not unresolved
    ordered = txns if chain.direction == "forward" else txns[::-1]
    if st.opening_balance is not None and st.closing_balance is not None and resolved_all:
        total = sum((t.amount for t in txns), Decimal("0"))
        if st.opening_balance + total == st.closing_balance:
            st.verified_by.append("opening_plus_rows_equals_closing")
        else:
            st.add("opening_closing_mismatch", "error",
                   f"opening {st.opening_balance} + rows {total} = {st.opening_balance + total}, statement says closing {st.closing_balance}")
    elif st.closing_balance is not None and ordered[-1].balance is not None:
        if ordered[-1].balance == st.closing_balance:
            st.verified_by.append("last_balance_equals_closing")
        else:
            st.add("closing_balance_mismatch", "error",
                   f"last row balance {ordered[-1].balance} but statement closing balance is {st.closing_balance}")

    # --- printed totals ----------------------------------------------------------------------------
    if resolved_all:
        debits = sum((-t.amount for t in txns if t.amount < 0), Decimal("0"))
        credits = sum((t.amount for t in txns if t.amount > 0), Decimal("0"))
        for name, declared, got in (("debits", st.declared_debits, debits), ("credits", st.declared_credits, credits)):
            if declared is None:
                continue
            if declared == got:
                st.verified_by.append(f"declared_total_{name}")
            else:
                st.add(f"declared_{name}_mismatch", "error", f"statement prints total {name} {declared}, rows sum to {got}")

    # --- the one row a balance chain cannot cover -----------------------------------------------------
    # The chronologically first row is checked against the opening balance. Without one, its amount is only
    # protected if printed debit/credit totals also matched. Say so explicitly instead of hiding it in "verified".
    totals_ok = any(v.startswith("declared_total") for v in st.verified_by) or "opening_plus_rows_equals_closing" in st.verified_by
    if st.opening_balance is None and not totals_ok and "balance_chain" in st.verified_by:
        first_i = 0 if chain.direction == "forward" else len(txns) - 1
        st.add("first_row_amount_unverified", "error" if cfg.strict_edges else "warn",
               "no opening balance or printed totals: the earliest row's amount has no independent check",
               txns[first_i].page, first_i)

    # --- dates -------------------------------------------------------------------------------------
    bad = [i for i, t in enumerate(txns) if t.date is None]
    for i in bad[:MAX_REPORTED_BREAKS]:
        st.add("date_invalid", "error", f"cannot read date '{txns[i].raw_date}'", txns[i].page, i)
    ds = [t.date for t in txns if isinstance(t.date, date)]
    if len(ds) > 1 and ds != sorted(ds) and ds != sorted(ds, reverse=True):
        st.add("dates_not_monotonic", "warn", "dates are neither ascending nor descending")

    # --- decision ----------------------------------------------------------------------------------
    if st.errors:
        st.status = REVIEW
    elif st.verified_by:
        st.status = ACCEPTED
    elif cfg.allow_unverified:
        st.status = ACCEPTED_UNVERIFIED
        st.add("unverified", "warn", "no balance column, opening/closing or printed totals to check against")
    else:
        st.status = REVIEW
        st.add("unverifiable", "error", "nothing to verify the rows against (no running balance, closing balance or totals)")
