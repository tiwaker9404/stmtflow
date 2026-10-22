"""PDF -> positioned words -> lines.

Pure-Python (pdfplumber). No system dependencies. Only digital PDFs: if a file has almost no text layer it is
reported as scanned and routed to review rather than guessed at.
"""
from __future__ import annotations

import hashlib
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

import pdfplumber

from .models import Config


@dataclass
class Word:
    x0: float
    x1: float
    top: float
    bottom: float
    text: str
    size: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class Line:
    page: int
    yc: float
    words: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Page:
    number: int
    width: float
    height: float
    lines: list = field(default_factory=list)
    dropped: int = 0


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def group_lines(words: list, page_no: int, ytol: float = 3.0) -> list:
    words = sorted(words, key=lambda w: (w.yc, w.x0))
    lines: list = []
    cur: Optional[Line] = None
    for w in words:
        if cur is not None and abs(w.yc - cur.yc) <= ytol:
            cur.words.append(w)
            cur.yc = sum(x.yc for x in cur.words) / len(cur.words)
        else:
            cur = Line(page_no, w.yc, [w])
            lines.append(cur)
    for ln in lines:
        ln.words.sort(key=lambda w: w.x0)
    return lines


def read_pages(path: str, cfg: Config) -> list:
    pages = []
    t0 = time.monotonic()
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages[: cfg.max_pages], start=1):
            if time.monotonic() - t0 > cfg.deadline_s:
                raise TimeoutError(f"deadline of {cfg.deadline_s}s exceeded at page {i}")
            raw = page.extract_words(x_tolerance=2, y_tolerance=2, extra_attrs=["size", "upright"], keep_blank_chars=False)
            words = [Word(w["x0"], w["x1"], w["top"], w["bottom"], w["text"], float(w["size"])) for w in raw if w.get("upright", True)]
            dropped = 0
            if words:
                med = statistics.median(w.size for w in words)
                kept = [w for w in words if w.size <= cfg.oversize_factor * med]
                dropped += len(words) - len(kept)
                words = kept
            # strike-through bold: the same word drawn twice at (almost) the same spot
            seen, uniq = set(), []
            for w in words:
                key = (w.text, round(w.x0), round(w.top))
                if key in seen:
                    dropped += 1
                    continue
                seen.add(key)
                uniq.append(w)
            pages.append(Page(i, float(page.width), float(page.height), group_lines(uniq, i), dropped))
    return pages


def word_count(pages: list) -> int:
    return sum(len(ln.words) for p in pages for ln in p.lines)
