"""
pipeline.py
Version: Step 4, v0.4.0 (2026-11-02). Code version 2026-11-step4.
Spec: README v3.1 DRAFT, sections 4, 5 and 10.

Purpose: the orchestrator. Sequences the per-ticker stages, handles failures,
ranks the results and returns plain data. It contains no scoring maths, no
data fetching and no UI code.

Status: every stage is a STUB except the store (step 2), the scorer (step 3) and the
filings reader (step 4). Real modules replace
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
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

import config
import pdf_extractor
import score_card
from errors import PermanentError, TransientError
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


def score_stage_v21(ticker, analysis, financials, indicators, manual,
                    horizon_days) -> ScoreBreakdown:
    """Score stage: runs the recovered v2.1 quality and governance scorer.

    The scorer's output is mapped onto ScoreBreakdown. The 55/45 fields stay
    None: v2.1 has no short-term timing layer (see the Price-Impact score, later).
    Peers are not passed, so the valuation signal scores at the prior until a
    batch supplies peer statistics.
    """
    result = score_card.compute_score(financials or {}, analysis or {}, peers=None)
    bd = result["breakdown"]
    labels = [f"Quality and governance score, model {bd['scoring_model']} (0 to 100)"]
    if analysis is None:
        labels.append("No LLM analysis: LLM component scored at the prior")
    if (financials or {}).get("stub"):
        labels.append("STUB data: every signal scored at the prior")
    if getattr(config, "UNVERIFIED_GOVERNANCE_CAP", None) is None:
        labels.append("Unverified-governance cap is OFF (value not recovered)")
    caps = [n for n in bd["governance_notes"] if n.startswith("Capped at")]
    components = {
        "llm_component": {"points": bd["llm_component"], "max": 60.0},
        "fin_level": {"points": bd["fin_level"], "max": 20.0},
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


def make_stub_stages(store: Any = None) -> tuple[Stages, Any]:
    """Stub stages. With store=None an in-memory StubStore is used (tests).
    Pass default_store() to use the real file-backed store."""
    use_memory_store = store is None
    if use_memory_store:
        store = StubStore()
    else:
        seed_demo_prior(store)

    @_stub
    def fetch(ticker: str) -> dict:
        return {"financials": {"ticker": ticker, "stub": True},
                "price_history": {"ticker": ticker, "bars": [], "stub": True}}

    @_stub
    def indicators(price_history: dict) -> dict:
        return {"stub": True}

    @_stub
    def manual_inputs(ticker: str) -> dict:
        return {}

    @_stub
    def analyse(ticker: str, context: dict, financials: dict) -> dict:
        if ticker in SIM_TRANSIENT:
            raise TransientError("simulated 429 from the analyser")
        if ticker in SIM_PERMANENT:
            raise PermanentError("simulated permanent analyser failure")
        return {"stub": True, "note": "no real analysis yet"}

    if use_memory_store:
        @_stub
        def load_prior(ticker: str) -> Optional[dict]:
            return store.load(ticker)

        @_stub
        def save(ticker: str, record: dict) -> None:
            store.save(ticker, record)
    else:
        load_prior, save = store.load, store.save  # real store: not marked as stubs

    stages = Stages(
        fetch=fetch, indicators=indicators, manual_inputs=manual_inputs,
        filings_context=filings_context_stage, analyse=analyse, score=score_stage_v21,
        load_prior=load_prior, save=save,
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
                            stubbed_stages=stubbed, prior_used=prior_used)

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

    # 4. filings context
    context, ctx_err = run("filings_context", stages.filings_context, ticker)

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

    return TickerResult(ticker=ticker, status=status, stage_errors=errors,
                        breakdown=breakdown, stubbed_stages=stubbed,
                        prior_used=prior_used)


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
