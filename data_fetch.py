# =====================================================
# data_fetch.py   (the recovered Fiscal_Data_collector.py)
# PORTED INTO THE STEP 8 LAYOUT: Step 8, v0.8.0 (2026-11-04). Code version 2026-11-step8.
# The collection logic below is the recovered code. Only TWO small changes were made:
#   1. _safe_call no longer retries AttributeError (a function that does not exist cannot
#      succeed on retry, so retrying only wasted seconds).
#   2. get_financial_data converts its record to plain JSON-safe data (dates, numpy numbers,
#      NaN) so the store can save it; values are unchanged.
# Spec: README v3.1 DRAFT section 6 (data acquisition).
# Multi-provider fundamentals + statement trends +
# estimate revisions + exchange-sourced governance data.
# =====================================================
"""
Provider chain for the core ratios
----------------------------------
1. yfinance  : <TICKER>.NS, then <TICKER>.BO   (also the source of statements/estimates)
2. dalal     : BSE fundamentals + meta           (no API key)
3. bfinance  : optional, only if installed

Design rules
------------
* Units are normalised AT INGESTION, per provider:
    - yfinance/bfinance debtToEquity is a PERCENT  -> always divided by 100.
    - dalal debt_to_equity unit is unknown         -> ignored unless config.DALAL_DE_UNIT is set.
    - every margin / growth / return field is a FRACTION (0.12 == 12%).
* Missing data is never invented. A field that no provider supplies stays None and the scorer
  treats it as "not measurable" (it lowers coverage; it is not counted as a failed test).
* Every field keeps its provenance, and values that two providers disagree on are flagged.
* No silent failures: anything that goes wrong is appended to record["warnings"].

Schema keys are read by score_card.py, gemini_analyser.py and report_generator.py.
Change them together.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime
from typing import Any, Callable, Optional

import config

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

try:
    import dalal
except ImportError:  # pragma: no cover
    dalal = None

try:
    import bfinance  # 1:1 yfinance-compatible SDK for NSE/BSE
except ImportError:  # pragma: no cover
    bfinance = None


# =====================================================
# Schema
# =====================================================

# Core ratios (providers fill these)
REQUIRED_FIELDS = [
    "market_cap", "revenue_growth", "ebitda_margin", "operating_margin",
    "debt_to_equity", "return_on_equity", "current_ratio", "trailing_pe", "forward_pe",
]
META_FIELDS = ["sector", "industry"]

# Derived from yfinance statements (annual, newest column first)
STATEMENT_FIELDS = [
    "revenue_growth_annual", "revenue_acceleration", "ebitda_margin_change", "ebit_growth",
    "net_margin", "net_margin_change", "interest_coverage", "no_interest_expense",
    "operating_cash_flow", "ocf_growth", "ocf_to_ebitda", "receivable_days",
    "receivable_days_change", "inventory_days", "payable_days", "ccc_days",
    "ccc_change_days", "total_debt", "debt_change_pct", "latest_fy_end",
]

# Forward-looking estimate revisions (yfinance)
ESTIMATE_FIELDS = ["eps_revision_90d_pct", "eps_net_revisions_30d"]

# Exchange-sourced governance / smart money (dalal)
GOVERNANCE_FIELDS = [
    "promoter_holding", "promoter_holding_change", "institutional_holding",
    "institutional_holding_change", "promoter_pledge_pct", "promoter_pledge_change",
    "ann_auditor_resignation", "ann_cfo_exit", "ann_pledge_invocation", "ann_order_wins",
    "ann_headlines", "ann_events", "ann_rows_in_window", "ann_undated_rows", "ann_window_days",
    "block_deals_recent", "governance_sources",
]

FINANCIAL_SECTOR_HINTS = ("financial", "bank", "insurance", "nbfc")


# =====================================================
# Small utilities
# =====================================================

def _clean_ticker(ticker: str) -> str:
    t = str(ticker).strip().upper()
    for suffix in (".NS", ".BO", ".NSE", ".BSE"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t


def _num(value: Any) -> Optional[float]:
    """Coerce to float; reject None, '', NaN, bool and junk. Does NOT rescale percentages."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if f != f else f
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("%", "")
        if s in ("", "-", "--", "N/A", "NA", "None", "nan"):
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        return None if f != f else f
    try:  # numpy scalars
        f = float(value)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _first_present(mapping: Any, candidates: list[str]) -> Any:
    for key in candidates:
        if isinstance(mapping, dict) and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _safe_call(fn: Callable[[], Any], label: str, warnings: Optional[list] = None) -> Any:
    last_error = None
    for attempt in range(1, config.PROVIDER_ATTEMPTS + 1):
        try:
            return fn()
        except AttributeError as ex:  # STEP 8 CHANGE 1: a missing function cannot succeed on retry
            last_error = ex
            break
        except Exception as ex:  # noqa: BLE001 - providers raise anything
            last_error = ex
            if attempt < config.PROVIDER_ATTEMPTS:
                time.sleep(config.PROVIDER_RETRY_DELAY * attempt)
    msg = f"{label} failed: {type(last_error).__name__}: {last_error}"
    print(f"  [warn] {msg}")
    if warnings is not None:
        warnings.append(msg)
    return None


def _jsonable(value: Any) -> Any:
    """STEP 8 CHANGE 2: plain JSON-safe data. Dates become ISO text, numpy numbers become floats,
    NaN and infinity become None. Values are otherwise unchanged."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _empty_record(ticker: str) -> dict:
    record = {field: None for field in REQUIRED_FIELDS}
    record["ticker"] = ticker
    return record


def _populated_count(record: dict) -> int:
    return sum(1 for field in REQUIRED_FIELDS if record.get(field) is not None)


# ---------- unit normalisation (provider specific) ----------

def _de_from_percent(value: Any) -> Optional[float]:
    """Yahoo-style: 45.2 means 45.2% == 0.452x. Negative equity -> None."""
    v = _num(value)
    if v is None or v < 0:
        return None
    return v / 100.0


def _de_from_dalal(value: Any, warnings: Optional[list] = None) -> Optional[float]:
    v = _num(value)
    if v is None:
        return None
    unit = config.DALAL_DE_UNIT
    if unit == "percent":
        v = v / 100.0
    elif unit == "ratio":
        pass
    else:
        if warnings is not None and not any("DALAL_DE_UNIT" in w for w in warnings):
            warnings.append("dalal debt_to_equity ignored: set DALAL_DE_UNIT after checking with probe_dalal.py")
        return None
    return None if v < 0 else v


# =====================================================
# Cache (schema-versioned; JSON-safe)
# =====================================================

def _cache_path(ticker: str) -> str:
    return os.path.join(config.FIN_CACHE_DIR, f"{ticker}_{date.today().isoformat()}.json")


def _read_cache(ticker: str) -> Optional[dict]:
    if not config.FIN_CACHE_ENABLED:
        return None
    path = _cache_path(ticker)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("ticker") != ticker or payload.get("cache_schema") != config.FIN_CACHE_SCHEMA:
        return None
    print(f"  [cache] hit for {ticker}")
    return payload


def _write_cache(record: dict) -> None:
    if not config.FIN_CACHE_ENABLED:
        return
    try:
        os.makedirs(config.FIN_CACHE_DIR, exist_ok=True)
        with open(_cache_path(record["ticker"]), "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, default=str)
    except (OSError, TypeError, ValueError) as ex:
        print(f"  [warn] cache write failed: {ex}")


# =====================================================
# Provider 1: yfinance (core ratios)
# =====================================================

YF_FIELD_MAP = {
    "market_cap": "marketCap",
    "revenue_growth": "revenueGrowth",
    "ebitda_margin": "ebitdaMargins",
    "operating_margin": "operatingMargins",
    "debt_to_equity": "debtToEquity",
    "return_on_equity": "returnOnEquity",
    "current_ratio": "currentRatio",
    "trailing_pe": "trailingPE",
    "forward_pe": "forwardPE",
}


def _normalise_yfinance_info(info: dict, ticker: str) -> dict:
    record = _empty_record(ticker)
    for field, yf_key in YF_FIELD_MAP.items():
        val = info.get(yf_key)
        record[field] = _de_from_percent(val) if field == "debt_to_equity" else _num(val)
    record["sector"] = info.get("sector") or None
    record["industry"] = info.get("industry") or None
    return record


def _best_info(module, ticker: str, label: str, warnings: list) -> tuple[Optional[dict], Optional[str]]:
    best_info, best_symbol, best_count = None, None, -1
    for suffix in (".NS", ".BO"):
        info = _safe_call(lambda s=suffix: module.Ticker(ticker + s).info, f"{label} {ticker}{suffix}", warnings)
        if not isinstance(info, dict) or not info:
            continue
        count = sum(1 for k in YF_FIELD_MAP.values() if info.get(k) is not None)
        if count > best_count:
            best_info, best_symbol, best_count = info, ticker + suffix, count
    return best_info, best_symbol


def _from_yfinance(ticker: str, warnings: list) -> Optional[dict]:
    if yf is None:
        return None
    info, symbol = _best_info(yf, ticker, "yfinance", warnings)
    if info is None:
        return None
    record = _normalise_yfinance_info(info, ticker)
    record["yf_symbol"] = symbol
    return record if _populated_count(record) >= 2 else None


# =====================================================
# Provider 2: dalal (BSE core ratios)
# =====================================================

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
_ANNUAL_RE = re.compile(r"^FY\s*(\d{2,4})\s*[-/]\s*(\d{2,4})$", re.IGNORECASE)
_QUARTER_RE = re.compile(r"^([A-Za-z]{3})[\s\-]+(\d{2,4})$")


def _unwrap_list(payload: Any) -> list:
    """dalal responses may be a list or a dict wrapping one; return a list of rows."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for container in ("results", "data", "items", "matches", "rows", "periods", "history"):
            if isinstance(payload.get(container), list):
                return payload[container]
        return [payload]
    return []


def _resolve_bse_code(ticker: str, warnings: Optional[list] = None, bse_hint: Optional[str] = None) -> Optional[str]:
    if bse_hint and str(bse_hint).strip():
        return str(bse_hint).strip()
    if str(ticker).isdigit():
        return str(ticker)

    if os.path.exists(config.BSE_CODE_FILE):
        try:
            import pandas as pd
            codes = pd.read_csv(config.BSE_CODE_FILE)
            codes.columns = [c.strip().lower() for c in codes.columns]
            if {"ticker", "bse_code"}.issubset(codes.columns):
                match = codes[codes["ticker"].astype(str).str.upper() == ticker]
                if not match.empty:
                    return str(match.iloc[0]["bse_code"]).strip()
        except Exception as ex:  # noqa: BLE001
            print(f"  [warn] could not read {config.BSE_CODE_FILE}: {ex}")

    if dalal is None or not hasattr(dalal, "lookup"):
        return None

    payload = _safe_call(lambda: dalal.lookup(ticker), f"dalal.lookup({ticker})", warnings)
    for row in _unwrap_list(payload):
        if not isinstance(row, dict):
            continue
        code = _first_present(row, ["scripcode", "scripCode", "scrip_code", "bse_code", "code"])
        if code is not None:
            return str(code).strip()
    return None


def _parse_period(period: Any) -> Optional[tuple]:
    if not isinstance(period, str):
        return None
    p = period.strip()
    annual = _ANNUAL_RE.match(p)
    if annual:
        end = annual.group(2)
        year = int(end) if len(end) == 4 else 2000 + int(end)
        return ("FY", year)
    quarter = _QUARTER_RE.match(p)
    if quarter:
        month = _MONTHS.get(quarter.group(1).title())
        if month:
            y = quarter.group(2)
            year = int(y) if len(y) == 4 else 2000 + int(y)
            return ("M", month, year)
    return None


def _compute_revenue_growth(periods: list[dict]) -> Optional[float]:
    """YoY growth as a fraction. Prefers annual periods, then same-quarter YoY."""
    series: dict[tuple, float] = {}
    for row in periods:
        if not isinstance(row, dict):
            continue
        key = _parse_period(row.get("period"))
        revenue = _num(row.get("revenue"))
        if key and revenue is not None and revenue != 0:
            series[key] = revenue
    if len(series) < 2:
        return None

    annual = {k: v for k, v in series.items() if k[0] == "FY"}
    if len(annual) >= 2:
        latest_key = max(annual, key=lambda k: k[1])
        prior_key = ("FY", latest_key[1] - 1)
        if prior_key in annual and annual[prior_key] != 0:
            return (annual[latest_key] - annual[prior_key]) / abs(annual[prior_key])

    quarters = {k: v for k, v in series.items() if k[0] == "M"}
    if len(quarters) >= 2:
        latest_key = max(quarters, key=lambda k: (k[2], k[1]))
        prior_key = ("M", latest_key[1], latest_key[2] - 1)
        if prior_key in quarters and quarters[prior_key] != 0:
            return (quarters[latest_key] - quarters[prior_key]) / abs(quarters[prior_key])
    return None


def _latest_period_row(periods: list) -> dict:
    """Pick the newest period by parsed label rather than assuming list order."""
    best, best_key = None, None
    for row in periods:
        if not isinstance(row, dict):
            continue
        parsed = _parse_period(row.get("period"))
        if not parsed:
            continue
        key = (parsed[1], 3) if parsed[0] == "FY" else (parsed[2], parsed[1])
        if best_key is None or key > best_key:
            best, best_key = row, key
    if best is not None:
        return best
    return periods[0] if periods and isinstance(periods[0], dict) else {}


def _from_dalal(ticker: str, warnings: list) -> Optional[dict]:
    if dalal is None:
        return None
    scrip_code = _resolve_bse_code(ticker, warnings)
    if not scrip_code:
        print(f"  [info] dalal skipped {ticker}: no BSE scrip code resolved")
        return None

    fundamentals = _safe_call(lambda: dalal.fundamentals(scrip_code), f"dalal.fundamentals({scrip_code})", warnings)
    meta = _safe_call(lambda: dalal.meta(scrip_code), f"dalal.meta({scrip_code})", warnings)
    if not isinstance(fundamentals, dict) and not isinstance(meta, dict):
        return None

    record = _empty_record(ticker)
    fundamentals = fundamentals if isinstance(fundamentals, dict) else {}
    meta = meta if isinstance(meta, dict) else {}

    periods = fundamentals.get("periods") or []
    record["revenue_growth"] = _compute_revenue_growth(periods)

    latest = _latest_period_row(periods)
    opm_pct = _num(latest.get("opm_pct"))
    if opm_pct is not None:
        record["operating_margin"] = opm_pct / 100.0
        if config.ALLOW_DERIVED_MARGINS:
            record["ebitda_margin"] = opm_pct / 100.0

    roe = _num(_first_present(meta, ["roe", "return_on_equity", "returnOnEquity"]))
    if roe is not None:
        record["return_on_equity"] = roe / 100.0  # dalal reports ROE in percent

    record["trailing_pe"] = _num(_first_present(meta, ["pe", "trailing_pe", "trailingPE"]))
    record["market_cap"] = _num(_first_present(meta, ["market_cap", "marketCap", "mcap"]))
    record["debt_to_equity"] = _de_from_dalal(
        _first_present(meta, ["debt_to_equity", "de_ratio", "debtEquity"]), warnings)
    record["current_ratio"] = _num(_first_present(meta, ["current_ratio", "currentRatio"]))
    record["forward_pe"] = _num(_first_present(meta, ["forward_pe", "forwardPE"]))
    record["sector"] = _first_present(meta, ["sector", "industry_group"])
    record["industry"] = _first_present(meta, ["industry"])

    return record if _populated_count(record) >= 2 else None


# =====================================================
# Provider 3: bfinance (optional, yfinance-compatible)
# =====================================================

def _from_bfinance(ticker: str, warnings: list) -> Optional[dict]:
    if bfinance is None:
        return None
    info, symbol = _best_info(bfinance, ticker, "bfinance", warnings)
    if info is None:
        return None
    record = _normalise_yfinance_info(info, ticker)
    return record if _populated_count(record) >= 2 else None


PROVIDERS: list[tuple[str, Callable[[str, list], Optional[dict]]]] = [
    ("yfinance", _from_yfinance),
    ("dalal", _from_dalal),
    ("bfinance", _from_bfinance),
]


# =====================================================
# Statement metrics (yfinance annual statements)
# =====================================================

def _sorted_columns(df):
    """Newest period first, regardless of how the provider ordered the columns."""
    try:
        return df.sort_index(axis=1, ascending=False)
    except Exception:  # noqa: BLE001
        return df


def _row(df, names: list[str]) -> Optional[list]:
    """Values of the first matching row, newest column first; None if no such row."""
    if df is None or getattr(df, "empty", True):
        return None
    df = _sorted_columns(df)
    for name in names:
        if name in df.index:
            return [_num(v) for v in df.loc[name].tolist()]
    return None


def _at(values: Optional[list], i: int) -> Optional[float]:
    if values is None or i >= len(values):
        return None
    return values[i]


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _growth(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old)


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a - b


def _statement_metrics(stock, warnings: list) -> dict:
    out = {k: None for k in STATEMENT_FIELDS}
    try:
        fin, bs, cf = stock.financials, stock.balance_sheet, stock.cashflow
    except Exception as ex:  # noqa: BLE001
        warnings.append(f"yfinance statements unavailable: {ex}")
        return out

    rev = _row(fin, ["Total Revenue", "Operating Revenue"])
    ebitda = _row(fin, ["EBITDA", "Normalized EBITDA"])
    ebit = _row(fin, ["EBIT", "Operating Income"])
    net_income = _row(fin, ["Net Income", "Net Income Common Stockholders"])
    interest = _row(fin, ["Interest Expense", "Interest Expense Non Operating"])
    cogs = _row(fin, ["Cost Of Revenue", "Reconciled Cost Of Revenue"])
    receivables = _row(bs, ["Accounts Receivable", "Receivables", "Net Receivables"])
    inventory = _row(bs, ["Inventory"])
    payables = _row(bs, ["Accounts Payable", "Payables"])
    total_debt = _row(bs, ["Total Debt"])
    ocf = _row(cf, ["Operating Cash Flow", "Total Cash From Operating Activities",
                    "Cash Flow From Continuing Operating Activities"])

    g0 = _growth(_at(rev, 0), _at(rev, 1))
    g1 = _growth(_at(rev, 1), _at(rev, 2))
    out["revenue_growth_annual"] = g0
    out["revenue_acceleration"] = _diff(g0, g1)

    m0, m1 = _ratio(_at(ebitda, 0), _at(rev, 0)), _ratio(_at(ebitda, 1), _at(rev, 1))
    out["ebitda_margin_change"] = _diff(m0, m1)
    out["ebit_growth"] = _growth(_at(ebit, 0), _at(ebit, 1))

    n0, n1 = _ratio(_at(net_income, 0), _at(rev, 0)), _ratio(_at(net_income, 1), _at(rev, 1))
    out["net_margin"] = n0
    out["net_margin_change"] = _diff(n0, n1)

    if interest is not None:
        interest0 = _at(interest, 0)
        if interest0 is not None and abs(interest0) > 0:
            out["interest_coverage"] = _ratio(_at(ebit, 0), abs(interest0))
        elif interest0 is not None:  # reported as zero => debt-free for coverage purposes
            out["no_interest_expense"] = True

    ocf0 = _at(ocf, 0)
    out["operating_cash_flow"] = ocf0
    ocf1 = _at(ocf, 1)
    out["ocf_growth"] = _growth(ocf0, ocf1) if (ocf1 is not None and ocf1 > 0) else None
    ebitda0 = _at(ebitda, 0)
    out["ocf_to_ebitda"] = _ratio(ocf0, ebitda0) if (ebitda0 is not None and ebitda0 > 0) else None

    def days(balance, base, i):
        v = _ratio(_at(balance, i), _at(base, i))
        return None if v is None else v * 365.0

    rec0, rec1 = days(receivables, rev, 0), days(receivables, rev, 1)
    out["receivable_days"] = rec0
    out["receivable_days_change"] = _diff(rec0, rec1)

    inv0, pay0 = days(inventory, cogs, 0), days(payables, cogs, 0)
    inv1, pay1 = days(inventory, cogs, 1), days(payables, cogs, 1)
    out["inventory_days"], out["payable_days"] = inv0, pay0
    if rec0 is not None:
        ccc0 = rec0 + (inv0 or 0.0) - (pay0 or 0.0)
        out["ccc_days"] = ccc0
        if rec1 is not None:
            out["ccc_change_days"] = ccc0 - (rec1 + (inv1 or 0.0) - (pay1 or 0.0))

    debt0, debt1 = _at(total_debt, 0), _at(total_debt, 1)
    out["total_debt"] = debt0
    if debt0 is not None and debt1 is not None:
        out["debt_change_pct"] = 0.0 if (debt0 == 0 and debt1 == 0) else _growth(debt0, debt1)

    try:
        cols = list(_sorted_columns(fin).columns) if fin is not None and not fin.empty else []
        out["latest_fy_end"] = str(cols[0])[:10] if cols else None
    except Exception:  # noqa: BLE001
        pass
    return out


# =====================================================
# Estimate revisions (yfinance)
# =====================================================

def _find_col(df, wanted: str):
    for c in df.columns:
        if str(c).lower() == wanted.lower():
            return c
    return None


def _estimate_metrics(stock, warnings: list) -> dict:
    out = {k: None for k in ESTIMATE_FIELDS}
    try:
        trend = stock.eps_trend
        if trend is not None and not trend.empty:
            for period in ("0y", "+1y"):
                if period in trend.index:
                    cur_c, old_c = _find_col(trend, "current"), _find_col(trend, "90daysAgo")
                    if cur_c is not None and old_c is not None:
                        out["eps_revision_90d_pct"] = _growth(_num(trend.loc[period, cur_c]),
                                                              _num(trend.loc[period, old_c]))
                    break
    except Exception as ex:  # noqa: BLE001
        warnings.append(f"yfinance eps_trend unavailable: {ex}")
    try:
        revs = stock.eps_revisions
        if revs is not None and not revs.empty:
            for period in ("0y", "+1y"):
                if period in revs.index:
                    up_c, dn_c = _find_col(revs, "upLast30days"), _find_col(revs, "downLast30days")
                    up, dn = (_num(revs.loc[period, up_c]) if up_c is not None else None,
                              _num(revs.loc[period, dn_c]) if dn_c is not None else None)
                    if up is not None and dn is not None:
                        out["eps_net_revisions_30d"] = up - dn
                    break
    except Exception as ex:  # noqa: BLE001
        warnings.append(f"yfinance eps_revisions unavailable: {ex}")
    return out


# =====================================================
# dalal: shareholding, announcements, block deals (exchange-sourced)
# =====================================================
# NOTE: dalal's response shapes could not be verified offline. These parsers are tolerant and
# fail LOUDLY (a warning naming the keys they saw). Run probe_dalal.py once to confirm.

_ANN_PATTERNS = {
    "ann_auditor_resignation": re.compile(
        r"(?:resign\w*|cessation|withdr\w+|step(?:ping)? down).{0,80}(?:statutory auditors?|auditors?)|"
        r"(?:statutory auditors?|auditors?).{0,80}(?:resign\w*|cessation|withdr\w+)", re.I | re.S),
    "ann_cfo_exit": re.compile(
        r"(?:resign\w*|cessation|step(?:ping)? down).{0,80}(?:chief financial officer|\bcfo\b)|"
        r"(?:chief financial officer|\bcfo\b).{0,80}(?:resign\w*|cessation)", re.I | re.S),
    "ann_pledge_invocation": re.compile(
        r"invocation of (?:the )?pledge|invok\w+.{0,30}pledge|pledge.{0,30}invok\w+", re.I | re.S),
    "ann_order_wins": re.compile(
        r"letter of award|work order|\b(?:bags?|bagged|secures?|secured|receives?|received|wins?|won)\b"
        r".{0,60}\b(?:orders?|contracts?|loi)\b", re.I | re.S),
}


def _row_text(row: Any) -> str:
    if isinstance(row, dict):
        return " ".join(str(v) for v in row.values() if v is not None)
    return str(row)


_ANN_DATE_RE = re.compile(r"\b(\d{1,2}-[A-Za-z]{3,9}-\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})\b")
_ANN_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")
_ANN_URL_RE = re.compile(r"https?://\S+")
_ANN_SIZE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:KB|MB|GB)\b", re.I)


def _announcement_date(text: str) -> Optional[date]:
    """First parseable date in the row (rows start with e.g. '08-Oct-2021 11:20:56')."""
    m = _ANN_DATE_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    for fmt in _ANN_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _announcement_text(text: str) -> str:
    """Row text without URLs and file sizes, so file names and attachment slugs cannot trigger patterns."""
    text = _ANN_SIZE_RE.sub(" ", _ANN_URL_RE.sub(" ", text))
    return " ".join(text.split())


def _announcement_flags(rows: list, today: Optional[date] = None, window_days: Optional[int] = None) -> dict:
    """
    Count governance / order announcements, but only those inside the look-back window
    (config.ANN_WINDOW_DAYS, default 365). Matching runs on the description only - URLs and sizes
    are stripped. If NO row has a parseable date the window cannot be applied: everything is counted
    and ann_window_days is None so the caller can see the signal is unwindowed.

    Adds ann_events: [{"event", "date", "text"}] (readable, for reports) alongside ann_headlines.
    """
    today = today or date.today()
    window = window_days if window_days is not None else getattr(config, "ANN_WINDOW_DAYS", 365)
    parsed = []
    for row in rows:
        raw = _row_text(row)
        parsed.append((_announcement_date(raw), _announcement_text(raw)))
    any_dated = any(d is not None for d, _ in parsed)

    out: dict = {k: 0 for k in _ANN_PATTERNS}
    headlines: list[str] = []
    events: list[dict] = []
    in_window = undated = 0
    for d, text in parsed:
        if any_dated:
            if d is None:
                undated += 1
                continue
            if (today - d).days > window:
                continue
        in_window += 1
        matched = [k for k, pat in _ANN_PATTERNS.items() if pat.search(text)]
        for k in matched:
            out[k] += 1
            if len(events) < 16:
                events.append({"event": k, "date": d.isoformat() if d else None, "text": text[:200]})
        if matched and len(headlines) < 8:
            headlines.append(f"{d.isoformat() if d else 'undated'} {text[:160]}")
    out["ann_headlines"] = headlines
    out["ann_events"] = events
    out["ann_rows_in_window"] = in_window
    out["ann_undated_rows"] = undated
    out["ann_window_days"] = window if any_dated else None
    return out


def _flatten_number(value: Any) -> Optional[float]:
    n = _num(value)
    if n is not None:
        return n
    if isinstance(value, dict):
        for k, v in value.items():
            if any(h in str(k).lower() for h in ("pct", "percent", "%", "holding")):
                n = _num(v)
                if n is not None:
                    return n
        for v in value.values():
            n = _num(v)
            if n is not None:
                return n
    return None


def _holding_from_row(row: dict) -> dict:
    """Pull promoter / pledge / FII / DII percentages out of one shareholding row."""
    found: dict = {"promoter": None, "pledge": None, "fii": None, "dii": None}
    for key, value in row.items():
        k = str(key).lower()
        n = _flatten_number(value)
        if n is None:
            continue
        if "pledg" in k or "encumb" in k:
            found["pledge"] = n if found["pledge"] is None else found["pledge"]
        elif "promoter" in k:
            found["promoter"] = n if found["promoter"] is None else found["promoter"]
        elif any(t in k for t in ("fii", "fpi", "foreign")):
            found["fii"] = n if found["fii"] is None else found["fii"]
        elif any(t in k for t in ("dii", "mutual", "domestic")):
            found["dii"] = n if found["dii"] is None else found["dii"]
    return found


def _row_date(row: dict):
    try:
        import pandas as pd
        for key, value in row.items():
            if any(t in str(key).lower() for t in ("date", "period", "quarter")):
                ts = pd.to_datetime(value, errors="coerce", dayfirst=True)
                if ts is not pd.NaT and ts == ts:
                    return ts
    except Exception:  # noqa: BLE001
        pass
    return None


def _shareholding_metrics(payload: Any, warnings: list) -> dict:
    out = {k: None for k in ("promoter_holding", "promoter_holding_change", "institutional_holding",
                             "institutional_holding_change", "promoter_pledge_pct", "promoter_pledge_change")}
    rows = [r for r in _unwrap_list(payload) if isinstance(r, dict)]
    if not rows:
        warnings.append("dalal.shareholding returned no recognisable rows; run probe_dalal.py")
        return out

    dated = [(r, _row_date(r)) for r in rows]
    if all(d is not None for _, d in dated):
        rows = [r for r, _ in sorted(dated, key=lambda t: t[1], reverse=True)]
    else:
        warnings.append("dalal.shareholding has no parseable dates; assuming newest row first")

    parsed = [_holding_from_row(r) for r in rows]
    if all(p["promoter"] is None for p in parsed):
        warnings.append("dalal.shareholding: no promoter column found (keys seen: "
                        f"{sorted(str(k) for k in rows[0].keys())[:12]}); run probe_dalal.py")
        return out

    # Normalise to percent (some APIs return fractions)
    values = [v for p in parsed for v in p.values() if v is not None]
    scale = 100.0 if values and max(values) <= 1.0 else 1.0
    for p in parsed:
        for k in p:
            if p[k] is not None:
                p[k] *= scale

    latest, prior = parsed[0], (parsed[1] if len(parsed) > 1 else None)
    out["promoter_holding"] = latest["promoter"]
    out["promoter_pledge_pct"] = latest["pledge"]
    if prior is not None:
        out["promoter_holding_change"] = _diff(latest["promoter"], prior["promoter"])
        out["promoter_pledge_change"] = _diff(latest["pledge"], prior["pledge"])

    def inst(p):
        parts = [p["fii"], p["dii"]]
        return None if all(x is None for x in parts) else sum(x for x in parts if x is not None)

    out["institutional_holding"] = inst(latest)
    if prior is not None:
        out["institutional_holding_change"] = _diff(inst(latest), inst(prior))
    return out


def _governance_data(ticker: str, warnings: list) -> dict:
    out: dict = {k: None for k in GOVERNANCE_FIELDS}
    out.update({"ann_auditor_resignation": 0, "ann_cfo_exit": 0, "ann_pledge_invocation": 0,
                "ann_order_wins": 0, "ann_headlines": [], "block_deals_recent": [], "governance_sources": []})
    if dalal is None:
        return out

    sh = _safe_call(lambda: dalal.shareholding(ticker), f"dalal.shareholding({ticker})", warnings)
    if sh is not None:
        out.update(_shareholding_metrics(sh, warnings))
        if out.get("promoter_holding") is not None:
            out["governance_sources"].append("dalal.shareholding")

    ann = _safe_call(lambda: dalal.announcements(ticker), f"dalal.announcements({ticker})", warnings)
    if ann is not None:
        out.update(_announcement_flags(_unwrap_list(ann)))
        out["governance_sources"].append("dalal.announcements")

    # block_deals() takes NO arguments and is market-wide: filter by symbol ourselves.
    deals = _safe_call(lambda: dalal.block_deals(), "dalal.block_deals()", warnings)
    if deals is not None:
        mine = []
        for row in _unwrap_list(deals):
            text = _row_text(row)
            if re.search(rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])", text.upper()):
                mine.append(" ".join(text.split())[:200])
        out["block_deals_recent"] = mine[:5]
    return out


# =====================================================
# Merge + quality
# =====================================================

CONFLICT_ABS = 0.05
CONFLICT_REL = 0.25


def _merge_records(ticker: str, results: list[tuple[str, dict]]) -> dict:
    merged = _empty_record(ticker)
    for f in META_FIELDS + ["yf_symbol"]:
        merged[f] = None
    per_field_source: dict[str, str] = {}
    field_values: dict[str, dict] = {}
    providers_used: list[str] = []

    for name, record in results:
        contributed = False
        for field in REQUIRED_FIELDS + META_FIELDS + ["yf_symbol"]:
            value = record.get(field)
            if value is None:
                continue
            if field in REQUIRED_FIELDS:
                field_values.setdefault(field, {})[name] = value
            if merged[field] is None:
                merged[field] = value
                per_field_source[field] = name
                contributed = True
        if contributed:
            providers_used.append(name)
        if config.MERGE_MODE == "first_complete":
            if not [f for f in REQUIRED_FIELDS if merged[f] is None]:
                break

    conflicts = {}
    for field, by_provider in field_values.items():
        vals = list(by_provider.values())
        if len(vals) >= 2:
            lo, hi = min(vals), max(vals)
            if (hi - lo) > max(CONFLICT_ABS, CONFLICT_REL * max(abs(lo), abs(hi))):
                conflicts[field] = by_provider

    missing = [f for f in REQUIRED_FIELDS if merged[f] is None]
    merged["missing_fields"] = missing
    merged["data_sources"] = per_field_source
    merged["providers_used"] = providers_used
    merged["conflicts"] = conflicts
    return merged


def _scoring_profile(sector: Optional[str], industry: Optional[str]) -> str:
    text = f"{sector or ''} {industry or ''}".lower()
    return "financials" if any(h in text for h in FINANCIAL_SECTOR_HINTS) else "standard"


def _usable_count(record: dict) -> int:
    n = _populated_count(record)
    n += sum(1 for f in STATEMENT_FIELDS if record.get(f) is not None)
    n += sum(1 for f in ESTIMATE_FIELDS if record.get(f) is not None)
    return n


def _finalise(record: dict) -> dict:
    populated_core = _populated_count(record)
    if _usable_count(record) < config.MIN_USABLE_FIELDS:
        quality = "insufficient"
    elif populated_core == len(REQUIRED_FIELDS):
        quality = "complete"
    else:
        quality = "partial"
    record["data_quality"] = quality
    record["scoring_profile"] = _scoring_profile(record.get("sector"), record.get("industry"))
    return record


# =====================================================
# Public entry point
# =====================================================

def get_financial_data(ticker: str, force_refresh: bool = False) -> Optional[dict]:
    """
    Returns a record (see schema lists above) plus provenance keys:
        data_quality: "complete" | "partial" | "insufficient"
        missing_fields, data_sources, providers_used, conflicts, warnings,
        scoring_profile ("standard" | "financials"), fetched_at, as_of_date, cache_schema.
    Returns None only when every provider returned nothing at all.
    """
    clean = _clean_ticker(ticker)

    if not force_refresh:
        cached = _read_cache(clean)
        if cached is not None:
            return cached

    print(f"  Fetching fundamentals for {clean}")
    warnings: list[str] = []

    results: list[tuple[str, dict]] = []
    for name, fetcher in PROVIDERS:
        record = fetcher(clean, warnings)
        if record is not None:
            print(f"    {name}: {_populated_count(record)}/{len(REQUIRED_FIELDS)} core fields")
            results.append((name, record))

    merged = _merge_records(clean, results)

    # Enrichment (all best-effort, all logged)
    stock = None
    symbol = merged.get("yf_symbol")
    if yf is not None:
        try:
            stock = yf.Ticker(symbol or (clean + ".NS"))
        except Exception as ex:  # noqa: BLE001
            warnings.append(f"yfinance Ticker() failed: {ex}")
    merged.update({k: None for k in STATEMENT_FIELDS + ESTIMATE_FIELDS})
    if stock is not None:
        merged.update(_statement_metrics(stock, warnings))
        merged.update(_estimate_metrics(stock, warnings))
    merged.update(_governance_data(clean, warnings))

    if not results and _usable_count(merged) == 0:
        print(f"  [error] all providers failed for {clean}")
        return None

    merged["warnings"] = warnings
    now = datetime.now()
    merged["fetched_at"] = now.isoformat(timespec="seconds")
    merged["as_of_date"] = now.date().isoformat()
    merged["cache_schema"] = config.FIN_CACHE_SCHEMA
    _finalise(merged)
    merged = _jsonable(merged)

    _write_cache(merged)
    print(f"  [ok] {clean}: quality={merged['data_quality']} via {merged['providers_used']} "
          f"| profile={merged['scoring_profile']} | warnings={len(warnings)}")
    return merged
