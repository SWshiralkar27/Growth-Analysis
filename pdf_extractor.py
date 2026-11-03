# =====================================================
# PORTED INTO THE STEP 4 LAYOUT: Step 4, v0.4.0 (2026-11-02). Code version 2026-11-step4.
# The extraction and context-building logic below is the recovered code, UNCHANGED.
# It reads these values from config.py: MAX_PAGE_CHARS, MAX_PROMPT_CHARS, PAGES_PER_SECTION,
# CONTEXT_GOVERNANCE_SHARE, CONTEXT_BUSINESS_SHARE, FILINGS_FOLDER.
# Spec: README v3.1 DRAFT section 6 (filings) and the v2.0 context builder.
# =====================================================
# =====================================================
# pdf_extractor.py
# Per-page extraction, document grading, and a context
# builder that always keeps business content AND adds the
# governance pages (auditor, related-party, pledge).
# =====================================================

from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Optional

from pypdf import PdfReader
from pypdf.errors import PdfReadError

import config

MAX_CHARS = 60_000  # legacy extract_text() cap


# -----------------------------------------------------
# Document grade (framework source hierarchy: A best, C weakest)
# -----------------------------------------------------
_GRADE_B = re.compile(r"presentation|investor|ppt|deck|transcript|concall|earnings[_\-\s]?call|press", re.I)
_GRADE_A = re.compile(r"annual|(?:^|[_\-\s])ar(?:[_\-\s\d]|$)|auditor|shareholding|xbrl|result|filing|sebi|nse|bse|disclosure", re.I)


def classify_document(filename: str) -> str:
    """A = exchange filings / annual report, B = company-prepared material, C = unknown."""
    name = os.path.basename(filename)
    if _GRADE_B.search(name):
        return "B"
    if _GRADE_A.search(name):
        return "A"
    return "C"


_DATE_FULL = re.compile(r"(20\d{2})[_\-.]?(0[1-9]|1[0-2])[_\-.]?(0[1-9]|[12]\d|3[01])(?!\d)")
_DATE_YM = re.compile(r"(20\d{2})[_\-.](0[1-9]|1[0-2])(?!\d)")


def parse_doc_date(filename: str, mtime_fallback: bool = True) -> Optional[date]:
    """
    Date from the file name (YYYY_MM_DD or YYYY_MM). When the name carries no date and
    mtime_fallback is True, fall back to the file's modified time (looked up next to the given path,
    then in config.FILINGS_FOLDER) so cached documents still get an age. extract_document() passes
    mtime_fallback=False because it applies its own, labelled fallback.
    """
    name = os.path.basename(filename)
    m = _DATE_FULL.search(name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _DATE_YM.search(name)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    if mtime_fallback:
        folders = [os.path.dirname(filename), getattr(config, "FILINGS_FOLDER", "filings")]
        for folder in folders:
            if not folder:
                continue
            try:
                return date.fromtimestamp(os.path.getmtime(os.path.join(folder, name)))
            except OSError:
                continue
    return None


# -----------------------------------------------------
# Section patterns (word-boundary regexes; "rpt" no longer matches "excerpt")
# -----------------------------------------------------
SECTION_PATTERNS = {
    "auditor_report": re.compile(
        r"independent auditor|auditors?['’]s? report|basis for (?:qualified |adverse )?opinion|"
        r"key audit matters?|emphasis of matter|qualified opinion|adverse opinion|disclaimer of opinion",
        re.I),
    "related_party": re.compile(r"related[\s\-]party|\brpts?\b", re.I),
    "pledge": re.compile(r"\bpledg\w*|\bencumb\w*", re.I),
    "business": re.compile(
        r"order[\s\-](?:book|backlog|inflow|intake|pipeline|execution)|book[\s\-]to[\s\-]bill|backlog|"
        r"capex|capital expenditure|capacity (?:utili[sz]ation|addition|expansion)|commission(?:ed|ing)|"
        r"outlook|guidance|market share|new (?:market|product|geograph\w*|customer|plant|facilit\w+)|"
        r"pipeline|price (?:hike|increase)|\bcagr\b|letter of (?:award|intent)|export orders?|"
        r"management discussion", re.I),
    # quantified results pages: give the model numbers it can quote for growth claims
    "results": re.compile(
        r"revenue from operations|segment (?:revenue|result)|\bebitda\b|profit before tax|"
        r"year[\s\-]on[\s\-]year|\byo?y\b|y-o-y", re.I),
}
GOVERNANCE_SECTIONS = ("auditor_report", "related_party", "pledge")


# -----------------------------------------------------
# Extraction
# -----------------------------------------------------
def extract_document(pdf_path: str) -> dict:
    """
    Read one PDF into per-page text. Never raises.

    Returns {document, path, grade, doc_date, date_source, page_count,
             pages: [{page, text}], scanned, error}
    """
    name = os.path.basename(pdf_path)
    doc_date = parse_doc_date(name, mtime_fallback=False)
    date_source = "filename"
    if doc_date is None:
        try:
            doc_date = date.fromtimestamp(os.path.getmtime(pdf_path))
            date_source = "file_modified_time"
        except OSError:
            date_source = "unknown"

    result = {
        "document": name, "path": pdf_path, "grade": classify_document(name),
        "doc_date": doc_date, "date_source": date_source,
        "page_count": 0, "pages": [], "scanned": False, "error": None,
    }

    try:
        reader = PdfReader(pdf_path)
    except (PdfReadError, OSError, ValueError) as ex:
        result["error"] = f"could not open PDF: {ex}"
        print(f"  [warn] {name}: {result['error']}")
        return result
    except Exception as ex:  # noqa: BLE001 - pypdf raises broadly on corrupt files
        result["error"] = f"could not read PDF: {ex}"
        print(f"  [warn] {name}: {result['error']}")
        return result

    try:
        page_total = len(reader.pages)
    except Exception as ex:  # noqa: BLE001
        result["error"] = f"could not read page list: {ex}"
        return result
    result["page_count"] = page_total

    for page_number in range(1, page_total + 1):
        try:
            text = reader.pages[page_number - 1].extract_text() or ""
        except Exception as ex:  # noqa: BLE001
            print(f"  [warn] {name} p.{page_number}: extract failed: {ex}")
            continue
        text = text.strip()
        if text:
            result["pages"].append({"page": page_number, "text": text[: config.MAX_PAGE_CHARS]})

    if not result["pages"]:
        result["scanned"] = True
        print(f"  [warn] {name} has no extractable text (likely scanned; needs OCR)")
    return result


def extract_text(pdf_path: str) -> str:
    """Legacy helper: concatenated text capped at MAX_CHARS ('' when unreadable)."""
    doc = extract_document(pdf_path)
    text = "\n".join(p["text"] for p in doc["pages"]).strip()
    return text[:MAX_CHARS]


# -----------------------------------------------------
# Context builder
# -----------------------------------------------------
def _section_hits(text: str) -> dict:
    return {name: len(pat.findall(text)) for name, pat in SECTION_PATTERNS.items()}


def _doc_sort_key(doc: dict):
    d = doc.get("doc_date")
    return (-(d.toordinal()) if d else 0, doc.get("grade", "C"), doc.get("document", ""))


def _cfg(name: str, default):
    return getattr(config, name, default)


def build_context(documents: list[dict], max_chars: Optional[int] = None,
                  pages_per_section: Optional[int] = None) -> tuple[str, dict]:
    """
    Build the document context for the LLM (v2: budgeted, growth-aware).

    Budget order (all shares are of max_chars and can be tuned in config.py):
      1. GOVERNANCE (CONTEXT_GOVERNANCE_SHARE, default 30%): best auditor / related-party /
         pledge pages, taken round-robin across sections and documents (newest document
         first) so no single section or old filing can use the whole reserve.
      2. BUSINESS (CONTEXT_BUSINESS_SHARE, default 45% + unused governance share): pages ranked
         by growth-keyword density x document recency x source grade, with a per-document cap
         when several filings exist, so one annual report cannot crowd out the newest results
         or concall.
      3. FILL: whatever budget remains, keyword pages first, then page order.

    Output format is unchanged: [[DOC: name | GRADE: g | PAGE: n]] headers, so evidence
    verification keeps working. meta["page_index"] still covers EVERY page.

    New meta keys: governance_coverage (which governance sections made it into the context),
    business_pages, selection (chars used per bucket). score_card v2 reads governance_coverage.
    """
    max_chars = max_chars or config.MAX_PROMPT_CHARS
    pages_per_section = pages_per_section or config.PAGES_PER_SECTION
    gov_share = _cfg("CONTEXT_GOVERNANCE_SHARE", 0.30)
    bus_share = _cfg("CONTEXT_BUSINESS_SHARE", 0.45)

    docs = sorted([d for d in documents if d.get("pages")], key=_doc_sort_key)
    doc_rank = {d["document"]: i for i, d in enumerate(docs)}

    page_index: dict = {}
    hits: dict = {}
    for d in docs:
        for p in d["pages"]:
            key = (d["document"], p["page"])
            page_index[key] = p["text"]
            hits[key] = _section_hits(p["text"])  # computed once per page

    selected: set = set()
    used = 0

    def cost(page) -> int:
        return len(page["text"]) + 80  # header allowance

    # ---- 1. governance reserve (round-robin, newest document first) ----------
    gov_cap = int(max_chars * gov_share)
    gov_used = 0
    ranked: dict = {}
    for d in docs:
        for section in GOVERNANCE_SECTIONS:
            scored = [(hits[(d["document"], p["page"])][section], p)
                      for p in d["pages"] if hits[(d["document"], p["page"])][section]]
            scored.sort(key=lambda t: (-t[0], t[1]["page"]))
            ranked[(d["document"], section)] = [p for _, p in scored[:pages_per_section]]

    for r in range(pages_per_section):
        for d in docs:
            for section in GOVERNANCE_SECTIONS:
                lst = ranked[(d["document"], section)]
                if r >= len(lst):
                    continue
                p = lst[r]
                key = (d["document"], p["page"])
                if key in selected:
                    continue
                c = cost(p)
                if gov_used + c <= gov_cap and used + c <= max_chars:
                    selected.add(key)
                    gov_used += c
                    used += c

    # ---- 2. business reserve (growth-keyword ranked) -------------------------
    bus_cap = int(max_chars * bus_share) + (gov_cap - gov_used)
    doc_cap = bus_cap if len(docs) <= 1 else int(bus_cap * 0.6)
    grade_weight = {"A": 1.0, "B": 1.0, "C": 0.8}
    candidates = []
    for d in docs:
        recency = 0.85 ** doc_rank[d["document"]]
        gw = grade_weight.get(d.get("grade", "C"), 0.8)
        for p in d["pages"]:
            key = (d["document"], p["page"])
            if key in selected:
                continue
            h = hits[key]
            raw = min(h["business"], 10) * 2 + min(h["results"], 10)
            if raw > 0:
                candidates.append((-(raw * recency * gw), doc_rank[d["document"]], p["page"], d, p))
    candidates.sort(key=lambda t: t[:3])

    bus_used = 0
    per_doc: dict = {}
    for _, _, _, d, p in candidates:
        key = (d["document"], p["page"])
        c = cost(p)
        if (bus_used + c <= bus_cap and per_doc.get(d["document"], 0) + c <= doc_cap
                and used + c <= max_chars):
            selected.add(key)
            bus_used += c
            used += c
            per_doc[d["document"]] = per_doc.get(d["document"], 0) + c

    # ---- 3. fill: keyword pages first, then page order -----------------------
    rest = []
    for d in docs:
        for p in d["pages"]:
            key = (d["document"], p["page"])
            if key in selected:
                continue
            h = hits[key]
            rest.append((-(min(h["business"], 10) * 2 + min(h["results"], 10)),
                         doc_rank[d["document"]], p["page"], d, p))
    rest.sort(key=lambda t: t[:3])
    for _, _, _, d, p in rest:
        c = cost(p)
        if used + c <= max_chars:
            selected.add((d["document"], p["page"]))
            used += c

    parts: list[str] = []
    for d in docs:
        for p in d["pages"]:
            if (d["document"], p["page"]) in selected:
                parts.append(f"[[DOC: {d['document']} | GRADE: {d['grade']} | PAGE: {p['page']}]]\n{p['text']}")
    context = "\n\n".join(parts)

    newest = max((d["doc_date"] for d in docs if d.get("doc_date")), default=None)
    age_days = (date.today() - newest).days if newest else None
    meta = {
        "page_index": page_index,
        "documents": [d["document"] for d in docs],
        "grades": {d["document"]: d["grade"] for d in docs},
        "pages_included": len(selected),
        "pages_total": len(page_index),
        "chars": len(context),
        "newest_doc_date": newest.isoformat() if newest else None,
        "filing_age_days": age_days,
        # new in v2
        "governance_coverage": [s for s in GOVERNANCE_SECTIONS
                                if any(hits[k][s] > 0 for k in selected)],
        "business_pages": sum(1 for k in selected if hits[k]["business"] > 0),
        "selection": {"governance_chars": gov_used, "business_chars": bus_used, "total_chars": used},
    }
    return context, meta
