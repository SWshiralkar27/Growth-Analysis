"""
store.py
Version: Step 2, v0.2.0 (2026-11-02). Code version 2026-11-step2.
Spec: README v3.1 DRAFT, section 10 (Store) and section 5.2.

Purpose: atomic read and write of one JSON record per ticker.
Rules implemented here:
  - One file per ticker: <root>/<TICKER>.json.
  - A save writes to a temp file in the same folder, flushes it to disk, then
    swaps it in with os.replace. A crash can never leave a half-written record.
  - Serialisation happens BEFORE any file is touched, so a record that cannot be
    serialised (a set, NaN, ...) leaves the prior record exactly as it was.
  - There is deliberately no delete method. An error never removes or replaces
    a prior analysis.
  - A damaged file is set aside (renamed, bytes kept) and reported with a
    StoreError. It is never silently overwritten.
  - Older schema versions are upgraded in memory on read through MIGRATIONS and
    never edited on disk. A newer schema than this code knows is refused.
This module imports only config and errors; it knows nothing about other
engine modules.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import config
from errors import StoreError

# from_version -> function(record) -> record with a higher schema_version.
# Empty today: schema 1 is the first schema. Add an entry when it changes.
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


class Store:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else Path(config.STORE_DIR)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _check_ticker(ticker: str) -> str:
        if not isinstance(ticker, str) or not config.TICKER_PATTERN.match(ticker) \
                or not any(ch.isalnum() for ch in ticker):
            raise StoreError(f"invalid ticker for the store: {ticker!r}")
        return ticker

    def path_for(self, ticker: str) -> Path:
        return self.root / f"{self._check_ticker(ticker)}.json"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # --------------------------------------------------------------- save
    def save(self, ticker: str, record: dict) -> None:
        """Atomically write a record. On any failure the prior file is untouched."""
        path = self.path_for(ticker)
        if not isinstance(record, dict):
            raise StoreError("a record must be a dict")

        envelope = dict(record)
        envelope["ticker"] = ticker
        envelope.setdefault("schema_version", config.SCHEMA_VERSION)
        envelope.setdefault("spec_version", config.SPEC_VERSION)
        envelope.setdefault("prompt_version", config.PROMPT_VERSION)
        envelope.setdefault("saved_at", self._now())
        try:
            text = json.dumps(envelope, indent=2, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"{ticker}: record cannot be serialised ({exc})") from exc

        with self._lock:
            tmp_path: Optional[Path] = None
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=f".{ticker}.",
                                                suffix=".tmp")
                tmp_path = Path(tmp_name)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, path)
                tmp_path = None
            except Exception as exc:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                raise StoreError(f"{ticker}: could not save ({exc})") from exc

    # --------------------------------------------------------------- load
    def load(self, ticker: str) -> Optional[dict]:
        """Return the stored record, None if there is none, or raise StoreError."""
        path = self.path_for(ticker)
        with self._lock:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise StoreError(f"{ticker}: could not read ({exc})") from exc
            try:
                record = json.loads(raw.decode("utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("record is not a JSON object")
            except (UnicodeDecodeError, ValueError) as exc:
                moved = self._set_aside(path, ticker)
                raise StoreError(
                    f"{ticker}: stored record is damaged ({exc}); "
                    f"kept as {moved.name} and not overwritten"
                ) from exc
        return self._migrate(ticker, record)

    def _set_aside(self, path: Path, ticker: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = path.with_name(f"{ticker}.corrupt-{stamp}")
        try:
            os.replace(path, target)
        except OSError as exc:
            raise StoreError(f"{ticker}: damaged record could not be set aside ({exc})") from exc
        return target

    def _migrate(self, ticker: str, record: dict) -> dict:
        version = record.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise StoreError(f"{ticker}: stored record has no valid schema_version")
        current = config.SCHEMA_VERSION
        if version > current:
            raise StoreError(
                f"{ticker}: stored schema {version} is newer than this code ({current}); "
                f"refusing to read it"
            )
        while version < current:
            step = MIGRATIONS.get(version)
            if step is None:
                raise StoreError(f"{ticker}: no migration from schema {version}")
            record = step(dict(record))
            new_version = record.get("schema_version")
            if not isinstance(new_version, int) or new_version <= version:
                raise StoreError(f"{ticker}: migration from schema {version} did not advance it")
            version = new_version
        return record

    # ------------------------------------------------------------ listing
    def exists(self, ticker: str) -> bool:
        return self.path_for(ticker).is_file()

    def list_tickers(self) -> list[str]:
        if not self.root.is_dir():
            return []
        names = []
        for p in self.root.glob("*.json"):
            name = p.stem
            if name.startswith("."):
                continue
            if config.TICKER_PATTERN.match(name) and any(ch.isalnum() for ch in name):
                names.append(name)
        return sorted(names)
