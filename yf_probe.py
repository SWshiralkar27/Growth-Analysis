"""
yf_probe.py
Version: Step 10d, v0.10.2 (2026-11-06). Code version 2026-11-step9 (config.py unchanged).
Spec: README v3.1 DRAFT section 6 (data acquisition); handover v2.1 open flags.

Purpose: READ-ONLY diagnostics for the Yahoo (yfinance) numbers the scorer relies on. For one
ticker it shows Yahoo's own revenueGrowth, the last annual and quarterly statement columns, and
what can be computed from them, so a person can see WHICH period a number really describes.
It never touches scoring, the store, the cache or reports, and never raises: every failure is
returned as data. It deliberately does not import any other engine module; the row names below
are copied from data_fetch.py and must be kept in step with it.
"""
from __future__ import annotations

from typing import Any, Optional

MATCH_TOLERANCE = 0.02   # two growth figures within 2 percentage points count as "the same"

REV = ["Total Revenue", "Operating Revenue"]
EBITDA = ["EBITDA", "Normalized EBITDA"]
EBIT = ["EBIT", "Operating Income"]
NET = ["Net Income", "Net Income Common Stockholders"]
OCF = ["Operating Cash Flow", "Total Cash From Operating Activities",
       "Cash Flow From Continuing Operating Activities"]
DEBT = ["Total Debt"]
INFO_KEYS = ["revenueGrowth", "ebitdaMargins", "operatingMargins", "debtToEquity", "marketCap",
             "mostRecentQuarter", "lastFiscalYearEnd", "financialCurrency", "currency", "sector", "industry"]


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _sorted(df):
    try:
        return df.sort_index(axis=1, ascending=False)
    except Exception:  # noqa: BLE001
        return df


def _columns(df) -> list:
    if df is None or getattr(df, "empty", True):
        return []
    try:
        return [str(c)[:10] for c in _sorted(df).columns]
    except Exception:  # noqa: BLE001
        return []


def _row(df, names: list) -> tuple:
    """(matched row name, values newest first) or (None, None)."""
    if df is None or getattr(df, "empty", True):
        return None, None
    try:
        frame = _sorted(df)
        for name in names:
            if name in frame.index:
                return name, [_num(v) for v in frame.loc[name].tolist()]
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _at(values: Optional[list], i: int) -> Optional[float]:
    return values[i] if values is not None and i < len(values) else None


def _growth(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old)


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _scale(values: Optional[list], divisor: float) -> Optional[list]:
    if values is None:
        return None
    return [None if v is None else round(v / divisor, 2) for v in values]


def _get(stock: Any, attr: str) -> Any:
    try:
        return getattr(stock, attr)
    except Exception:  # noqa: BLE001
        return None


def _read_info(stock: Any) -> tuple:
    """(info dict or None, error text or None)."""
    try:
        info = stock.info
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"
    return (info, None) if isinstance(info, dict) and info else (None, "Yahoo returned no info")


def _readings(info_rg, annual_g, q_yoy, q_qoq, ocf0, ebitda0, ocf_cols, fin_cols) -> list:
    lines = []
    if info_rg is None:
        lines.append("Yahoo revenueGrowth is missing.")
    else:
        def near(x):
            return x is not None and abs(info_rg - x) <= MATCH_TOLERANCE
        if near(q_yoy):
            what = "matches the latest-quarter year-on-year growth, not the annual growth"
        elif near(annual_g):
            what = "matches the annual growth"
        elif near(q_qoq):
            what = "matches quarter-on-quarter growth"
        else:
            what = "matches none of the growth figures computed here"
        lines.append(f"Yahoo revenueGrowth {_pct(info_rg)} {what}. "
                     f"Computed: annual {_pct(annual_g)}, latest quarter year-on-year {_pct(q_yoy)}, "
                     f"latest quarter on previous quarter {_pct(q_qoq)}.")
    if ocf0 is None or ebitda0 is None:
        lines.append("OCF/EBITDA cannot be computed: operating cash flow or EBITDA is missing.")
    elif ebitda0 <= 0:
        lines.append("EBITDA is zero or negative, so OCF/EBITDA is not computed.")
    else:
        lines.append(f"OCF/EBITDA = {ocf0 / ebitda0:.2f}x (operating cash flow divided by EBITDA, newest annual columns).")
    if ocf_cols and fin_cols and ocf_cols[0] != fin_cols[0]:
        lines.append(f"Warning: the newest cash-flow column ({ocf_cols[0]}) and the newest income-statement "
                     f"column ({fin_cols[0]}) are different dates, so that ratio mixes two years.")
    return lines


def probe(ticker: str, module: Any = None) -> dict:
    """Run the probe for one ticker. `module` is for tests; the default imports yfinance."""
    if module is None:
        try:
            import yfinance as module  # type: ignore
        except ImportError:
            return {"installed": False, "note": "the yfinance package is not installed"}

    errors = []
    stock, info, symbol = None, None, None
    for suffix in (".NS", ".BO"):
        try:
            candidate = module.Ticker(ticker + suffix)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ticker}{suffix}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        got, err = _read_info(candidate)
        if got is not None:
            stock, info, symbol = candidate, got, ticker + suffix
            break
        errors.append(f"{ticker}{suffix}: {err}")
    if stock is None:
        return {"installed": True, "found": False, "ticker": ticker, "errors": errors}

    fin, bs, cf = _get(stock, "financials"), _get(stock, "balance_sheet"), _get(stock, "cashflow")
    qfin = _get(stock, "quarterly_financials")
    currency = info.get("financialCurrency") or info.get("currency")
    divisor, unit = (1e7, "crore INR") if currency == "INR" else (1.0, f"raw {currency or 'unknown currency'}")

    names, annual = {}, {}
    for label, df, wanted in (("Total Revenue", fin, REV), ("EBITDA", fin, EBITDA), ("EBIT", fin, EBIT),
                              ("Net Income", fin, NET), ("Operating Cash Flow", cf, OCF),
                              ("Total Debt", bs, DEBT)):
        matched, values = _row(df, wanted)
        names[label] = matched
        annual[label] = _scale(values, divisor)
    raw = {label: _row(df, wanted)[1] for label, df, wanted in (
        ("rev", fin, REV), ("ebitda", fin, EBITDA), ("ocf", cf, OCF), ("qrev", qfin, REV))}

    rev, ebitda, ocf, qrev = raw["rev"], raw["ebitda"], raw["ocf"], raw["qrev"]
    annual_g = _growth(_at(rev, 0), _at(rev, 1))
    q_yoy = _growth(_at(qrev, 0), _at(qrev, 4))
    q_qoq = _growth(_at(qrev, 0), _at(qrev, 1))
    info_rg = _num(info.get("revenueGrowth"))
    fin_cols, cf_cols, q_cols = _columns(fin), _columns(cf), _columns(qfin)

    return {
        "installed": True, "found": True, "ticker": ticker, "symbol": symbol, "unit": unit,
        "errors": errors,
        "info": {k: info.get(k) for k in INFO_KEYS if info.get(k) is not None},
        "row_names_used": names,
        "annual": {"columns_income_statement": fin_cols, "columns_cash_flow": cf_cols, "rows": annual},
        "quarterly": {"columns": q_cols, "total_revenue": _scale(qrev, divisor)},
        "computed": {"annual_revenue_growth": annual_g, "latest_quarter_yoy": q_yoy,
                     "latest_quarter_qoq": q_qoq,
                     "ocf_to_ebitda": (_at(ocf, 0) / _at(ebitda, 0)
                                       if _at(ocf, 0) is not None and (_at(ebitda, 0) or 0) > 0 else None)},
        "readings": _readings(info_rg, annual_g, q_yoy, q_qoq, _at(ocf, 0), _at(ebitda, 0), cf_cols, fin_cols),
    }