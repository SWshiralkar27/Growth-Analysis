"""
batch_runner.py
Version: Step B1, v0.13.0 (2026-11-08). Code version 2026-11-stepB1.
Spec: README v3.3 DRAFT sections 1.1, 10 and 12.1 (the v2.0 batch decision matrix).

Purpose: the PURE decision logic of the overnight batch (about 90 tickers). This step holds no
runner: nothing here reads a file, calls the network or imports another engine module. It imports
only config. Step B2 adds the runner that uses these functions; step B3 adds the GitHub Actions job.

What is here
  - parse_tickers_csv      read the tickers list.
  - classify               pick the matrix row for one ticker (delta, no_record, stale,
                           financials_only, governance_only, fresh).
  - Budget                 count Gemini calls and estimated tokens against the run limits.
  - order_tickers          oldest analysis first, so new and stale tickers come before fresh ones.
  - failure counters       consecutive failed runs; a ticker is skipped after
                           config.MAX_CONSECUTIVE_FAILURES until the owner clears it.
  - summary_counts and public_log_lines
                           counts only, so the public Actions log never shows a ticker name.

Rules taken from README v2.0 (binding): the six matrix rows, 90 days, 5 percent drift, 200 calls,
1,500,000 tokens, 3 failures. Transient errors (429, 503) never count as failures.

[CONFIRM] choices made here because the spec is silent (the owner has not approved them):
  1. Drift is the relative change of each ratio against its stored value, in percent, and applies to
     revenue_growth, ebitda_margin, return_on_equity and debt_to_equity. A ratio whose stored value is
     missing or zero is not compared.
  2. A filing counts as new when its name is not among the documents the stored analysis used or
     recorded as unreadable. A filing that was removed is ignored.
  3. Tokens are estimated as characters divided by config.BATCH_CHARS_PER_TOKEN (4), rounded up. The
     fixed prompt text is not added.
  4. Tickers are processed oldest analysis first (no stored analysis first), then in file order.
  5. "governance_only" means the scorer would treat governance as never checked (it mirrors the two
     routes in score_card._governance_checked that do not need a verified flag). It changes the
     label only: the action is the same as "fresh".
  6. The failure counter goes up only for status "failed"; "ok" and "stale" reset it; "partial",
     "deferred" and "skipped" leave it unchanged.
"""
from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

import config

# ------------------------------------------------------------ matrix rows
DELTA = "delta"
NO_RECORD = "no_record"
STALE = "stale"
FINANCIALS_ONLY = "financials_only"
GOVERNANCE_ONLY = "governance_only"
FRESH = "fresh"
MATRIX_STATUSES = (DELTA, NO_RECORD, STALE, FINANCIALS_ONLY, GOVERNANCE_ONLY, FRESH)
GEMINI_CALL_STATUSES = frozenset({DELTA, NO_RECORD, STALE})

# Result statuses a batch run reports per ticker. The first four are the pipeline's own statuses.
RUN_STATUSES = ("ok", "stale", "partial", "failed", "deferred", "skipped")

DRIFT_FIELDS = ("revenue_growth", "ebitda_margin", "return_on_equity", "debt_to_equity")
STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Decision:
    """The matrix row chosen for one ticker."""
    status: str
    reason: str
    gemini_call: bool
    force_refresh: bool = False     # True for "stale": the same inputs must not hit the cache
    drifted: tuple = ()             # ratio names that moved more than the threshold


# -------------------------------------------------------------- utilities
def _num(value: Any) -> Optional[float]:
    """A finite number, or None (booleans, text and NaN are not numbers)."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _parse_iso_date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    return stamp.date()


# ---------------------------------------------------------------- tickers
def parse_tickers_csv(text: Any) -> tuple[list, list]:
    """Read the tickers list. Returns (tickers, problems).

    The first column is the ticker. A header row (ticker, tickers or symbol), blank lines and lines
    starting with # are ignored. Tickers are upper-cased, checked against config.TICKER_PATTERN and
    de-duplicated in order. A problem message never repeats the text of the bad line.
    """
    if not isinstance(text, str):
        return [], ["the tickers file is not text"]
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    tickers: list = []
    problems: list = []
    seen: set = set()
    first_row = True
    for row in reader:
        cell = (row[0] if row else "").strip()
        if not cell or cell.startswith("#"):
            continue
        if first_row:
            first_row = False
            if cell.lower() in ("ticker", "tickers", "symbol"):
                continue
        ticker = cell.upper()
        if not config.TICKER_PATTERN.match(ticker) or not any(ch.isalnum() for ch in ticker):
            problems.append(f"line {reader.line_num}: not a valid ticker")
            continue
        if ticker in seen:
            problems.append(f"line {reader.line_num}: duplicate ticker ignored")
            continue
        seen.add(ticker)
        tickers.append(ticker)
    return tickers, problems


# ----------------------------------------------------- stored-record facts
def analysis_date(record: Any) -> Optional[date]:
    """The day the stored analysis was made: analysis.analysed_at, else the record's saved_at."""
    if not isinstance(record, dict):
        return None
    analysis = record.get("analysis")
    if isinstance(analysis, dict):
        found = _parse_iso_date(analysis.get("analysed_at"))
        if found is not None:
            return found
    return _parse_iso_date(record.get("saved_at"))


def known_documents(analysis: Any) -> set:
    """Names of the filings the stored analysis has already seen (read or unreadable)."""
    names: set = set()
    if not isinstance(analysis, dict):
        return names
    for key in ("documents_used", "documents_unreadable"):
        items = analysis.get(key)
        if not isinstance(items, (list, tuple)):
            continue
        for item in items:
            if isinstance(item, (list, tuple)) and item:
                item = item[0]
            if isinstance(item, str) and item:
                names.add(item)
    return names


def financial_drift(prior_fin: Any, new_fin: Any) -> dict:
    """Relative change in percent for each drift ratio that has a usable stored value and a new value."""
    prior = prior_fin if isinstance(prior_fin, dict) else {}
    new = new_fin if isinstance(new_fin, dict) else {}
    out: dict = {}
    for name in DRIFT_FIELDS:
        old, cur = _num(prior.get(name)), _num(new.get(name))
        if old is None or cur is None or old == 0.0:
            continue
        out[name] = round(abs(cur - old) / abs(old) * 100.0, 9)   # rounded so exactly 5% is not 5.000000000000004
    return out


def drifted_fields(prior_fin: Any, new_fin: Any, threshold_pct: Optional[float] = None) -> tuple:
    """Ratios that moved by more than the threshold (strictly), in a fixed order."""
    limit = config.FINANCIAL_CHANGE_THRESHOLD_PCT if threshold_pct is None else threshold_pct
    moved = financial_drift(prior_fin, new_fin)
    return tuple(name for name in DRIFT_FIELDS if name in moved and moved[name] > limit)


def governance_checked(analysis: Any, financials: Any) -> bool:
    """Mirror of the two scorer routes that need no verified flag (see [CONFIRM] 5)."""
    coverage = analysis.get("governance_coverage") if isinstance(analysis, dict) else None
    if isinstance(coverage, list) and "auditor_report" in coverage and len(coverage) >= 2:
        return True
    sources = financials.get("governance_sources") if isinstance(financials, dict) else None
    sources = sources if isinstance(sources, (list, tuple, set)) else []
    return "dalal.shareholding" in sources and "dalal.announcements" in sources


# ---------------------------------------------------------------- matrix
def classify(prior: Any, current_docs: Iterable, new_fin: Any, today: Optional[date] = None) -> Decision:
    """Pick the matrix row for one ticker.

    prior         the stored record (dict) or None.
    current_docs  names of the filings readable now (the context's document list).
    new_fin       the freshly fetched financials.
    Order of the rules: no_record, delta, stale, financials_only, governance_only, fresh.
    """
    today = today or date.today()
    analysis = prior.get("analysis") if isinstance(prior, dict) else None
    if not isinstance(analysis, dict) or analysis.get("analysis_status") != "success":
        return Decision(NO_RECORD, "no stored analysis", True)

    version = analysis.get("prompt_version") or prior.get("prompt_version")
    if version != config.PROMPT_VERSION:
        return Decision(NO_RECORD, "prompt version changed", True)

    docs = {d for d in (current_docs or []) if isinstance(d, str)}
    new_docs = docs - known_documents(analysis)
    if new_docs:
        return Decision(DELTA, f"{len(new_docs)} new filing(s)", True)

    made = analysis_date(prior)
    if made is None or (today - made).days > config.ANALYSIS_MAX_AGE_DAYS:
        return Decision(STALE, f"analysis older than {config.ANALYSIS_MAX_AGE_DAYS} days or undated",
                        True, force_refresh=True)

    moved = drifted_fields(prior.get("financials"), new_fin)
    if moved:
        return Decision(FINANCIALS_ONLY,
                        f"ratios moved more than {config.FINANCIAL_CHANGE_THRESHOLD_PCT:g}%: reuse the AI output",
                        False, drifted=moved)

    if not governance_checked(analysis, new_fin):
        return Decision(GOVERNANCE_ONLY, "governance data missing: reuse the AI output", False)

    return Decision(FRESH, "stored analysis is current", False)


# ----------------------------------------------------------------- budget
def estimate_tokens(chars: Any) -> int:
    """Estimated tokens for a prompt of this many characters (see [CONFIRM] 3)."""
    n = _num(chars)
    if n is None or n <= 0:
        return 0
    per = max(1, int(config.BATCH_CHARS_PER_TOKEN))
    return int(math.ceil(n / per))


class Budget:
    """Counts Gemini calls and estimated tokens against the per-run limits."""

    def __init__(self, max_calls: Optional[int] = None, max_tokens: Optional[int] = None) -> None:
        self.max_calls = config.BATCH_MAX_CALLS_PER_RUN if max_calls is None else int(max_calls)
        self.max_tokens = config.BATCH_MAX_TOKENS_PER_RUN if max_tokens is None else int(max_tokens)
        self.calls = 0
        self.tokens = 0

    def can_afford(self, tokens: int) -> bool:
        return self.calls + 1 <= self.max_calls and self.tokens + max(0, int(tokens)) <= self.max_tokens

    def charge(self, tokens: int) -> None:
        """Record one call. Call can_afford first; charging past a limit is refused."""
        if not self.can_afford(tokens):
            raise ValueError("budget exceeded")
        self.calls += 1
        self.tokens += max(0, int(tokens))


def order_tickers(tickers: Iterable, records: Optional[dict] = None) -> list:
    """Oldest analysis first; tickers with no stored analysis come first; file order breaks ties."""
    records = records or {}
    listed = list(tickers)

    def key(item):
        made = analysis_date(records.get(item))
        return (made is not None, made or date.min)

    return sorted(listed, key=key)   # sorted() is stable, so equal keys keep file order


# ------------------------------------------------------- failure counters
def new_state() -> dict:
    return {"schema_version": STATE_SCHEMA_VERSION, "tickers": {}}


def parse_state(text: Any) -> tuple[dict, Optional[str]]:
    """Read the saved counters. A missing file gives an empty state; a damaged or newer file gives an
    empty state and a warning, so the runner can keep the original and tell the owner."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return new_state(), None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return new_state(), "the batch state file is damaged and was not used"
    if not isinstance(data, dict) or not isinstance(data.get("tickers"), dict):
        return new_state(), "the batch state file is damaged and was not used"
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != STATE_SCHEMA_VERSION:
        return new_state(), "the batch state file has an unknown version and was not used"
    clean = new_state()
    dropped = 0
    for ticker, entry in data["tickers"].items():
        failures = entry.get("consecutive_failures") if isinstance(entry, dict) else None
        if not isinstance(ticker, str) or not isinstance(failures, int) or isinstance(failures, bool) \
                or failures < 0:
            dropped += 1
            continue
        clean["tickers"][ticker] = {
            "consecutive_failures": failures,
            "permanently_failed": bool(entry.get("permanently_failed")),
            "last_status": entry.get("last_status") if isinstance(entry.get("last_status"), str) else None,
            "last_run": entry.get("last_run") if isinstance(entry.get("last_run"), str) else None,
        }
    warning = f"{dropped} unreadable entr{'y' if dropped == 1 else 'ies'} in the batch state file dropped" \
        if dropped else None
    return clean, warning


def dump_state(state: dict) -> str:
    return json.dumps(state, indent=2, sort_keys=True)


def is_skipped(state: dict, ticker: str) -> bool:
    entry = (state.get("tickers") or {}).get(ticker)
    return bool(entry and entry.get("permanently_failed"))


def skipped_tickers(state: dict, tickers: Iterable) -> list:
    return [t for t in tickers if is_skipped(state, t)]


def record_outcome(state: dict, ticker: str, status: str, today: Optional[date] = None) -> dict:
    """Update one ticker's counter after a run and return its entry (see [CONFIRM] 6)."""
    today = today or date.today()
    entry = state.setdefault("tickers", {}).setdefault(
        ticker, {"consecutive_failures": 0, "permanently_failed": False,
                 "last_status": None, "last_run": None})
    entry["last_status"] = status
    entry["last_run"] = today.isoformat()
    if status in ("ok", "stale"):
        entry["consecutive_failures"] = 0
        entry["permanently_failed"] = False
    elif status == "failed":
        entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
        if entry["consecutive_failures"] >= config.MAX_CONSECUTIVE_FAILURES:
            entry["permanently_failed"] = True
    return dict(entry)


def clear_failures(state: dict, ticker: str) -> bool:
    """The owner clears a skipped ticker. Returns False when there was nothing to clear."""
    entry = (state.get("tickers") or {}).get(ticker)
    if not entry:
        return False
    entry["consecutive_failures"] = 0
    entry["permanently_failed"] = False
    return True


# ------------------------------------------------------------ the summary
def summary_counts(rows: Iterable, calls: int = 0, est_tokens: int = 0,
                   max_calls: Optional[int] = None, max_tokens: Optional[int] = None) -> dict:
    """Counts only. Each row is a dict with "status" (a RUN_STATUSES value) and optionally "matrix"."""
    rows = list(rows)
    by_status = {s: 0 for s in RUN_STATUSES}
    by_matrix = {m: 0 for m in MATRIX_STATUSES}
    for row in rows:
        status = row.get("status")
        if status in by_status:
            by_status[status] += 1
        matrix = row.get("matrix")
        if matrix in by_matrix:
            by_matrix[matrix] += 1
    return {
        "tickers": len(rows),
        "by_status": by_status,
        "by_matrix": by_matrix,
        "gemini_calls": int(calls),
        "est_tokens": int(est_tokens),
        "max_calls": config.BATCH_MAX_CALLS_PER_RUN if max_calls is None else int(max_calls),
        "max_tokens": config.BATCH_MAX_TOKENS_PER_RUN if max_tokens is None else int(max_tokens),
    }


def public_log_lines(counts: dict) -> list:
    """Lines for the public Actions log: counts only, never a ticker name or a score."""
    status = counts.get("by_status", {})
    matrix = counts.get("by_matrix", {})
    return [
        f"Batch run: {counts.get('tickers', 0)} tickers",
        "Status: " + ", ".join(f"{s} {status.get(s, 0)}" for s in RUN_STATUSES),
        "Matrix: " + ", ".join(f"{m} {matrix.get(m, 0)}" for m in MATRIX_STATUSES),
        f"Gemini calls: {counts.get('gemini_calls', 0)} of {counts.get('max_calls', 0)}; "
        f"estimated tokens {counts.get('est_tokens', 0):,} of {counts.get('max_tokens', 0):,}",
    ]
