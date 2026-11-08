"""
batch_runner.py
Version: Step B2, v0.14.0 (2026-11-08). Code version 2026-11-stepB2.
Spec: README v3.3 DRAFT sections 1.1, 10 and 12.1 (the v2.0 batch decision matrix).

Purpose: the overnight batch (about 90 tickers). Command line: python batch_runner.py [--limit N].
Like app.py this is an entry point, so it imports pipeline (the only engine module it uses), config
and errors. Step B1 added the pure decision logic (the first part of this file, which reads no file
and uses no network). Step B2 adds the runner (the second part). Step B3 adds the GitHub Actions job.

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
  - run_batch / run_live   (Step B2) wrap the pipeline's analyse stage with the matrix decision,
                           spend the budget, update the counters and write the summary files.

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
  7. (B2) A ticker the budget cannot afford is "deferred": nothing is saved for it and its stored
     analysis stays as it was. A stored analysis with no analysed_at date takes the record's saved_at
     as its date the first time it is reused. The engine's own printed output is not shown in the
     public log; it is written to the private run log instead.
  8. (B2) Demo tickers (FAKE, STALE429, NOPRIOR429, FAILPERM) are ignored by the batch.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import logging
import math
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import config
import pipeline
from errors import TransientError

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


# =====================================================================
# Step B2: the runner
# =====================================================================
def _reused_analysis(prior: dict) -> dict:
    """The stored analysis for a zero-call row. A legacy analysis with no analysed_at takes the
    record's saved_at as its date, once, so the 90-day clock can start (see [CONFIRM] 7)."""
    analysis = dict(prior["analysis"])
    if not analysis.get("analysed_at") and _parse_iso_date(prior.get("saved_at")) is not None:
        analysis["analysed_at"] = prior["saved_at"]
    return analysis


def make_batch_analyse(real_analyse: Callable, load_prior: Callable, budget: "Budget", today: date,
                       now_fn: Callable, decisions: dict, deferred: set) -> Callable:
    """The analyse stage for a batch run: pick the matrix row, then reuse the stored analysis (no
    Gemini call) or call Gemini once. The decision of every ticker is written into `decisions`;
    a ticker the budget cannot afford is added to `deferred` and raises TransientError, which the
    pipeline turns into "keep the prior analysis, save nothing"."""
    lock = threading.Lock()

    def analyse(ticker: str, context: Any, financials: Any) -> dict:
        try:
            prior = load_prior(ticker)
        except Exception:      # a damaged record is set aside by the store; treat it as no record
            prior = None
        ctx = context if isinstance(context, dict) else {}
        decision = classify(prior, ctx.get("documents") or [], financials, today)
        with lock:
            decisions[ticker] = decision
        if not decision.gemini_call:
            return _reused_analysis(prior)
        estimate = estimate_tokens(len(ctx.get("text") or ""))
        with lock:
            if not budget.can_afford(estimate):
                deferred.add(ticker)
                raise TransientError("deferred: the Gemini budget for this run is used up")
            budget.charge(estimate)
        if decision.force_refresh:
            result = real_analyse(ticker, ctx, financials, force_refresh=True)
        else:
            result = real_analyse(ticker, ctx, financials)
        analysis = dict(result)
        analysis["analysed_at"] = now_fn().isoformat()
        return analysis

    return analyse


@dataclass
class BatchResult:
    rows: list
    counts: dict
    state: dict
    warnings: list = field(default_factory=list)
    run_date: str = ""
    started_at: str = ""
    finished_at: str = ""

    def summary_json(self) -> str:
        """The full summary, with ticker names and scores. PRIVATE: written to the private repository."""
        return json.dumps({
            "run_date": self.run_date, "started_at": self.started_at, "finished_at": self.finished_at,
            "code_version": config.CODE_VERSION, "prompt_version": config.PROMPT_VERSION,
            "counts": self.counts, "warnings": self.warnings, "rows": self.rows,
        }, indent=2, default=str)

    def ranked_csv(self) -> str:
        """The ranked list as CSV (private)."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["Rank", "Ticker", "Status", "Matrix row", "Total", "Rating", "Reason", "Notes"])
        for r in self.rows:
            writer.writerow([r.get("rank"), r.get("ticker"), r.get("status"), r.get("matrix"),
                             r.get("total"), r.get("rating"), r.get("reason"), r.get("notes")])
        return out.getvalue()

    def public_lines(self) -> list:
        """Counts only: safe for the public Actions log."""
        lines = public_log_lines(self.counts)
        if self.warnings:
            lines.append(f"Warnings: {len(self.warnings)} (see the private run summary)")
        return lines


def run_batch(tickers: Iterable, state: dict, stages: "pipeline.Stages", real_analyse: Callable,
              budget: Optional[Budget] = None, today: Optional[date] = None,
              now_fn: Optional[Callable] = None, horizon_days: Optional[int] = None,
              limit: Optional[int] = None, warnings: Optional[list] = None) -> BatchResult:
    """Run the pipeline over the tickers with the batch decision matrix and budget.

    `stages` carries everything except the analyse stage, which is wrapped here. `state` (the failure
    counters) is updated in place and also returned in the result. One failing ticker never stops the run.
    """
    today = today or date.today()
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    budget = budget or Budget()
    horizon = horizon_days or config.DEFAULT_HORIZON_DAYS
    notes = list(warnings or [])
    started = now_fn().isoformat()

    tickers = list(tickers)
    listed = [t for t in tickers if t not in pipeline.DEMO_TICKERS]
    if len(tickers) != len(listed):
        notes.append(f"{len(tickers) - len(listed)} demo ticker(s) ignored")
    if limit is not None and limit > 0:
        listed = listed[:limit]
    skipped = skipped_tickers(state, listed)
    skip_set = set(skipped)
    to_run = [t for t in listed if t not in skip_set]

    records: dict = {}
    for t in to_run:
        try:
            records[t] = stages.load_prior(t)
        except Exception:
            records[t] = None
    ordered = order_tickers(to_run, records)

    decisions: dict = {}
    deferred: set = set()
    wrapped = replace(stages, analyse=make_batch_analyse(
        real_analyse, stages.load_prior, budget, today, now_fn, decisions, deferred))
    run = pipeline.run_pipeline(ordered, horizon, wrapped, max_workers=1)

    rows: list = []
    for result, line in zip(run.results, run.to_rows()):
        status = "deferred" if result.ticker in deferred else result.status
        decision = decisions.get(result.ticker)
        rows.append({"ticker": result.ticker, "status": status,
                     "matrix": decision.status if decision else None,
                     "reason": decision.reason if decision else "",
                     "rank": line.get("Rank"), "total": line.get("Total"), "rating": line.get("Rating"),
                     "notes": line.get("Notes")})
        record_outcome(state, result.ticker, status, today)
    for t in skipped:
        rows.append({"ticker": t, "status": "skipped", "matrix": None,
                     "reason": f"skipped after {config.MAX_CONSECUTIVE_FAILURES} failed runs in a row; "
                               f"clear it in batch/state.json",
                     "rank": None, "total": None, "rating": None, "notes": ""})
        record_outcome(state, t, "skipped", today)

    counts = summary_counts(rows, budget.calls, budget.tokens, budget.max_calls, budget.max_tokens)
    return BatchResult(rows=rows, counts=counts, state=state, warnings=notes, run_date=today.isoformat(),
                       started_at=started, finished_at=now_fn().isoformat())


# ------------------------------------------------------------ live run
def _rel(path: Any) -> Optional[str]:
    """Path relative to the data folder, in the form the private repository uses."""
    try:
        return Path(path).resolve().relative_to(Path(config.DATA_DIR).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _write_atomic(path: Any, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _live_stages() -> "pipeline.Stages":
    stages, _ = pipeline.make_stub_stages(store=pipeline.default_store())
    return replace(stages, filings_context=partial(pipeline.filings_context_stage,
                                                   max_chars=config.BATCH_MAX_PROMPT_CHARS))


def run_live(limit: Optional[int] = None, today: Optional[date] = None,
             stages_factory: Optional[Callable] = None,
             analyse_factory: Optional[Callable] = None) -> tuple:
    """Restore data, run the batch, write and back up the summary. Returns (exit_code, public_lines).

    Exit codes: 0 done (individual failed tickers do not fail the job); 1 done but the summary or
    counters could not be backed up; 2 stopped before running (set-up problem). Messages name no ticker.
    The engine's own printed output is captured into the private run log, never the public log.
    """
    today = today or date.today()
    try:
        config.ensure_dirs()
        mirror = pipeline.get_mirror()
        if mirror is None:
            return 2, ["Batch stopped: permanent storage is not set up (GITHUB_DATA_REPO and GITHUB_DATA_TOKEN)."]
        if analyse_factory is None and not config.has_secret("gemini_api_key"):
            return 2, ["Batch stopped: the Gemini key (GEMINI_API_KEY) is not set."]

        warnings: list = []
        restored = pipeline.restore_from_backup()
        if restored is not None and not restored.ok:
            warnings.append(f"restore had {len(restored.errors)} error(s)")

        tickers_path, state_path = Path(config.BATCH_TICKERS_FILE), Path(config.BATCH_STATE_FILE)
        mirror.pull_file(_rel(tickers_path))
        if not tickers_path.is_file():
            return 2, ["Batch stopped: batch/tickers.csv was not found in the private data repository."]
        mirror.pull_file(_rel(state_path))           # absent on the very first run: that is fine

        tickers, problems = parse_tickers_csv(tickers_path.read_text(encoding="utf-8"))
        if problems:
            warnings.append(f"{len(problems)} problem line(s) in batch/tickers.csv")
        if not tickers:
            return 2, ["Batch stopped: batch/tickers.csv has no valid tickers."]
        state_text = state_path.read_text(encoding="utf-8") if state_path.is_file() else None
        state, state_warning = parse_state(state_text)
        if state_warning:
            warnings.append(state_warning)

        stages = stages_factory() if stages_factory else _live_stages()
        real_analyse = analyse_factory() if analyse_factory else pipeline.make_real_analyse()

        captured = io.StringIO()
        logging.disable(logging.CRITICAL)
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                result = run_batch(tickers, state, stages, real_analyse, today=today,
                                   limit=limit, warnings=warnings)
        finally:
            logging.disable(logging.NOTSET)

        runs_dir = Path(config.BATCH_RUNS_DIR)
        files = [(state_path, dump_state(result.state)),
                 (runs_dir / f"{today.isoformat()}.json", result.summary_json()),
                 (runs_dir / f"{today.isoformat()}.csv", result.ranked_csv()),
                 (runs_dir / f"{today.isoformat()}.log", captured.getvalue() or "(no engine output)\n")]
        pushed_ok = 0
        for path, text in files:
            _write_atomic(path, text)
            outcome = pipeline.push_local(path)
            if outcome is not None and outcome.ok:
                pushed_ok += 1
        lines = result.public_lines()
        lines.append(f"Backup of the counters, summary and log: {pushed_ok} of {len(files)} ok")
        return (0 if pushed_ok == len(files) else 1), lines
    except Exception as exc:        # never print the message: it could contain a ticker name
        return 2, [f"Batch stopped: unexpected {type(exc).__name__}"]


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Overnight batch for the Equity Growth Analyst.")
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N tickers of batch/tickers.csv (for a trial run)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be 1 or more")
    return args


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    code, lines = run_live(limit=args.limit)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
