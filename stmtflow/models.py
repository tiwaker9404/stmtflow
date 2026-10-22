"""Data model. All money is Decimal; nothing in the pipeline ever touches floats."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

PARSER_VERSION = "0.2.0"

# Statuses a statement can end in.
ACCEPTED = "accepted"                    # no errors AND at least one independent check passed
ACCEPTED_UNVERIFIED = "accepted_unverified"  # no errors, but nothing to verify against (opt-in)
REVIEW = "review"                        # something failed a check: a human should look
FAILED = "failed"                        # could not be read at all (scanned, no table, exception)


@dataclass
class Config:
    require_decimals: bool = True        # amounts must print as 12.34; stops IDs/years being read as money
    oversize_factor: float = 2.0         # drop glyphs larger than N x page median size (watermarks, stray decoration)
    max_pages: int = 300
    deadline_s: float = 120.0            # wall-clock guard per file
    allow_unverified: bool = False       # if False, statements with no balance/total to check go to review
    min_chain_coverage: float = 0.9
    strict_edges: bool = False           # True: an amount nothing can verify (first row, no opening balance) sends the file to review


@dataclass
class Txn:
    date: Optional[date]
    raw_date: str
    description: str
    amount: Decimal                      # signed: credit +, debit -. Positive magnitude while sign_source == "unresolved"
    balance: Optional[Decimal]
    page: int
    top: float                           # y position on the page, so a reviewer can jump to the row
    sign_source: str                     # column | marker | balance_delta | unresolved

    @property
    def debit(self) -> Optional[Decimal]:
        return -self.amount if self.amount < 0 else None

    @property
    def credit(self) -> Optional[Decimal]:
        return self.amount if self.amount > 0 else None

    def to_json(self) -> dict:
        return {
            "date": self.date.isoformat() if self.date else None,
            "raw_date": self.raw_date,
            "description": self.description,
            "amount": str(self.amount),
            "debit": None if self.debit is None else str(self.debit),
            "credit": None if self.credit is None else str(self.credit),
            "balance": None if self.balance is None else str(self.balance),
            "page": self.page,
            "top": round(self.top, 1),
            "sign_source": self.sign_source,
        }


@dataclass
class Finding:
    code: str
    severity: str                        # "error" | "warn"
    message: str
    page: Optional[int] = None
    row: Optional[int] = None

    def to_json(self) -> dict:
        d = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.page is not None:
            d["page"] = self.page
        if self.row is not None:
            d["row"] = self.row
        return d


@dataclass
class Statement:
    file: str
    sha256: str
    status: str = FAILED
    fingerprint: Optional[str] = None
    roles: list = field(default_factory=list)
    date_order: Optional[str] = None
    opening_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None
    declared_debits: Optional[Decimal] = None
    declared_credits: Optional[Decimal] = None
    chain_direction: Optional[str] = None
    chain_checked: int = 0
    chain_breaks: int = 0
    verified_by: list = field(default_factory=list)
    txns: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def add(self, code, severity, message, page=None, row=None):
        self.findings.append(Finding(code, severity, message, page, row))

    @property
    def errors(self):
        return [f for f in self.findings if f.severity == "error"]

    def to_json(self) -> dict:
        s = lambda v: None if v is None else str(v)
        return {
            "file": self.file,
            "sha256": self.sha256,
            "parser_version": PARSER_VERSION,
            "status": self.status,
            "layout": {"fingerprint": self.fingerprint, "roles": self.roles, "date_order": self.date_order},
            "opening_balance": s(self.opening_balance),
            "closing_balance": s(self.closing_balance),
            "declared_debits": s(self.declared_debits),
            "declared_credits": s(self.declared_credits),
            "verification": {
                "verified_by": self.verified_by,
                "chain_direction": self.chain_direction,
                "chain_checked": self.chain_checked,
                "chain_breaks": self.chain_breaks,
            },
            "findings": [f.to_json() for f in self.findings],
            "stats": self.stats,
            "transactions": [t.to_json() for t in self.txns],
        }
