"""
manual_inputs_store.py
Version: Step 11a, v0.11.0 (2026-11-07). Code version 2026-11-step11a.
Spec: README v3.1 DRAFT section 8 (manual input system); approved 2026-11-07 with a 90-day
freshness limit and the first-version field list in config.MANUAL_FIELDS.

Purpose: keep values the owner enters by hand (and the source document behind each one) for
later runs. One job: save, look up and judge the freshness of manual values. No UI, no scoring,
no knowledge of other engine modules (it imports only config and errors).

Rules implemented here:
  - One file per ticker: <root>/values/<TICKER>.json. Source documents go to <root>/sources/.
  - A value is validated before anything is written: known field, a number inside its range
    or a real date. A refused input changes nothing on disk.
  - "Leave blank" is a real, saved choice (value None) with an as-of date, so the form does not
    ask again until the limit passes. A blank is never turned into zero.
  - Freshness is counted from the as-of date the owner gives: fresh up to
    config.MANUAL_INPUT_MAX_AGE_DAYS days, stale after that, "missing" if never saved.
  - Saves are atomic: serialise first, write a temp file in the same folder, flush it, then
    os.replace. A crash or an error never leaves a half-written record and never damages the
    previous one.
  - Source documents are stored under a name that includes a hash of their content, so a
    document is never overwritten and the same document is stored once. Allowed types and the
    size limit come from config.
  - A damaged record is set aside (renamed, bytes kept) and reported; it is never overwritten.
  - There is deliberately no delete method.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import config
from errors import StoreError

SCHEMA_VERSION = 1   # layout of one manual-input record file (separate from config.SCHEMA_VERSION)
_MIN_DATE = date(2000, 1, 1)
_FUTURE_DAYS = 366   # next_result_date may be at most this far ahead
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%B-%Y")
_LOCK = threading.RLock()


class InputRejected(ValueError):
    """The value, date, ticker or document was refused. Nothing on disk was changed."""


@dataclass
class ManualInput:
    ticker: str
    field: str
    value: Any                       # None means deliberately blank
    as_of: str                       # ISO date the value refers to
    source_file: Optional[str] = None
    source_hash: Optional[str] = None
    saved_at: str = ""


# ----------------------------------------------------------------- checks
def check_ticker(ticker: Any) -> str:
    if not isinstance(ticker, str) or not config.TICKER_PATTERN.match(ticker) \
            or not any(ch.isalnum() for ch in ticker):
        raise InputRejected(f"invalid ticker: {ticker!r}")
    return ticker


def check_field(field: Any) -> dict:
    spec = config.MANUAL_FIELDS.get(field) if isinstance(field, str) else None
    if spec is None:
        raise InputRejected(f"unknown field: {field!r}")
    return spec


def _parse_date(raw: Any) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    raise InputRejected(f"not a valid date: {raw!r} (use YYYY-MM-DD)")


def parse_as_of(raw: Any, today: Optional[date] = None) -> date:
    """The as-of date: between 2000-01-01 and today."""
    today = today or date.today()
    d = _parse_date(raw)
    if d < _MIN_DATE:
        raise InputRejected("the as-of date must be 2000-01-01 or later")
    if d > today:
        raise InputRejected("the as-of date cannot be in the future")
    return d


def parse_value(field: str, raw: Any, today: Optional[date] = None) -> Any:
    """Validate and normalise one entered value. Numbers come back as float, dates as ISO text."""
    spec = check_field(field)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise InputRejected("enter a value, or choose leave blank")
    if spec["kind"] == "number":
        if isinstance(raw, bool):
            raise InputRejected("not a number")
        try:
            number = float(raw.strip().replace(",", "").replace("%", "")) if isinstance(raw, str) else float(raw)
        except (TypeError, ValueError):
            raise InputRejected(f"not a number: {raw!r}") from None
        if not math.isfinite(number):
            raise InputRejected("not a finite number")
        if not (spec["min"] <= number <= spec["max"]):
            raise InputRejected(f"must be between {spec['min']:g} and {spec['max']:g}")
        return number
    today = today or date.today()
    d = _parse_date(raw)
    if d < _MIN_DATE or d > today + timedelta(days=_FUTURE_DAYS):
        raise InputRejected("the date is out of range")
    return d.isoformat()


# ------------------------------------------------------------------ store
class ManualInputStore:
    def __init__(self, root=None) -> None:
        self.root = Path(root if root is not None else config.MANUAL_INPUTS_DIR)

    @property
    def values_dir(self) -> Path:
        return self.root / "values"

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    def path_for(self, ticker: str) -> Path:
        return self.values_dir / f"{check_ticker(ticker)}.json"

    # ------------------------------------------------------------ reading
    def get(self, ticker: str) -> dict:
        """All saved entries for a ticker as {field: ManualInput}. Empty if none. May raise StoreError."""
        path = self.path_for(ticker)
        with _LOCK:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                return {}
            except OSError as exc:
                raise StoreError(f"{ticker}: manual inputs could not be read ({exc})") from exc
            try:
                record = json.loads(raw.decode("utf-8"))
                if not isinstance(record, dict) or not isinstance(record.get("fields"), dict):
                    raise ValueError("record has no fields")
            except (UnicodeDecodeError, ValueError) as exc:
                moved = self._set_aside(path, ticker)
                raise StoreError(f"{ticker}: manual inputs file is damaged ({exc}); "
                                 f"kept as {moved.name} and not overwritten") from exc
        if record.get("schema_version") != SCHEMA_VERSION:
            raise StoreError(f"{ticker}: manual inputs schema {record.get('schema_version')!r} "
                             f"is not understood by this code ({SCHEMA_VERSION})")
        out = {}
        for name, item in record["fields"].items():
            if isinstance(item, dict):
                out[name] = ManualInput(ticker=ticker, field=name, value=item.get("value"),
                                        as_of=str(item.get("as_of") or ""),
                                        source_file=item.get("source_file"),
                                        source_hash=item.get("source_hash"),
                                        saved_at=str(item.get("saved_at") or ""))
        return out

    def _set_aside(self, path: Path, ticker: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = path.with_name(f"{ticker}.corrupt-{stamp}")
        try:
            os.replace(path, target)
        except OSError as exc:
            raise StoreError(f"{ticker}: damaged manual inputs could not be set aside ({exc})") from exc
        return target

    def freshness(self, entry: Optional[ManualInput], today: Optional[date] = None) -> str:
        """"missing" (never saved), "fresh" (within the limit) or "stale" (older than the limit)."""
        if entry is None:
            return "missing"
        today = today or date.today()
        try:
            age = (today - _parse_date(entry.as_of)).days
        except InputRejected:
            return "stale"
        return "fresh" if age <= config.MANUAL_INPUT_MAX_AGE_DAYS else "stale"

    def usable_values(self, ticker: str, today: Optional[date] = None) -> dict:
        """{field: value} for fresh, non-blank entries only. Stale or blank values are not used."""
        entries = self.get(ticker)
        return {f: entries[f].value for f in config.MANUAL_FIELDS
                if f in entries and entries[f].value is not None
                and self.freshness(entries[f], today) == "fresh"}

    def pending(self, ticker: str, today: Optional[date] = None) -> list:
        """Fields that are missing or stale, in the order of config.MANUAL_FIELDS."""
        entries = self.get(ticker)
        return [f for f in config.MANUAL_FIELDS
                if self.freshness(entries.get(f), today) in ("missing", "stale")]

    def list_tickers(self) -> list:
        if not self.values_dir.is_dir():
            return []
        names = [p.stem for p in self.values_dir.glob("*.json") if not p.name.startswith(".")]
        return sorted(n for n in names if config.TICKER_PATTERN.match(n) and any(c.isalnum() for c in n))

    # ------------------------------------------------------------ writing
    def save(self, ticker: str, field: str, value: Any, as_of: Any, *, blank: bool = False,
             source_name: Optional[str] = None, source_bytes: Optional[bytes] = None,
             today: Optional[date] = None) -> ManualInput:
        """Validate, then save one entry. Raises InputRejected (nothing changed) or StoreError."""
        check_ticker(ticker)
        check_field(field)
        today = today or date.today()
        as_of_date = parse_as_of(as_of, today)
        stored_value = None if blank else parse_value(field, value, today)
        if (source_name is None) != (source_bytes is None):
            raise InputRejected("a source document needs both a file name and its content")

        with _LOCK:
            entries = self.get(ticker)   # may raise StoreError for a damaged file; never overwritten
            src_file, src_hash = (None, None)
            if source_bytes is not None:
                src_file, src_hash = self._save_source(ticker, field, as_of_date, source_name, source_bytes)
            entry = ManualInput(ticker=ticker, field=field, value=stored_value,
                                as_of=as_of_date.isoformat(), source_file=src_file, source_hash=src_hash,
                                saved_at=datetime.now(timezone.utc).isoformat())
            entries[field] = entry
            self._write_record(ticker, entries)
        return entry

    def _save_source(self, ticker: str, field: str, as_of_date: date, name: str, data: bytes) -> tuple:
        ext = Path(name).suffix.lower() if isinstance(name, str) else ""
        if ext not in config.MANUAL_SOURCE_EXTENSIONS:
            raise InputRejected("that file type is not allowed (allowed: "
                                + ", ".join(config.MANUAL_SOURCE_EXTENSIONS) + ")")
        if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
            raise InputRejected("the source document is empty")
        if len(data) > int(config.MANUAL_SOURCE_MAX_MB * 1024 * 1024):
            raise InputRejected(f"the source document is larger than the {config.MANUAL_SOURCE_MAX_MB:g} MB limit")
        digest = hashlib.sha256(bytes(data)).hexdigest()
        target = self.sources_dir / f"{ticker}_{field}_{as_of_date:%Y_%m_%d}_{digest[:12]}{ext}"
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                return target.name, digest          # the same document is already stored
            raise InputRejected(f"{target.name} already exists with different content")
        self._atomic_write(target, bytes(data), ticker)
        return target.name, digest

    def _write_record(self, ticker: str, entries: dict) -> None:
        envelope = {"schema_version": SCHEMA_VERSION, "ticker": ticker,
                    "fields": {name: {k: v for k, v in asdict(e).items() if k not in ("ticker", "field")}
                               for name, e in entries.items()}}
        try:
            text = json.dumps(envelope, indent=2, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"{ticker}: manual inputs cannot be serialised ({exc})") from exc
        self._atomic_write(self.path_for(ticker), text.encode("utf-8"), ticker)

    @staticmethod
    def _atomic_write(target: Path, data: bytes, label: str) -> None:
        tmp_path: Optional[Path] = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{label}.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
            tmp_path = None
        except Exception as exc:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise StoreError(f"{label}: could not save {target.name} ({exc})") from exc