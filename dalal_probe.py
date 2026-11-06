"""
dalal_probe.py
Version: Step 10b, v0.10.0 (2026-11-06). Code version 2026-11-step9 (config.py unchanged).
Spec: README v3.1 DRAFT section 6 (data acquisition); handover v2.1 section 6 (Step 10b).

Purpose: READ-ONLY diagnostics for the dalal package. Reports the installed version, the
functions it really has with their signatures, and the type, keys and first rows of what
lookup, shareholding, announcements, block_deals, fundamentals and meta return. It never
touches scoring, the store, reports or the cache, and it never raises: every failure is
returned as data. Replaces the old probe_dalal.py, which was never recovered.
"""
from __future__ import annotations

import inspect
import json
from typing import Any, Callable, Optional

MAX_TEXT = 1500      # characters kept per sample
MAX_ROWS = 2         # rows shown per reply
_CONTAINERS = ("results", "data", "items", "matches", "rows", "periods", "history")
_CODE_KEYS = ("scripcode", "scripCode", "scrip_code", "bse_code", "code")


def _short(value: Any, limit: int = MAX_TEXT) -> str:
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + " ...(cut)"


def _rows(payload: Any) -> list:
    """Same unwrapping idea as data_fetch._unwrap_list, so we see what the parser sees."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for name in _CONTAINERS:
            if isinstance(payload.get(name), list):
                return payload[name]
        return [payload]
    return []


def describe(payload: Any) -> dict:
    """Type, keys and first rows of one reply."""
    info: dict = {"type": type(payload).__name__}
    if payload is None:
        return info
    if isinstance(payload, dict):
        info["top_level_keys"] = sorted(str(k) for k in payload)[:60]
        info["list_containers"] = {str(k): len(v) for k, v in payload.items() if isinstance(v, list)}
    rows = _rows(payload)
    info["row_count"] = len(rows)
    first = rows[0] if rows else None
    if isinstance(first, dict):
        info["first_row_keys"] = sorted(str(k) for k in first)
        info["first_row_values"] = {str(k): _short(v, 120) for k, v in first.items()}
        nested = {str(k): sorted(str(x) for x in v)[:25] for k, v in first.items() if isinstance(v, dict)}
        if nested:
            info["first_row_nested_keys"] = nested
    info["sample_rows"] = [_short(r) for r in rows[:MAX_ROWS]]
    if not rows and not isinstance(payload, (list, dict)):
        info["value"] = _short(payload, 300)
    return info


def _call(label: str, fn: Optional[Callable], *args: Any) -> dict:
    if fn is None:
        return {"label": label, "ok": False, "error": "this function does not exist in the installed dalal"}
    try:
        payload = fn(*args)
    except Exception as exc:  # noqa: BLE001 - providers raise anything
        return {"label": label, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return {"label": label, "ok": True, "result": describe(payload), "_payload": payload}


def _find_code(payload: Any) -> Optional[str]:
    for row in _rows(payload):
        if isinstance(row, dict):
            for key in _CODE_KEYS:
                if row.get(key) is not None:
                    return str(row[key]).strip()
    return None


def _version() -> str:
    try:
        from importlib import metadata
        return metadata.version("dalal")
    except Exception:  # noqa: BLE001
        return "unknown"


def probe(ticker: str, module: Any = None) -> dict:
    """Run the probe for one ticker. `module` is for tests; the default imports dalal."""
    if module is None:
        try:
            import dalal as module  # type: ignore
        except ImportError:
            return {"installed": False, "version": None, "functions": [], "calls": [],
                    "note": "the dalal package is not installed"}

    functions = []
    for name in sorted(dir(module)):
        if name.startswith("_"):
            continue
        obj = getattr(module, name, None)
        if callable(obj):
            try:
                sig = str(inspect.signature(obj))
            except (TypeError, ValueError):
                sig = "(signature unavailable)"
            functions.append({"name": name, "signature": sig})

    get = lambda n: getattr(module, n, None)  # noqa: E731
    calls = [_call(f"lookup({ticker})", get("lookup"), ticker)]
    code = _find_code(calls[0].get("_payload")) if calls[0]["ok"] else None
    calls.append(_call(f"shareholding({ticker})", get("shareholding"), ticker))
    calls.append(_call(f"announcements({ticker})", get("announcements"), ticker))
    calls.append(_call("block_deals()", get("block_deals")))
    if code:
        calls.append(_call(f"shareholding({code})", get("shareholding"), code))
        calls.append(_call(f"fundamentals({code})", get("fundamentals"), code))
        calls.append(_call(f"meta({code})", get("meta"), code))
    else:
        calls.append({"label": "scrip-code calls", "ok": False,
                      "error": "skipped: lookup gave no scrip code"})
    for c in calls:
        c.pop("_payload", None)
    return {"installed": True, "version": _version(), "ticker": ticker,
            "scrip_code": code, "functions": functions, "calls": calls}