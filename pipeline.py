"""
pipeline.py
Version: Step 12, v0.12.0 (2026-11-07). Code version 2026-11-step12.
Spec: README v3.1 DRAFT, sections 4, 5, 8 and 10.

Step 11b: added the manual-input helpers (manual_status, save_manual_input,
ensure_local_manual_inputs) and made restore_from_backup also bring back manual_inputs/.
Step 11c: the manual_inputs stage is now real. It reads the saved manual values from the local
store (no network), keeps only fresh, non-blank ones in "values", and records every field's
status in the ticker result so the details panel can show them.
Step 12: the score stage now uses them (approved 2026-11-07). A fresh, non-blank promoter pledge
feeds the scorer's existing pledge penalty; FII and DII holding changes, only when BOTH are fresh,
are summed into the scorer's existing institutional-holding signal. score_card.py is unchanged, so
no weight, cap or threshold changed; the 69 cap is not lifted by a manual pledge. The saved record
keeps the raw fetched financials; the manual values are applied to a copy used only for scoring.
PROMPT_VERSION: unchanged (the prompt, schema and Gemini input are the same).

Purpose: the orchestrator. Sequences the per-ticker stages, handles failures,
ranks the results and returns plain data. It contains no scoring maths, no
data fetching and no UI code.

Status: every stage is a STUB except the store (step 2), the scorer (step 3), the
filings reader (step 4), and the data fetch and AI analysis for non-demo tickers
(steps 7 and 8). Real modules replace
the stubs one step at a time by passing a different `Stages` object; nothing
else changes.

Rules implemented here (README 5.2):
  - A stage failure is recorded per ticker; one bad ticker never stops a run.
  - Transient error (429/503) in the analysis: keep the stored prior analysis,
    status "stale", re-score from it, still rank it with a visible marker.
  - No prior analysis: status "partial" with no LLM component.
  - Permanent error: status "failed", listed but not ranked.
  - The store is written only when a ticker finishes with status "ok", so a
    transient error can never overwrite or delete a prior analysis.
  - Progress goes only through the on_progress(ticker, stage, state) callback.
"""
from __future__ import annotations

import copy
import glob
import io
import os
import re
import threading
import time
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

import config
import data_fetch
import filings
import gemini_analyser
import manual_inputs_store
import pdf_extractor
import persistence
import report_generator
import score_card
from errors import PermanentError, TransientError
from rate_limiter import RateLimiter
from store import Store

ProgressCallback = Callable[[str, str, str], None]

STAGE_NAMES = (
    "fetch", "indicators", "manual_inputs", "filings_context",
    "analyse", "score", "save",
)

# Higher number = worse. A status is only ever downgraded, never upgraded.
_SEVERITY = {
    config.STATUS_OK: 0,
    config.STATUS_STALE: 1,
    config.STATUS_PARTIAL: 2,
    config.STATUS_FAILED: 3,
}


# ------------------------------------------------------------ data classes
@dataclass
class ScoreBreakdown:
    long_term: Optional[float] = None
    short_term: Optional[float] = None
    total: Optional[float] = None
    components: dict = field(default_factory=dict)  # name -> points, max, coverage
    caps_applied: list = field(default_factory=list)
    gates: dict = field(default_factory=dict)
    coverage_pct: Optional[float] = None
    horizon_days: int = 0
    labels: list = field(default_factory=list)
    spec_version: str = config.SPEC_VERSION
    prompt_version: str = config.PROMPT_VERSION
    schema_version: int = config.SCHEMA_VERSION


@dataclass
class TickerResult:
    ticker: str
    status: str
    stage_errors: dict = field(default_factory=dict)  # stage -> message
    breakdown: Optional[ScoreBreakdown] = None
    stubbed_stages: list = field(default_factory=list)
    prior_used: bool = False
    documents: list = field(default_factory=list)   # filings read for this ticker
    unreadable: list = field(default_factory=list)  # [name, reason] for filings that could not be read
    financials: dict = field(default_factory=dict)  # the data used (for the report and details view)
    analysis: Optional[dict] = None                 # the AI analysis used (fresh or the prior one)
    manual_inputs: dict = field(default_factory=dict)  # saved manual values seen by the run (shown; pledge and FII+DII are scored since step 12)
    rank: Optional[int] = None
    schema_version: int = config.SCHEMA_VERSION


@dataclass
class RunResult:
    results: list
    horizon_days: int
    started_at: str
    finished_at: str
    notices: list
    code_version: str = config.CODE_VERSION
    spec_version: str = config.SPEC_VERSION
    prompt_version: str = config.PROMPT_VERSION

    def to_rows(self) -> list[dict]:
        """Flat rows for a table or CSV. Missing numbers stay None (blank)."""
        rows = []
        for r in self.results:
            b = r.breakdown
            notes = list(b.labels) if b else []
            notes += [f"{stage}: {msg}" for stage, msg in r.stage_errors.items()]
            rows.append({
                "Rank": r.rank,
                "Ticker": r.ticker,
                "Status": r.status,
                "Total": b.total if b else None,
                "Rating": (score_card.rating_for_score(b.total)
                           if b and b.total is not None else None),
                "Filings": len(r.documents),
                "Long-term": b.long_term if b else None,
                "Short-term": b.short_term if b else None,
                "Coverage %": b.coverage_pct if b else None,
                "Notes": "; ".join(notes),
            })
        return rows


@dataclass
class Stages:
    """The replaceable stage functions. Real modules plug in here."""
    fetch: Callable[[str], dict]                    # -> {"financials", "price_history"}
    indicators: Callable[[dict], dict]              # price_history -> indicators
    manual_inputs: Callable[[str], dict]            # ticker -> manual inputs
    filings_context: Callable[[str], dict]          # ticker -> context text
    analyse: Callable[[str, dict, dict], dict]      # ticker, context, financials -> analysis
    score: Callable[..., ScoreBreakdown]            # ticker, analysis, financials, indicators, manual, horizon
    load_prior: Callable[[str], Optional[dict]]     # ticker -> stored record or None
    save: Callable[[str, dict], None]               # ticker, record -> None
    backup: Optional[Callable[[str], Any]] = None   # ticker -> persistence.OpResult or None


# ------------------------------------------------------------ ticker input
def normalise_tickers(raw: str | Iterable[str],
                      max_count: int = config.GUI_MAX_TICKERS) -> tuple[list[str], list[tuple[str, str]]]:
    """Split, upper-case, de-duplicate and validate tickers.

    Returns (valid, rejected) where rejected is a list of (token, reason).
    """
    tokens = re.split(r"[,\s;]+", raw) if isinstance(raw, str) else list(raw)
    valid: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token in tokens:
        token = (token or "").strip().upper()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        if not config.TICKER_PATTERN.match(token):
            rejected.append((token, "invalid characters or length"))
        elif len(valid) >= max_count:
            rejected.append((token, f"over the limit of {max_count} tickers"))
        else:
            valid.append(token)
    return valid, rejected


# ------------------------------------------------------------------- stubs
def _stub(fn):
    fn.is_stub = True
    return fn


DEMO_PRIOR = {"analysis": {"stub": True, "note": "seeded prior analysis"},
              "saved_at": "seeded"}


class StubStore:
    """In-memory store for tests. Seeded with one prior record (STALE429)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: dict[str, dict] = {"STALE429": copy.deepcopy(DEMO_PRIOR)}

    def load(self, ticker: str) -> Optional[dict]:
        with self._lock:
            rec = self.records.get(ticker)
            return dict(rec) if rec else None

    def save(self, ticker: str, record: dict) -> None:
        with self._lock:
            self.records[ticker] = dict(record)


# Demo hooks so the failure rules can be seen working. Remove at step 5.
SIM_TRANSIENT = {"STALE429", "NOPRIOR429"}
SIM_PERMANENT = {"FAILPERM"}
# These four tickers always use the stub analysis so the demo table never calls Gemini.
DEMO_TICKERS = frozenset({"FAKE", "STALE429", "NOPRIOR429", "FAILPERM"})

# One limiter shared by every analysis in this process (RATE_LIMIT_PER_MINUTE calls a minute).
_LIMITER = RateLimiter(config.RATE_LIMIT_PER_MINUTE)


def make_real_fetch(fetch_fn=None):
    """The fetch stage backed by data_fetch. fetch_fn is for tests.

    No data at all, or data quality "insufficient", is a PermanentError: a stock is not scored
    on almost nothing (this follows the old orchestrator). Partial data is accepted and the
    missing fields are listed in the result.
    """
    def fetch(ticker: str) -> dict:
        record = (fetch_fn or data_fetch.get_financial_data)(ticker)
        if not record:
            raise PermanentError("no financial data from any provider")
        if record.get("data_quality") == "insufficient":
            raise PermanentError("insufficient fundamental data to score reliably")
        return {"financials": record, "price_history": {}}
    return fetch


def make_real_analyse(generate_fn=None, limiter=None, sleep=time.sleep, cache_dir=None):
    """The analyse stage backed by gemini_analyser. generate_fn is for tests; the default
    builds the real Gemini call from the GEMINI_API_KEY secret."""
    def analyse(ticker: str, context: dict, financials: dict) -> dict:
        ctx = context if isinstance(context, dict) else {}
        return gemini_analyser.analyse_company(
            ticker, financials, ctx.get("text", ""), ctx.get("meta", {}),
            generate_fn=generate_fn, limiter=limiter or _LIMITER, sleep=sleep, cache_dir=cache_dir)
    return analyse


def _manual_num(v: Any) -> Optional[float]:
    """A finite number from a manual value, or None (booleans and text are not numbers)."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return None if f != f or f in (float("inf"), float("-inf")) else f


def apply_manual_inputs(financials: Optional[dict], manual: Any) -> tuple[dict, list]:
    """Return (financials copy for scoring, labels) with the saved manual values applied.

    Step 12. Rules (approved 2026-11-07):
      - promoter_pledge_pct (fresh, non-blank) fills the scorer's pledge key. The scorer's own
        penalty applies (above 10 percent). A manual value replaces a fetched one.
      - fii_holding_change + dii_holding_change fill institutional_holding_change (percentage
        points), only when BOTH are fresh and non-blank. One alone is not counted.
      - Blank, missing or older-than-limit values change nothing. An expired value is named in a
        label so a lapsed pledge does not silently raise the score.
      - next_result_date is not scored.
    The input dict is never changed; with no usable manual values the copy equals the input.
    """
    fin = dict(financials or {})
    labels: list = []
    summary = manual if isinstance(manual, dict) else {}
    values = summary.get("values") or {}
    fields = summary.get("fields") or {}
    limit = config.MANUAL_INPUT_MAX_AGE_DAYS

    def as_of(name: str) -> str:
        return str((fields.get(name) or {}).get("as_of") or "date not recorded")

    def expired(name: str) -> bool:
        f = fields.get(name) or {}
        return f.get("status") == "stale" and not f.get("blank")

    # promoter pledge
    pledge = _manual_num(values.get("promoter_pledge_pct"))
    if pledge is not None and 0.0 <= pledge <= 100.0:
        fetched = _manual_num(fin.get("promoter_pledge_pct"))
        fin["promoter_pledge_pct"] = pledge
        text = f"Manual input counted: promoter pledge {pledge:g}% (as of {as_of('promoter_pledge_pct')})"
        if fetched is not None and fetched != pledge:
            text += f"; replaces fetched value {fetched:g}%"
        labels.append(text)
    elif expired("promoter_pledge_pct"):
        labels.append(f"Manual promoter pledge is older than {limit} days (as of "
                      f"{as_of('promoter_pledge_pct')}): not counted")

    # institutional holding change = FII + DII, both required
    fii = _manual_num(values.get("fii_holding_change"))
    dii = _manual_num(values.get("dii_holding_change"))
    if fii is not None and dii is not None:
        total = fii + dii
        fin["institutional_holding_change"] = total
        labels.append(f"Manual input counted: institutional holding change {total:+g}pp "
                      f"(FII {fii:+g} + DII {dii:+g}; as of {as_of('fii_holding_change')} "
                      f"and {as_of('dii_holding_change')})")
    elif fii is not None or dii is not None:
        labels.append("Manual FII/DII not counted: both must be fresh and filled in")
    elif expired("fii_holding_change") or expired("dii_holding_change"):
        labels.append(f"Manual FII/DII is older than {limit} days: not counted")
    return fin, labels


def score_stage_v21(ticker, analysis, financials, indicators, manual,
                    horizon_days) -> ScoreBreakdown:
    """Score stage: runs the recovered v2.1 quality and governance scorer.

    The scorer's output is mapped onto ScoreBreakdown. The 55/45 fields stay
    None: v2.1 has no short-term timing layer (see the Price-Impact score, later).
    Peers are not passed, so the valuation signal scores at the prior until a
    batch supplies peer statistics.
    """
    scoring_fin, manual_labels = apply_manual_inputs(financials, manual)
    result = score_card.compute_score(scoring_fin, analysis or {}, peers=None)
    bd = result["breakdown"]
    labels = [f"Quality and governance score, model {bd['scoring_model']} (0 to 100)"]
    labels.extend(manual_labels)
    if analysis is None:
        labels.append("No LLM analysis: LLM component scored at the prior")
    fin = financials or {}
    if fin.get("data_quality"):
        providers = ", ".join(fin.get("providers_used") or []) or "none"
        missing = len(fin.get("missing_fields") or [])
        warns = len(fin.get("warnings") or [])
        labels.append(f"Data quality: {fin['data_quality']} (providers: {providers}; "
                      f"{missing} core field(s) missing; {warns} warning(s))")
    if fin.get("stub"):
        labels.append("STUB data: every signal scored at the prior")
    if getattr(config, "UNVERIFIED_GOVERNANCE_CAP", None) is None:
        labels.append("Unverified-governance cap is OFF (value not recovered)")
    caps = [n for n in bd["governance_notes"] if n.startswith("Capped at")]
    components = {
        "llm_component": {"points": bd["llm_component"], "max": 60.0},
        "fin_level": {"points": bd["fin_level"], "max": 20.0},
        "scoring_model": bd["scoring_model"],
        "financial_bonus": bd["financial_bonus"],
        "fin_direction": {"points": bd["fin_direction"], "max": 14.0},
        "fin_valuation": {"points": bd["fin_valuation"], "max": 6.0},
        "governance_penalty": {"points": bd["governance_penalty"]},
        "raw_score": bd["raw_score"],
        "llm_coverage": bd["llm_coverage"],
        "notes": {
            "factor_notes": bd["factor_notes"],
            "financial_notes": bd["financial_notes"],
            "governance_notes": bd["governance_notes"],
        },
    }
    return ScoreBreakdown(
        long_term=None, short_term=None, total=result["score"],
        components=components, caps_applied=caps, gates={},
        coverage_pct=None, horizon_days=horizon_days, labels=labels,
    )


def filings_context_stage(ticker: str) -> dict:
    """Filings stage: read TICKER_*.pdf (or TICKER.pdf) and build the LLM context.

    Returns plain data: {"text", "meta", "documents", "unreadable"}. With no filings
    the text is empty and meta says so. Unreadable files are listed, never raised.
    """
    ensure_local_filings(ticker)
    folder = config.FILINGS_FOLDER
    paths = glob.glob(os.path.join(folder, f"{glob.escape(ticker)}_*.pdf"))
    exact = os.path.join(folder, f"{ticker}.pdf")
    if os.path.exists(exact) and exact not in paths:
        paths.append(exact)
    paths.sort(reverse=True)  # newest-dated names first

    documents, unreadable = [], []
    for path in paths:
        doc = pdf_extractor.extract_document(path)
        if doc.get("pages"):
            documents.append(doc)
        else:
            unreadable.append((doc.get("document"), doc.get("error") or "no extractable text (scanned?)"))

    if documents:
        text, meta = pdf_extractor.build_context(documents, max_chars=config.MAX_PROMPT_CHARS)
    else:
        text, meta = pdf_extractor.build_context([], max_chars=config.MAX_PROMPT_CHARS)
    return {"text": text, "meta": meta,
            "documents": [d["document"] for d in documents], "unreadable": unreadable}


# ----------------------------------------------------- permanent storage
_MIRROR: Any = None
_MIRROR_READY = False
_MIRROR_LOCK = threading.Lock()


def get_mirror():
    """The private-repository mirror, or None when permanent storage is not set up."""
    global _MIRROR, _MIRROR_READY
    with _MIRROR_LOCK:
        if not _MIRROR_READY:
            _MIRROR = persistence.make_mirror()
            _MIRROR_READY = True
        return _MIRROR


def set_mirror(mirror: Any) -> None:
    """Install a mirror (used by tests)."""
    global _MIRROR, _MIRROR_READY
    with _MIRROR_LOCK:
        _MIRROR, _MIRROR_READY = mirror, True


def reset_mirror() -> None:
    global _MIRROR, _MIRROR_READY
    with _MIRROR_LOCK:
        _MIRROR, _MIRROR_READY = None, False


def _rel_to_data(path) -> Optional[str]:
    try:
        return Path(path).resolve().relative_to(Path(config.DATA_DIR).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def push_local(path):
    """Copy one local file to permanent storage. Returns an OpResult, or None when it is off."""
    mirror = get_mirror()
    if mirror is None:
        return None
    rel = _rel_to_data(path)
    if rel is None:
        return persistence.OpResult(False, "refused", "file is outside the data folder", str(path))
    return mirror.push_file(rel)


def backup_note(res) -> str:
    if res is None:
        return ""
    return "backed up" if res.ok else f"not backed up: {res.message}"


def ensure_local_filings(ticker: str) -> None:
    """Fetch this ticker's backed-up filings that are missing locally. Never raises."""
    mirror = get_mirror()
    if mirror is None:
        return
    try:
        mirror.pull_ticker_filings(ticker)
    except Exception:
        pass


def restore_from_backup():
    """Bring back saved analysis records after a restart. None when storage is off."""
    mirror = get_mirror()
    if mirror is None:
        return None
    try:
        return mirror.pull_missing(("store/", "manual_inputs/"))
    except Exception as exc:
        return persistence.PullResult(errors=[f"{type(exc).__name__}: {exc}"])


def storage_status() -> dict:
    mirror = get_mirror()
    if mirror is None:
        return {"enabled": False, "repo": None, "ok": 0, "failed": 0, "last_error": None}
    return {"enabled": True, "repo": mirror.repo, **mirror.log.summary()}


# ------------------------------------------------------------------ filings
FILING_TYPE_LABELS = list(filings.FILING_TYPES)


def upload_filing(ticker: str, doc_date, type_label: str, data: bytes):
    """Save one uploaded filing and copy it to permanent storage.

    Raises FilingError on refusal. The result's backup_note says whether the copy worked.
    """
    result = filings.save_filing(ticker, doc_date, type_label, data)
    result.backup_note = backup_note(push_local(result.path))
    return result


def stored_filings(ticker: str):
    ensure_local_filings(ticker)
    return filings.list_filings(ticker)


def delete_filing(ticker: str, name: str) -> bool:
    """Remove a filing locally and, when storage is on, from permanent storage (explicit user action)."""
    existed = filings.remove_filing(ticker, name)
    mirror = get_mirror()
    if mirror is not None:
        rel = _rel_to_data(Path(config.FILINGS_FOLDER) / name)
        if rel is not None:
            mirror.delete_file(rel)
    return existed


def backup_store_record(store, ticker: str):
    """Backup stage for the real file store: copy the ticker's record."""
    return push_local(store.path_for(ticker))


# ------------------------------------------------------------ manual inputs
InputRejected = manual_inputs_store.InputRejected


@dataclass
class ManualSaveResult:
    entry: Any                 # the saved manual_inputs_store.ManualInput
    backup_note: str = ""      # "", "backed up" or "not backed up: <reason>"


def default_manual_store() -> manual_inputs_store.ManualInputStore:
    """The real file-backed store for manual inputs (step 11a)."""
    return manual_inputs_store.ManualInputStore(config.MANUAL_INPUTS_DIR)


def ensure_local_manual_inputs() -> None:
    """Fetch backed-up manual inputs and their source documents that are missing locally.
    Never raises; local files are never overwritten."""
    mirror = get_mirror()
    if mirror is None:
        return
    try:
        mirror.pull_missing(("manual_inputs/",))
    except Exception:
        pass


def manual_status(ticker: str, today=None) -> list:
    """One plain row per manual field for the Inputs tab.

    Keys: field, label, kind, value (None when missing or blank), blank (bool), as_of,
    source_file, status ("missing", "fresh" or "stale"). May raise StoreError (damaged file) or
    InputRejected (invalid ticker); the caller shows the message.
    """
    store = default_manual_store()
    entries = store.get(ticker)
    rows = []
    for name, spec in config.MANUAL_FIELDS.items():
        entry = entries.get(name)
        rows.append({
            "field": name, "label": spec["label"], "kind": spec["kind"],
            "value": entry.value if entry else None,
            "blank": bool(entry is not None and entry.value is None),
            "as_of": entry.as_of if entry else None,
            "source_file": entry.source_file if entry else None,
            "status": store.freshness(entry, today),
        })
    return rows


def save_manual_input(ticker: str, field: str, value: Any, as_of: Any, blank: bool = False,
                      source_name: Optional[str] = None, source_bytes: Optional[bytes] = None,
                      today=None) -> ManualSaveResult:
    """Save one manual input (and its source document) locally, then copy them to permanent storage.

    Raises InputRejected (nothing changed) or StoreError. A failed copy never undoes the save:
    the result's backup_note says "not backed up: ..." instead.
    """
    store = default_manual_store()
    entry = store.save(ticker, field, value, as_of, blank=blank, source_name=source_name,
                       source_bytes=source_bytes, today=today)
    results = [push_local(store.path_for(ticker))]
    if entry.source_file:
        results.append(push_local(store.sources_dir / entry.source_file))
    failed = [r for r in results if r is not None and not r.ok]
    if failed:
        return ManualSaveResult(entry, f"not backed up: {failed[0].message}")
    if all(r is None for r in results):
        return ManualSaveResult(entry, "")
    return ManualSaveResult(entry, "backed up")


def manual_inputs_stage(ticker: str, today=None) -> dict:
    """Pipeline stage: the saved manual values for this ticker, read from the local store only.

    Returns {"fields": {name: {status, blank, value, as_of}}, "values": {name: value}, "warning"}.
    "values" holds only fresh, non-blank entries. A damaged or unreadable file is reported in
    "warning" and the run carries on; this stage never makes a ticker partial or failed.
    """
    store = default_manual_store()
    try:
        entries = store.get(ticker)
    except Exception as exc:  # StoreError for a damaged file, InputRejected for a bad ticker
        return {"fields": {}, "values": {}, "warning": f"manual inputs not read: {type(exc).__name__}: {exc}"}
    fields: dict = {}
    values: dict = {}
    for name in config.MANUAL_FIELDS:
        entry = entries.get(name)
        status = store.freshness(entry, today)
        fields[name] = {"status": status,
                        "blank": bool(entry is not None and entry.value is None),
                        "value": entry.value if entry is not None else None,
                        "as_of": entry.as_of if entry is not None else None}
        if status == "fresh" and entry is not None and entry.value is not None:
            values[name] = entry.value
    return {"fields": fields, "values": values, "warning": None}


def manual_summary_lines(result: "TickerResult") -> list:
    """Plain text lines for the details panel, one per manual field (in config order)."""
    summary = getattr(result, "manual_inputs", None) or {}
    lines = []
    if summary.get("warning"):
        lines.append(str(summary["warning"]))
    fields = summary.get("fields") or {}
    if not fields:
        if not lines:
            lines.append("Not read (the run stopped before this step)")
        return lines
    limit = config.MANUAL_INPUT_MAX_AGE_DAYS
    for name, spec in config.MANUAL_FIELDS.items():
        f = fields.get(name) or {}
        label = spec["label"]
        status, as_of = f.get("status", "missing"), f.get("as_of")
        if status == "missing":
            lines.append(f"{label}: not entered")
        elif status == "stale":
            what = "blank" if f.get("blank") else f"{f.get('value'):g}" if isinstance(f.get("value"), float) else str(f.get("value"))
            lines.append(f"{label}: {what}, last as of {as_of}; older than {limit} days, not used")
        elif f.get("blank"):
            lines.append(f"{label}: left blank (as of {as_of})")
        else:
            v = f.get("value")
            lines.append(f"{label}: {v:g}" + f" (as of {as_of})" if isinstance(v, float) else f"{label}: {v} (as of {as_of})")
    return lines


# ----------------------------------------------------------------- reports
def _legacy_breakdown(b: dict) -> dict:
    """Rebuild the v2.1 scorer's breakdown dict (what report_generator reads) from a saved
    ScoreBreakdown (as a dict). No numbers are recomputed."""
    c = b.get("components", {}) or {}

    def pts(key):
        return (c.get(key) or {}).get("points", 0)

    notes = c.get("notes", {}) or {}
    return {
        "scoring_model": c.get("scoring_model", "v2.1"),
        "llm_component": pts("llm_component"),
        "financial_bonus": c.get("financial_bonus", round(pts("fin_level") + pts("fin_direction") + pts("fin_valuation"), 1)),
        "fin_level": pts("fin_level"), "fin_direction": pts("fin_direction"), "fin_valuation": pts("fin_valuation"),
        "governance_penalty": pts("governance_penalty"),
        "raw_score": c.get("raw_score"),
        "llm_coverage": c.get("llm_coverage") or 0,
        "factor_notes": notes.get("factor_notes", []),
        "financial_notes": notes.get("financial_notes", []),
        "governance_notes": notes.get("governance_notes", []),
    }


def _report_html(ticker, financials, analysis, breakdown: dict, has_filing: bool, meta: dict) -> Optional[str]:
    total = breakdown.get("total")
    if total is None:
        return None
    score_result = {"score": total, "breakdown": _legacy_breakdown(breakdown)}
    return report_generator.create_html_report(ticker, financials or {}, analysis or {}, score_result,
                                               has_filing=has_filing, meta=meta)


def build_report(result: "TickerResult") -> Optional[str]:
    """The per-stock HTML report for a run result, or None if the ticker was not scored.
    Reports for stale or partial results use the prior or missing analysis; the recovered
    report format has no marker for that, so the app labels them."""
    if result.breakdown is None or result.breakdown.total is None or result.status == config.STATUS_FAILED:
        return None
    meta = {"financial_as_of_date": (result.financials or {}).get("as_of_date")}
    if result.status == config.STATUS_OK:
        meta["analysis_date"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta["cache_hit"] = bool((result.analysis or {}).get("from_cache"))
    return _report_html(result.ticker, result.financials, result.analysis, asdict(result.breakdown),
                        bool(result.documents), meta)


def report_from_record(ticker: str, record: dict) -> Optional[str]:
    """Rebuild a report from a saved record, without calling any provider or Gemini."""
    breakdown = (record or {}).get("breakdown") or {}
    analysis = (record or {}).get("analysis") or {}
    meta = {"analysis_date": (record or {}).get("saved_at"),
            "cache_hit": bool(analysis.get("from_cache")),
            "financial_as_of_date": ((record or {}).get("financials") or {}).get("as_of_date")}
    return _report_html(ticker, (record or {}).get("financials"), analysis, breakdown,
                        bool(analysis.get("documents_used")), meta)


def build_dashboard(results: list) -> str:
    """The ranked dashboard page (links point at TICKER.html next to it)."""
    rows = []
    for r in results:
        b = r.breakdown
        if b is None or b.total is None or r.status == config.STATUS_FAILED:
            continue
        rows.append({"Ticker": r.ticker, "Score": b.total, "Rating": score_card.rating_for_score(b.total),
                     "Has_Filing": bool(r.documents), "Report": f"{r.ticker}.html"})
    return report_generator.create_portfolio_report(rows)


def build_reports_zip(results: list) -> bytes:
    """Dashboard plus one report per scored ticker, in a zip so the dashboard links work."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Portfolio_Dashboard.html", build_dashboard(results))
        for r in results:
            html = build_report(r)
            if html:
                zf.writestr(f"{r.ticker}.html", html)
    return buffer.getvalue()


def data_notes(result: "TickerResult") -> dict:
    """Plain facts for the details view: where the data came from and what went wrong."""
    fin = result.financials or {}
    return {
        "providers": list(fin.get("providers_used") or []),
        "data_quality": fin.get("data_quality"),
        "missing_fields": list(fin.get("missing_fields") or []),
        "warnings": [str(w) for w in (fin.get("warnings") or [])],
        "conflicts": fin.get("conflicts") or [],
        "governance_sources": list(fin.get("governance_sources") or []),
        "filings_read": list(result.documents),
        "filings_unreadable": list(result.unreadable),
        "stage_errors": dict(result.stage_errors),
    }


def saved_tickers() -> list:
    try:
        return default_store().list_tickers()
    except Exception:
        return []


def load_saved(ticker: str):
    """A saved record (after restoring from permanent storage), or None. Never raises."""
    ensure_local_store_record(ticker)
    try:
        return default_store().load(ticker)
    except Exception:
        return None


def ensure_local_store_record(ticker: str) -> None:
    mirror = get_mirror()
    if mirror is None:
        return
    try:
        mirror.pull_missing(("store/",))
    except Exception:
        pass


def default_store() -> Store:
    """The real file-backed store (step 2)."""
    return Store(config.STORE_DIR)


def seed_demo_prior(store: Any) -> None:
    """Make sure the STALE429 demo ticker has a stored prior. Remove at step 5."""
    try:
        if store.load("STALE429") is None:
            store.save("STALE429", copy.deepcopy(DEMO_PRIOR))
    except Exception:
        pass  # demo scaffolding only; it must never break a run


def make_stub_stages(store: Any = None, analyse_fn: Any = None,
                     fetch_fn: Any = None) -> tuple[Stages, Any]:
    """Stub stages. With store=None an in-memory StubStore is used (tests).
    Pass default_store() to use the real file-backed store. The four demo tickers use the
    stub analysis and stub data; every other ticker uses analyse_fn / fetch_fn, or the real
    Gemini analysis and the real data fetch by default."""
    use_memory_store = store is None
    if use_memory_store:
        store = StubStore()
    else:
        seed_demo_prior(store)

    def stub_fetch(ticker: str) -> dict:
        return {"financials": {"ticker": ticker, "stub": True},
                "price_history": {"ticker": ticker, "bars": [], "stub": True}}

    real_fetch = make_real_fetch(fetch_fn)

    def fetch(ticker: str) -> dict:
        if ticker in DEMO_TICKERS:
            return stub_fetch(ticker)
        return real_fetch(ticker)

    @_stub
    def indicators(price_history: dict) -> dict:
        return {"stub": True}

    def stub_analyse(ticker: str, context: dict, financials: dict) -> dict:
        if ticker in SIM_TRANSIENT:
            raise TransientError("simulated 429 from the analyser")
        if ticker in SIM_PERMANENT:
            raise PermanentError("simulated permanent analyser failure")
        return {"stub": True, "note": "no real analysis yet"}

    real_analyse = analyse_fn or make_real_analyse()

    def analyse(ticker: str, context: dict, financials: dict) -> dict:
        if ticker in DEMO_TICKERS:
            return stub_analyse(ticker, context, financials)
        return real_analyse(ticker, context, financials)

    if use_memory_store:
        @_stub
        def load_prior(ticker: str) -> Optional[dict]:
            return store.load(ticker)

        @_stub
        def save(ticker: str, record: dict) -> None:
            store.save(ticker, record)
    else:
        load_prior, save = store.load, store.save  # real store: not marked as stubs

    backup = None
    if not use_memory_store and hasattr(store, "path_for"):
        backup = lambda ticker: backup_store_record(store, ticker)  # noqa: E731

    stages = Stages(
        fetch=fetch, indicators=indicators, manual_inputs=manual_inputs_stage,
        filings_context=filings_context_stage, analyse=analyse, score=score_stage_v21,
        load_prior=load_prior, save=save, backup=backup,
    )
    return stages, store


# -------------------------------------------------------- per-ticker logic
def _downgrade(current: str, new: str) -> str:
    return new if _SEVERITY[new] > _SEVERITY[current] else current


def process_ticker(ticker: str, horizon_days: int, stages: Stages,
                   on_progress: Optional[ProgressCallback] = None) -> TickerResult:
    errors: dict[str, str] = {}
    stubbed: list[str] = []
    status = config.STATUS_OK
    prior_used = False
    documents_read: list = []
    unreadable_files: list = []
    financials: dict = {}
    analysis: Optional[dict] = None
    manual_summary: dict = {}

    def notify(stage: str, state: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(ticker, stage, state)
        except Exception:
            pass  # a broken callback must never break a run

    def run(stage: str, fn: Callable, *args: Any) -> tuple[Any, Optional[str]]:
        """Run one stage. Returns (output, error_kind); error_kind is None on success."""
        notify(stage, "start")
        if getattr(fn, "is_stub", False) and stage not in stubbed:
            stubbed.append(stage)
        try:
            out = fn(*args)
        except TransientError as exc:
            errors[stage] = f"transient: {exc}"
            notify(stage, "transient_error")
            return None, "transient"
        except PermanentError as exc:
            errors[stage] = f"permanent: {exc}"
            notify(stage, "permanent_error")
            return None, "permanent"
        except Exception as exc:  # unexpected: treated as permanent for this ticker
            errors[stage] = f"unexpected {type(exc).__name__}: {exc}"
            notify(stage, "error")
            return None, "permanent"
        notify(stage, "done")
        return out, None

    def failed() -> TickerResult:
        return TickerResult(ticker=ticker, status=config.STATUS_FAILED,
                            stage_errors=errors, breakdown=None,
                            stubbed_stages=stubbed, prior_used=prior_used,
                            documents=documents_read, unreadable=unreadable_files,
                            financials=financials, analysis=analysis,
                            manual_inputs=manual_summary)

    # 1. fetch: without data nothing else can run
    fetched, err = run("fetch", stages.fetch, ticker)
    if err or not isinstance(fetched, dict):
        if not err:
            errors["fetch"] = "permanent: fetch returned no data"
        return failed()
    financials = fetched.get("financials", {})
    price_history = fetched.get("price_history", {})

    # 2. indicators
    indicators, err = run("indicators", stages.indicators, price_history)
    if err:
        indicators = {}
        status = _downgrade(status, config.STATUS_PARTIAL)

    # 3. manual inputs
    manual, err = run("manual_inputs", stages.manual_inputs, ticker)
    if err:
        manual = {}
        status = _downgrade(status, config.STATUS_PARTIAL)
    manual_summary = manual if isinstance(manual, dict) else {}

    # 4. filings context
    context, ctx_err = run("filings_context", stages.filings_context, ticker)
    if isinstance(context, dict):
        documents_read = list(context.get("documents", []))
        unreadable_files = [[str(n), str(r)] for n, r in (context.get("unreadable") or [])]

    # 5. analysis (needs context); fall back to the stored prior if it cannot run
    analysis: Optional[dict] = None
    fallback_wanted = False
    if ctx_err:
        fallback_wanted = True
    else:
        analysis, a_err = run("analyse", stages.analyse, ticker, context, financials)
        if a_err == "permanent":
            return failed()
        if a_err == "transient":
            fallback_wanted = True

    if fallback_wanted:
        prior, p_err = run("load_prior", stages.load_prior, ticker)
        if prior and isinstance(prior, dict) and prior.get("analysis") is not None:
            analysis = prior["analysis"]
            prior_used = True
            status = _downgrade(status, config.STATUS_STALE)
        else:
            analysis = None
            status = _downgrade(status, config.STATUS_PARTIAL)

    # 6. score (re-scores from the prior analysis in the stale case)
    breakdown, err = run("score", stages.score, ticker, analysis, financials,
                         indicators, manual, horizon_days)
    if err or not isinstance(breakdown, ScoreBreakdown):
        if not err:
            errors["score"] = "permanent: score did not return a ScoreBreakdown"
        return failed()
    breakdown = replace(breakdown, labels=list(breakdown.labels))
    if status == config.STATUS_STALE:
        breakdown.labels.append("STALE: prior analysis reused after a transient error")
    if status == config.STATUS_PARTIAL:
        breakdown.labels.append("PARTIAL: some inputs or the LLM component are missing")

    # 7. save, only for fully successful tickers
    if status == config.STATUS_OK:
        record = {
            "analysis": analysis,
            "financials": financials,
            "indicators": indicators,
            "breakdown": asdict(breakdown),
            "prompt_version": config.PROMPT_VERSION,
            "spec_version": config.SPEC_VERSION,
            "schema_version": config.SCHEMA_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        _, err = run("save", stages.save, ticker, record)
        if err:
            status = _downgrade(status, config.STATUS_PARTIAL)
            breakdown.labels.append("PARTIAL: result could not be saved")
        elif stages.backup is not None:
            # Copy to permanent storage. A failed backup never changes the status.
            notify("backup", "start")
            try:
                res = stages.backup(ticker)
            except Exception as exc:
                res = persistence.OpResult(False, "failed", f"{type(exc).__name__}: {exc}")
            if res is not None:
                notify("backup", "done" if res.ok else "error")
                if not res.ok:
                    breakdown.labels.append(f"NOT BACKED UP: {res.message}")

    return TickerResult(ticker=ticker, status=status, stage_errors=errors,
                        breakdown=breakdown, stubbed_stages=stubbed,
                        prior_used=prior_used, documents=documents_read,
                        unreadable=unreadable_files, financials=financials, analysis=analysis,
                        manual_inputs=manual_summary)


# ----------------------------------------------------------------- ranking
def rank_results(results: list[TickerResult]) -> list[TickerResult]:
    """Rank by total (desc), then long-term (desc), then coverage (desc).

    Stale and partial results are ranked but labelled. Failed results are
    listed after the ranked ones with no rank. Tickers with no total are
    ranked after those with one, in input order.
    """
    ranked = [r for r in results if r.status != config.STATUS_FAILED]
    failed = [r for r in results if r.status == config.STATUS_FAILED]

    def key(r: TickerResult):
        b = r.breakdown
        total = b.total if b else None
        long_term = b.long_term if b and b.long_term is not None else float("-inf")
        coverage = b.coverage_pct if b and b.coverage_pct is not None else float("-inf")
        return (total is None, -(total if total is not None else 0.0), -long_term, -coverage)

    ranked.sort(key=key)  # stable: ties keep input order
    for i, r in enumerate(ranked, start=1):
        r.rank = i
    for r in failed:
        r.rank = None
    return ranked + failed


# --------------------------------------------------------------------- run
def run_pipeline(tickers: list[str], horizon_days: int,
                 stages: Optional[Stages] = None,
                 on_progress: Optional[ProgressCallback] = None,
                 max_workers: int = config.DEFAULT_WORKERS) -> RunResult:
    if not (config.HORIZON_MIN_DAYS <= horizon_days <= config.HORIZON_MAX_DAYS):
        raise ValueError(
            f"horizon_days must be between {config.HORIZON_MIN_DAYS} and "
            f"{config.HORIZON_MAX_DAYS}; got {horizon_days}."
        )
    notices = config.validate_config()
    if stages is None:
        stages, _ = make_stub_stages()

    started = datetime.now(timezone.utc).isoformat()

    def safe(ticker: str) -> TickerResult:
        try:
            return process_ticker(ticker, horizon_days, stages, on_progress)
        except Exception as exc:  # last line of defence: never lose the run
            return TickerResult(ticker=ticker, status=config.STATUS_FAILED,
                                stage_errors={"pipeline": f"unexpected {type(exc).__name__}: {exc}"})

    workers = max(1, min(max_workers, len(tickers) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(safe, tickers))  # keeps input order

    results = rank_results(results)
    return RunResult(results=results, horizon_days=horizon_days,
                     started_at=started,
                     finished_at=datetime.now(timezone.utc).isoformat(),
                     notices=notices)


if __name__ == "__main__":
    demo = run_pipeline(["FAKE", "STALE429", "NOPRIOR429", "FAILPERM"], 30,
                        on_progress=lambda t, s, st: print(f"{t:12s} {s:16s} {st}"))
    print()
    for row in demo.to_rows():
        print(row)

