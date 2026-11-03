"""
filings.py
Version: Step 5, v0.5.0 (2026-11-03). Code version 2026-11-step5.
Spec: README v3.1 DRAFT section 6 (filings); upload box approved 2026-11-03.

Purpose: save, list and remove uploaded filing PDFs in the filings folder. One job,
no UI, no PDF parsing, no knowledge of other engine modules.

Rules implemented here:
  - A filing is stored as TICKER_YYYY_MM_DD_Type.pdf, with the Type taken from a fixed list.
    The names follow what pdf_extractor already uses to grade (A/B/C) and date a document.
  - Checks before saving: valid ticker, date between 2000-01-01 and today, a known type,
    non-empty bytes, size within config.MAX_UPLOAD_MB, and a PDF header.
  - The write is atomic: temp file in the same folder, flushed, then os.replace.
  - Nothing is ever overwritten. Identical content under an existing name is reported as
    "already_present"; different content under an existing name is refused.
  - Removal happens only when explicitly requested, and only for a file that belongs to
    the given ticker (no path tricks).
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import config
from errors import FilingError

# Label shown to the user -> token used in the file name.
FILING_TYPES = {
    "Annual Report": "AnnualReport",
    "Quarterly Results": "Results",
    "Investor Presentation": "Investor_Presentation",
    "Concall Transcript": "Concall_Transcript",
    "Other": "Other",
}
_TOKEN_TO_LABEL = {token: label for label, token in FILING_TYPES.items()}

_NAME_RE = re.compile(
    r"^(?P<ticker>[A-Z0-9&.\-]{1,20})_(?P<y>\d{4})_(?P<m>\d{2})_(?P<d>\d{2})_(?P<type>[A-Za-z_]+)\.pdf$"
)
_MIN_DATE = date(2000, 1, 1)
_LOCK = threading.Lock()


@dataclass
class SaveResult:
    name: str
    path: str
    status: str        # "saved" or "already_present"
    size_bytes: int


@dataclass
class FilingInfo:
    name: str
    size_bytes: int
    doc_date: Optional[date]
    type_label: str    # "Unknown" for names that do not follow the pattern (e.g. TICKER.pdf)


def _folder(folder) -> Path:
    return Path(folder if folder is not None else config.FILINGS_FOLDER)


def _check_ticker(ticker: str) -> str:
    if not isinstance(ticker, str) or not config.TICKER_PATTERN.match(ticker) \
            or not any(ch.isalnum() for ch in ticker):
        raise FilingError(f"invalid ticker: {ticker!r}")
    return ticker


def filing_name(ticker: str, doc_date: date, type_label: str) -> str:
    """Build the standard file name, or raise FilingError."""
    _check_ticker(ticker)
    if not isinstance(doc_date, date):
        raise FilingError("the document date is missing")
    if doc_date < _MIN_DATE:
        raise FilingError("the document date must be 2000-01-01 or later")
    if doc_date > date.today():
        raise FilingError("the document date cannot be in the future")
    token = FILING_TYPES.get(type_label)
    if token is None:
        raise FilingError(f"unknown document type: {type_label!r}")
    return f"{ticker}_{doc_date:%Y_%m_%d}_{token}.pdf"


def save_filing(ticker: str, doc_date: date, type_label: str, data: bytes,
                folder=None) -> SaveResult:
    """Validate and atomically save one filing. Raises FilingError and changes nothing on refusal."""
    name = filing_name(ticker, doc_date, type_label)
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise FilingError("the file is empty")
    limit = int(config.MAX_UPLOAD_MB * 1024 * 1024)
    if len(data) > limit:
        raise FilingError(f"the file is larger than the {config.MAX_UPLOAD_MB} MB limit")
    if b"%PDF-" not in bytes(data[:1024]):
        raise FilingError("the file is not a PDF")

    target_dir = _folder(folder)
    target = target_dir / name
    with _LOCK:
        if target.exists():
            same = hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(bytes(data)).digest()
            if same:
                return SaveResult(name, str(target), "already_present", len(data))
            raise FilingError(
                f"{name} already exists with different content; "
                f"change the date or the type, or remove the old file first"
            )
        tmp_path: Optional[Path] = None
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=f".{name}.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as fh:
                fh.write(bytes(data))
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
            raise FilingError(f"could not save {name} ({exc})") from exc
    return SaveResult(name, str(target), "saved", len(data))


def _belongs_to(ticker: str, name: str) -> bool:
    return name.startswith(f"{ticker}_") or name == f"{ticker}.pdf"


def list_filings(ticker: str, folder=None) -> list[FilingInfo]:
    """Filings stored for one ticker, newest document date first."""
    _check_ticker(ticker)
    target_dir = _folder(folder)
    if not target_dir.is_dir():
        return []
    out: list[FilingInfo] = []
    for path in target_dir.glob("*.pdf"):
        name = path.name
        if name.startswith(".") or not _belongs_to(ticker, name):
            continue
        doc_date, label = None, "Unknown"
        m = _NAME_RE.match(name)
        if m and m.group("ticker") == ticker:
            try:
                doc_date = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
            except ValueError:
                doc_date = None
            label = _TOKEN_TO_LABEL.get(m.group("type"), "Unknown")
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out.append(FilingInfo(name=name, size_bytes=size, doc_date=doc_date, type_label=label))
    out.sort(key=lambda f: (f.doc_date is None, -(f.doc_date.toordinal() if f.doc_date else 0), f.name))
    return out


def remove_filing(ticker: str, name: str, folder=None) -> bool:
    """Remove one stored filing of this ticker. Returns False if it was not there."""
    _check_ticker(ticker)
    if not isinstance(name, str) or os.path.basename(name) != name or not name.endswith(".pdf") \
            or name.startswith(".") or not _belongs_to(ticker, name):
        raise FilingError("that file does not belong to this ticker")
    path = _folder(folder) / name
    with _LOCK:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise FilingError(f"could not remove {name} ({exc})") from exc
    return True
