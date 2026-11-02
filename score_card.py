# =====================================================
# PORTED INTO THE STEP 3 LAYOUT: Step 3, v0.3.0 (2026-11-02). Code version 2026-11-step3.
# The scoring logic below is the recovered v2.1 code, UNCHANGED. No weight, cap,
# threshold or penalty was edited. It reads two optional values from config.py:
#   SCORE_PARAMS                 (overrides for _DEFAULTS; empty = use the defaults)
#   UNVERIFIED_GOVERNANCE_CAP    (None = that cap is OFF; the real value was in the lost config.py)
# Spec: README v3.1 DRAFT (this is the "quality and governance score").
# =====================================================
# =====================================================
# score_card.py  (v2.1)
# =====================================================
# Drop-in replacement for v2. Public API unchanged:
#     compute_score(financials, llm_output, peers=None) -> {"score", "breakdown"}
#     rating_for_score(score)                           -> str
#     build_peer_stats(iterable_of_financials)          -> dict
#
# v2.1 changes (all driven by the first real run - see score_diagnostics DETAIL output):
#   1. ORDER BOOK: a factor with NO evidence is excluded (not scored at the prior) when the company's
#      sector is not one where order books drive growth. 10 of 17 names were being marked down for
#      "no order book evidence" in hospitals, autos, consumer, chemicals, pharma, gas utilities.
#      Evidence that exists is always counted; unknown sector is never excluded.
#   2. QUOTE SUPPORT CHECK: a verified governance flag only costs points if its quote (a) contains
#      language that actually describes that kind of event and (b) does not say the matter was
#      dismissed / resolved / a planned transition. "Quote exists on the page" is not "quote supports
#      the flag". Waived flags are listed in the notes with the reason, so nothing is hidden.
#   3. EXCHANGE-ONLY EVENTS (announcement regexes, no filing quote): smaller penalty, no gate -
#      except pledge invocation, which is specific enough to keep full weight. If a filing quote
#      corroborates the exchange hit, full penalty and the gate apply.
#   4. CFO exit penalty 8 -> 5 (a yellow flag, not a red one).
#   5. CONFIRMED EVENTS: events you verified by hand (SCORE_PARAMS['confirmed_events']) get the full
#      penalty and the gate, whatever their original source.
#
# Everything is tunable from config.py:  SCORE_PARAMS = {"cfo_exit_penalty": ..., ...}
# (see _DEFAULTS; override only the keys you want to change).
# =====================================================

from __future__ import annotations

import re
from statistics import median
from typing import Any, Iterable, Optional

import config

MODEL_VERSION = "v2.1"

_DEFAULTS: dict[str, Any] = {
    "evidence_prior": 0.35,
    "confidence_weights": {"high": 1.0, "medium": 0.8, "low": 0.5, "none": 0.0},
    "factor_points": 15.0,
    # factors excluded (not zeroed) for a collector scoring_profile
    "not_applicable_factors": {"financials": ["Order Book Growth"]},
    # Order Book with NO evidence is excluded unless the sector is one of these (yfinance sector names)
    "order_book_relevant_sectors": ["Industrials", "Technology", "Real Estate"],
    "no_filing_cap": 54,
    "governance_gate_cap": 54,
    "flag_penalties": {
        "auditor_qualification": 20.0,
        "auditor_resignation": 20.0,
        "pledge_invocation": 20.0,
        "related_party_concerns": 8.0,
        "cfo_exit": 5.0,
        "governance_concerns": 10.0,
    },
    # penalty when the ONLY source is the exchange-announcement regexes (no corroborating filing quote)
    "exchange_only_penalties": {"auditor_resignation": 10.0, "cfo_exit": 4.0},
    "exchange_only_gate_flags": ["pledge_invocation"],
    "hard_flags": ["auditor_qualification", "auditor_resignation", "pledge_invocation"],
    "flag_penalty_cap": 40.0,
    "use_exchange_signals": True,
    "quote_support_check": True,
    # Events YOU have verified by hand (e.g. after reading the exchange headline): full penalty and gate.
    #   SCORE_PARAMS = {"confirmed_events": {"AURUM": ["auditor_resignation"]}}
    "confirmed_events": {},
    "peer_min_n": 4,
}

# collector (Fiscal_Data_collector) announcement counters -> governance event names
_EXCHANGE_MAP = {
    "ann_auditor_resignation": "auditor_resignation",
    "ann_cfo_exit": "cfo_exit",
    "ann_pledge_invocation": "pledge_invocation",
}
_EVENT_TO_COUNTER = {v: k for k, v in _EXCHANGE_MAP.items()}

# Language a quote must contain to describe each kind of event
_SUPPORT = {
    "auditor_qualification": re.compile(
        r"qualif|adverse opinion|disclaimer of opinion|emphasis of matter|except for the effects", re.I),
    "auditor_resignation": re.compile(r"resign|cessation|withdr|step(?:ped|ping)? down|\bceas", re.I),
    "cfo_exit": re.compile(
        r"resign|cessation|step(?:ped|ping)? down|\bceas|relinquish|vacat|demit|\bexit(?:ed)?\b|\bdepart(?:ed|ure)\b", re.I),
    "related_party_concerns": re.compile(
        r"loan|advance|guarantee|financial assistance|arm.?s length|ordinary course|unsecured|"
        r"interest.free|waive|exceed|without (?:prior )?approval|non.?compliance", re.I),
    "governance_concerns": re.compile(
        r"investigat|indict|fraud|\bsebi\b|enforcement|penalt|show.cause|\bprobe\b|allegation|whistle|"
        r"misappropriat|\bdefault|\braid|sfio|\bcbi\b|\bdoj\b|\bcharges?\b|lawsuit|litigation|violation|"
        r"non.?compliance|\bfined?\b", re.I),
}
# Cues that the matter is over, or was a planned / routine change rather than a warning sign
_RESOLVED = re.compile(
    r"dismiss|dropped|withdrawn|quash|acquit|settled|resolved|exonerat|no further action|"
    r"\btransition|new role|re.?designat|succession|completion of (?:the |their |his |her )?term|"
    r"end of (?:the |their |his |her )?term|\bretire|superannuat", re.I)


def _p(name: str) -> Any:
    override = getattr(config, "SCORE_PARAMS", None) or {}
    return override.get(name, _DEFAULTS[name])


# -----------------------------------------------------
# helpers
# -----------------------------------------------------
def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _up(x: float, lo: float, hi: float) -> float:
    """0 at <=lo, 1 at >=hi, linear between (higher is better)."""
    return _clamp01((x - lo) / (hi - lo))


def _down(x: float, good: float, bad: float) -> float:
    """1 at <=good, 0 at >=bad, linear between (lower is better)."""
    return _clamp01((bad - x) / (bad - good))


def _first(f: dict, *keys: str) -> Optional[float]:
    for k in keys:
        v = _num(f.get(k))
        if v is not None:
            return v
    return None


def _short(text: Any, limit: int = 110) -> str:
    return " ".join(str(text).split())[:limit]


def _fmt(v: float, kind: str) -> str:
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "spct":
        return f"{v * 100:+.1f}%"
    if kind == "dpp":      # fraction difference shown as percentage points
        return f"{v * 100:+.1f}pp"
    if kind == "pp":       # already in percentage points
        return f"{v:+.1f}pp"
    if kind == "x":
        return f"{v:.2f}x"
    if kind == "n":
        return f"{v:+.0f}"
    return f"{v:.2f}"


def _score_block(signals: list[tuple], prior: float) -> tuple[float, float, list[str]]:
    """
    signals: (label, value, fn(value)->0..1, max_points, fmt_kind)
    A signal whose value is None is scored at `prior` of its max (not at zero).
    """
    points = 0.0
    maximum = 0.0
    notes: list[str] = []
    missing = 0
    for label, value, fn, max_pts, kind in signals:
        maximum += max_pts
        v = _num(value)
        if v is None:
            points += prior * max_pts
            missing += 1
            continue
        pts = fn(v) * max_pts
        points += pts
        notes.append(f"{label} {_fmt(v, kind)} (+{pts:.1f}/{max_pts:g})")
    if missing:
        notes.append(f"{missing} signal(s) not measurable - scored at prior {prior:.0%}")
    return points, maximum, notes


# -----------------------------------------------------
# peers (optional sector-relative valuation)
# -----------------------------------------------------
def _valuation_pe(f: dict) -> Optional[float]:
    for key in ("forward_pe", "trailing_pe"):
        v = _num(f.get(key))
        if v is not None and 0 < v < 300:
            return v
    return None


def build_peer_stats(financials_iter: Iterable[dict], min_n: Optional[int] = None) -> dict:
    """
    {"industry:<name>" | "sector:<name>": {"pe_median": x, "n": n}} for groups with >= min_n names.
    Pass the batch's financials; interactive runs can skip it (valuation then scores at prior).
    """
    min_n = min_n or _p("peer_min_n")
    groups: dict[str, list[float]] = {}
    for f in financials_iter:
        if not isinstance(f, dict):
            continue
        pe = _valuation_pe(f)
        if pe is None:
            continue
        for kind in ("industry", "sector"):
            name = f.get(kind)
            if name:
                groups.setdefault(f"{kind}:{name}", []).append(pe)
    return {k: {"pe_median": median(v), "n": len(v)} for k, v in groups.items() if len(v) >= min_n}


def _peer_pe_ratio(f: dict, peers: Optional[dict]) -> Optional[float]:
    if not peers:
        return None
    pe = _valuation_pe(f)
    if pe is None:
        return None
    for kind in ("industry", "sector"):
        stats = peers.get(f"{kind}:{f.get(kind)}")
        if stats and stats.get("pe_median"):
            return pe / stats["pe_median"]
    return None


# -----------------------------------------------------
# 1. LLM component (max 60)
# -----------------------------------------------------
def _llm_component(financials: dict, factors: list) -> tuple[float, float, list[str]]:
    prior = _p("evidence_prior")
    weights = _p("confidence_weights")
    per = _p("factor_points")
    profile = financials.get("scoring_profile") or "standard"
    sector = financials.get("sector")
    not_applicable = set(_p("not_applicable_factors").get(profile, []))
    ob_relevant = set(_p("order_book_relevant_sectors"))

    points, n, wsum = 0.0, 0, 0.0
    notes: list[str] = []
    for f in factors or []:
        name = f.get("factor", "?")
        conf = f.get("confidence_applied", "low")
        if name in not_applicable:
            notes.append(f"{name}: not applicable for {profile} profile (excluded)")
            continue
        if name == "Order Book Growth" and conf == "none" and sector and sector not in ob_relevant:
            notes.append(f"{name}: no evidence and not an order-book sector ({sector}) - excluded")
            continue
        score = _num(f.get("score"))
        score = 50.0 if score is None else max(0.0, min(100.0, score))
        w = weights.get(conf, weights["low"])
        # shrink the factor score toward the prior in proportion to evidence quality
        eff = prior + (score / 100.0 - prior) * w
        pts = eff * per
        points += pts
        wsum += w
        n += 1
        notes.append(f"{name}: {score:.0f}/100 ({conf} confidence -> +{pts:.1f}/{per:g})")

    if n == 0:
        return prior * 60.0, 0.0, notes + ["No applicable factors - LLM component at prior"]
    return points / (per * n) * 60.0, wsum / n, notes


# -----------------------------------------------------
# 2. Financial points (max 40) = level 20 + direction 14 + valuation/ownership 6
# -----------------------------------------------------
def _level_signals(f: dict) -> list[tuple]:
    rev = _first(f, "revenue_growth_ttm", "revenue_growth")
    if f.get("scoring_profile") == "financials":
        return [
            ("Revenue growth", rev, lambda v: _up(v, 0.0, 0.15), 8, "pct"),
            ("Return on equity", f.get("return_on_equity"), lambda v: _up(v, 0.08, 0.18), 12, "pct"),
        ]
    icr = _num(f.get("interest_coverage"))
    icr_label = "Interest cover"
    if icr is None and f.get("no_interest_expense"):
        icr, icr_label = 99.0, "Interest cover (debt-free)"
    return [
        ("Revenue growth", rev, lambda v: _up(v, 0.0, 0.20), 6, "pct"),
        ("EBITDA margin", f.get("ebitda_margin"), lambda v: _up(v, 0.05, 0.20), 5, "pct"),
        ("Debt/equity", f.get("debt_to_equity"), lambda v: _down(v, 0.5, 1.5), 5, "x"),
        ("OCF/EBITDA", f.get("ocf_to_ebitda"), lambda v: _up(v, 0.4, 0.8), 2, "x"),
        (icr_label, icr, lambda v: _up(v, 2.0, 5.0), 2, "x"),
    ]


def _direction_signals(f: dict) -> list[tuple]:
    if f.get("scoring_profile") == "financials":
        margin = ("Net margin change", f.get("net_margin_change"), lambda v: _up(v, -0.02, 0.02), 3, "dpp")
    else:
        margin = ("EBITDA margin change", f.get("ebitda_margin_change"), lambda v: _up(v, -0.02, 0.02), 3, "dpp")
    return [
        ("Revenue acceleration", f.get("revenue_acceleration"), lambda v: _up(v, -0.05, 0.05), 3, "dpp"),
        margin,
        ("EBIT growth", f.get("ebit_growth"), lambda v: _up(v, -0.10, 0.25), 2, "spct"),
        ("EPS revision 90d", f.get("eps_revision_90d_pct"), lambda v: _up(v, -0.06, 0.06), 4, "spct"),
        ("Net EPS revisions 30d", f.get("eps_net_revisions_30d"), lambda v: _up(v, -3, 3), 2, "n"),
    ]


def _valuation_signals(f: dict, peers: Optional[dict]) -> list[tuple]:
    return [
        ("PE vs sector median", _peer_pe_ratio(f, peers), lambda v: _down(v, 0.8, 1.6), 3, "x"),
        ("Promoter holding change", f.get("promoter_holding_change"), lambda v: _up(v, -1.5, 0.5), 1.5, "pp"),
        ("Institutional holding change", f.get("institutional_holding_change"), lambda v: _up(v, -2.0, 3.0), 1.5, "pp"),
    ]


# -----------------------------------------------------
# 3. Governance
# -----------------------------------------------------
def _quote_text(g: dict) -> str:
    parts = [str(e.get("quote", "")) for e in (g.get("evidence") or []) if isinstance(e, dict)]
    if g.get("quote"):
        parts.append(str(g["quote"]))
    return " ".join(parts).strip()


def _waiver_reason(flag: str, quote: str) -> Optional[str]:
    """Why a verified flag should NOT cost points (None = it stands)."""
    if not quote:
        return "no quote attached"
    support = _SUPPORT.get(flag)
    if support is not None and not support.search(quote):
        return "quote does not describe this kind of event"
    if _RESOLVED.search(quote):
        return "quote indicates the matter is resolved or a planned change"
    return None


def _exchange_evidence(financials: dict, event: str) -> list[str]:
    counter = _EVENT_TO_COUNTER.get(event)
    lines = []
    for e in financials.get("ann_events") or []:
        if isinstance(e, dict) and e.get("event") == counter:
            lines.append(f"{e.get('date') or 'undated'} {_short(e.get('text'), 110)}")
    if not lines:  # older cached financials without ann_events
        lines = [_short(h, 110) for h in (financials.get("ann_headlines") or [])[:2]]
    return lines[:2]


def _governance(financials: dict, governance: list) -> dict:
    penalties = _p("flag_penalties")
    exchange_only = _p("exchange_only_penalties")
    hard_flags = set(_p("hard_flags"))
    gate_exchange_only = set(_p("exchange_only_gate_flags"))
    check = _p("quote_support_check")

    events: dict[str, set] = {}
    notes: list[str] = []

    for g in governance or []:
        if not (g.get("raised") and g.get("verified")):
            continue
        flag = g.get("flag", "governance_concerns")
        quote = _quote_text(g)
        reason = _waiver_reason(flag, quote) if check else None
        if reason:
            notes.append(f"{flag}: penalty waived - {reason}: \"{_short(quote)}\"")
            continue
        events.setdefault(flag, set()).add("filing")

    if _p("use_exchange_signals"):
        for key, event in _EXCHANGE_MAP.items():
            count = _num(financials.get(key))
            if count and count > 0:
                events.setdefault(event, set()).add("exchange")

    ticker = str(financials.get("ticker") or "").upper()
    for event in (_p("confirmed_events") or {}).get(ticker, []):
        events.setdefault(event, set()).add("confirmed")

    flag_penalty = 0.0
    hard_events: set[str] = set()
    for event, sources in sorted(events.items()):
        if "filing" in sources or "confirmed" in sources:
            pen = penalties.get(event, 10.0)
        else:
            pen = exchange_only.get(event, penalties.get(event, 10.0))
        flag_penalty += pen
        suffix = " - exchange feed only, verify" if sources == {"exchange"} and event not in gate_exchange_only else ""
        notes.append(f"{event} via {'+'.join(sorted(sources))} (-{pen:g}){suffix}")
        if "exchange" in sources:
            for line in _exchange_evidence(financials, event):
                notes.append(f"  exchange {event}: {line}")
        if event in hard_flags and ("filing" in sources or "confirmed" in sources or event in gate_exchange_only):
            hard_events.add(event)
    flag_penalty = min(flag_penalty, _p("flag_penalty_cap"))

    pledge_penalty = 0.0
    pledge = _num(financials.get("promoter_pledge_pct"))
    if pledge is not None and pledge > 10.0:
        pledge_penalty = min(20.0, pledge * 0.5)
        notes.append(f"Promoter pledge {pledge:.1f}% (-{pledge_penalty:.1f})")

    return {
        "penalty": -(flag_penalty + pledge_penalty),
        "events": events,
        "hard": bool(hard_events),
        "hard_events": hard_events,
        "verified_in_filing": any(("filing" in s or "confirmed" in s) for s in events.values()),
        "notes": notes,
    }


def _governance_checked(financials: dict, llm_output: dict, verified_in_filing: bool) -> tuple[bool, str]:
    """True when governance was actually looked at, not merely when some feed answered."""
    if verified_in_filing:
        return True, "verified flag in filings"
    coverage = llm_output.get("governance_coverage")
    if isinstance(coverage, list) and "auditor_report" in coverage and len(coverage) >= 2:
        return True, f"filing sections read: {', '.join(coverage)}"
    sources = financials.get("governance_sources") or []
    if "dalal.shareholding" in sources and "dalal.announcements" in sources:
        return True, "both exchange feeds returned data"
    return False, "no filing governance sections in context and exchange feeds incomplete"


def _no_filing(llm_output: dict) -> bool:
    docs = llm_output.get("documents_used")
    return (docs is not None and len(docs) == 0) or llm_output.get("pages_included") == 0


# -----------------------------------------------------
# Public API
# -----------------------------------------------------
def compute_score(financials: dict, llm_output: dict, peers: Optional[dict] = None) -> dict:
    prior = _p("evidence_prior")

    # 1. LLM component
    llm_points, coverage, factor_notes = _llm_component(financials, llm_output.get("factors", []))

    # 2. Financial points
    lvl, lvl_max, lvl_notes = _score_block(_level_signals(financials), prior)
    dirn, dir_max, dir_notes = _score_block(_direction_signals(financials), prior)
    val, val_max, val_notes = _score_block(_valuation_signals(financials, peers), prior)
    fin_total = lvl + dirn + val
    fin_notes = ([f"[level {lvl:.1f}/{lvl_max:g}] " + "; ".join(lvl_notes)] +
                 [f"[direction {dirn:.1f}/{dir_max:g}] " + "; ".join(dir_notes)] +
                 [f"[valuation/ownership {val:.1f}/{val_max:g}] " + "; ".join(val_notes)])

    # 3. Governance
    gov = _governance(financials, llm_output.get("governance", []))
    gov_notes = list(gov["notes"])

    raw = llm_points + fin_total + gov["penalty"]
    final = max(0.0, min(100.0, raw))

    # 4. Caps (lowest applicable cap wins)
    caps: list[tuple[float, str]] = []
    if gov["hard"]:
        caps.append((_p("governance_gate_cap"), "Governance gate: " + ", ".join(sorted(gov["hard_events"]))))
    if _no_filing(llm_output):
        caps.append((_p("no_filing_cap"), "No filing text analysed (financials-only)"))
    unverified_cap = getattr(config, "UNVERIFIED_GOVERNANCE_CAP", None)
    if unverified_cap:
        checked, why = _governance_checked(financials, llm_output, gov["verified_in_filing"])
        if not checked:
            caps.append((unverified_cap, f"Governance unchecked: {why}"))
    for cap, why in caps:
        if final > cap:
            final = float(cap)
            gov_notes.append(f"Capped at {cap:g} ({why})")

    return {
        "score": round(final, 1),
        "breakdown": {
            "scoring_model": MODEL_VERSION,
            "llm_component": round(llm_points, 1),
            "financial_bonus": round(fin_total, 1),
            "fin_level": round(lvl, 1),
            "fin_direction": round(dirn, 1),
            "fin_valuation": round(val, 1),
            "governance_penalty": round(gov["penalty"], 1),
            "raw_score": round(raw, 1),
            "llm_coverage": round(coverage, 2),
            "factor_notes": factor_notes,
            "financial_notes": fin_notes,
            "governance_notes": gov_notes,
        },
    }


def rating_for_score(score: float) -> str:
    if score >= 85: return "Strong Candidate"
    if score >= 70: return "Good Candidate"
    if score >= 55: return "Mixed Signals"
    if score >= 40: return "Speculative"
    return "Avoid"
