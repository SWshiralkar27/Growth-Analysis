# user_inputs.py
"""
Schema + validation for 'unavailable' user-supplied inputs.
Long format: ticker, field, value, as_of_date, source_note.
"""

from __future__ import annotations
import pandas as pd

# field -> (allowed values, kind)
#   kind = "validation"  -> confirms a prior
#   kind = "signal"      -> feeds the scorer as a new input
FIELD_ENUM: dict[str, tuple[set[str], str]] = {
    "shareholding_change": ({"YES", "NO", "UNKNOWN"}, "signal"),
    "policy_change":       ({"YES", "NO", "UNKNOWN"}, "signal"),
    "management_change":   ({"YES", "NO", "UNKNOWN"}, "signal"),
    # TODO: confirm below — placeholders
    "pledge_flag":         ({"YES", "NO", "UNKNOWN"}, "signal"),
    "auditor_change":      ({"YES", "NO", "UNKNOWN"}, "signal"),
    "related_party_flag":  ({"YES", "NO", "UNKNOWN"}, "signal"),
}

REQUIRED_COLS = ["ticker", "field", "value", "as_of_date", "source_note"]


def validate_user_inputs(df: pd.DataFrame, holdings_tickers: set[str]) -> tuple[pd.DataFrame, list[str]]:
    """
    Returns (clean_df, errors). Row-level, human-readable.
    Duplicates: last-wins (per confirmed Q6 decision).
    Missing (ticker, field): treated as UNKNOWN, not an error.
    """
    errors: list[str] = []

    for c in REQUIRED_COLS:
        if c not in df.columns:
            return pd.DataFrame(), [f"user_inputs.csv missing column: {c}"]

    df = df[REQUIRED_COLS].copy()
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["field"]  = df["field"].astype(str).str.strip()
    df["value"]  = df["value"].astype(str).str.strip().str.upper()

    # ticker must exist in holdings
    orphans = df[~df["ticker"].isin(holdings_tickers)]
    for idx in orphans.index:
        errors.append(f"user_inputs.csv row {idx + 2}: ticker '{df.at[idx, 'ticker']}' not in holdings")

    # field must be known
    unknown_fields = df[~df["field"].isin(FIELD_ENUM)]
    for idx in unknown_fields.index:
        errors.append(f"user_inputs.csv row {idx + 2}: unknown field '{df.at[idx, 'field']}'")

    # value must be allowed for that field
    for idx, row in df.iterrows():
        f = row["field"]
        if f in FIELD_ENUM:
            allowed, _ = FIELD_ENUM[f]
            if row["value"] not in allowed:
                errors.append(
                    f"user_inputs.csv row {idx + 2}: value '{row['value']}' "
                    f"not allowed for '{f}' (allowed: {sorted(allowed)})"
                )

    # last-wins on duplicate (ticker, field)
    df = df.drop_duplicates(subset=["ticker", "field"], keep="last")

    return df.reset_index(drop=True), errors


def apply_to_tickers(user_df: pd.DataFrame, tickers: list[str]) -> dict[str, dict[str, str]]:
    """Return {ticker: {field: value}} with UNKNOWN defaults for missing."""
    out: dict[str, dict[str, str]] = {t: {} for t in tickers}
    for t in tickers:
        for f in FIELD_ENUM:
            out[t][f] = "UNKNOWN"
    for row in user_df.itertuples():
        out.setdefault(row.ticker, {})[row.field] = row.value
    return out