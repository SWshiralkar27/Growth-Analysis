"""
config.py
Version: Step 11a, v0.11.0 (2026-11-07). Code version 2026-11-step11a.
Spec: README v3.1 DRAFT (v2.0 is binding where it differs).

Purpose: constants, paths, versions, secret lookup, start-up checks.
Rules:
  - No secret value is ever written here. Secrets come from environment
    variables (Streamlit Community Cloud also exposes its secrets as
    environment variables).
  - Anything marked [FILL FROM v2.0] must be copied from README v2.0,
    never guessed.
  - No scoring weight, cap or threshold is set in this step.
Changes in v0.11.0 (Step 11a): added the manual-input settings (MANUAL_FIELDS,
MANUAL_INPUT_MAX_AGE_DAYS, MANUAL_SOURCE_*) and added "manual_inputs/" to
PERSIST_PREFIXES so saved inputs and their source documents can be backed up.
Everything else is unchanged.
PROMPT_VERSION: unchanged (2026-11-v3). No bump needed.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from errors import ConfigError

# ---------------------------------------------------------------- versions
CODE_VERSION = "2026-11-step11a"
SPEC_VERSION = "v3.1-draft"
PROMPT_VERSION = "2026-11-v3"  # bump whenever the prompt, JSON schema or scoring changes (v2.0 used 2026-10-v2)
SCHEMA_VERSION = 1

# ------------------------------------------------------------------- paths
# Override with GA_DATA_DIR / GA_REPORTS_DIR to move storage elsewhere.
# On Streamlit Community Cloud the disk is temporary: files written here can
# vanish on restart or redeploy. Keep file access behind store.py (step 2) so
# a persistent backend can replace it later.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GA_DATA_DIR", BASE_DIR / "data"))
REPORTS_DIR = Path(os.environ.get("GA_REPORTS_DIR", BASE_DIR / "reports"))

STORE_DIR = DATA_DIR / "store"
CACHE_DIR = DATA_DIR / "cache"
FILINGS_DIR = DATA_DIR / "filings"
MANUAL_INPUTS_DIR = DATA_DIR / "manual_inputs"
MANUAL_SOURCES_DIR = MANUAL_INPUTS_DIR / "sources"
EVENTS_DIR = DATA_DIR / "events"
PENDING_DIR = DATA_DIR / "pending"
REPORTS_GUI_DIR = REPORTS_DIR / "gui"
REPORTS_BATCH_DIR = REPORTS_DIR / "batch"

ALL_DIRS = (
    STORE_DIR, CACHE_DIR, FILINGS_DIR, MANUAL_INPUTS_DIR, MANUAL_SOURCES_DIR,
    EVENTS_DIR, PENDING_DIR, REPORTS_GUI_DIR, REPORTS_BATCH_DIR,
)


def ensure_dirs() -> None:
    """Create the data and report folders. Call from entry points, not at import."""
    for path in ALL_DIRS:
        path.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------- run limits
HORIZON_MIN_DAYS = 1          # APPROVED: holding horizon 1 to 90 days
HORIZON_MAX_DAYS = 90
DEFAULT_HORIZON_DAYS = 30     # UI starting value only; not a scoring input
GUI_MAX_TICKERS = 10          # SPEC: GUI takes 1 to 10 tickers
DEFAULT_WORKERS = 1           # worker threads; v2.0 value [FILL FROM v2.0]
TICKER_PATTERN = re.compile(r"^[A-Z0-9&.\-]{1,20}$")

# ----------------------------------------------------------------- Gemini
# Values from the README v2.0 configuration reference unless marked otherwise.
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL", "") or "").strip() or "gemini-3.5-flash"
# ^ v2.0 listed "gemini-3.8-flash" ("or current model"); that name could not be verified,
#   so the default is the documented stable Flash model. Override with the GEMINI_MODEL secret.
GEMINI_THINKING_LEVEL = (os.environ.get("GEMINI_THINKING_LEVEL", "") or "low").strip().lower()
GEMINI_MAX_ATTEMPTS = 5
GEMINI_INITIAL_DELAY = 2.0      # seconds, first backoff (recovered from the old reconstruction)
GEMINI_MAX_DELAY = 30.0         # seconds, backoff ceiling (recovered from the old reconstruction)
GEMINI_MAX_WAIT_SECONDS = 90.0  # NEW safety limit: if Google asks for a longer wait, stop retrying
RATE_LIMIT_PER_MINUTE = 5
ALLOW_NO_FILING_ANALYSIS = True  # analyse even with no filings (the scorer then caps the score at 54)
LLM_CACHE_ENABLED = True         # reuse an identical analysis within the same inputs
HORIZON = "the next 2 quarters (about 6 months)"   # wording used inside the prompt

# ----------------------------------------------------- financial data (step 8)
# Values the recovered collector reads. FIN_CACHE_SCHEMA is from the README v2.0 reference;
# the others come from the old reconstruction and are marked [CONFIRM].
PROVIDER_ATTEMPTS = 3            # [CONFIRM]
PROVIDER_RETRY_DELAY = 2.0       # seconds, multiplied by the attempt number [CONFIRM]
MIN_USABLE_FIELDS = 4            # below this the data quality is "insufficient" [CONFIRM]
MERGE_MODE = "fill_missing"      # first provider wins per field, later ones fill gaps [CONFIRM]
ALLOW_DERIVED_MARGINS = False    # [CONFIRM]
DALAL_DE_UNIT = (os.environ.get("DALAL_DE_UNIT", "") or "").strip().lower() or None
ANN_WINDOW_DAYS = 365            # look-back for exchange announcements (the collector's own default)
BSE_CODE_FILE = str(BASE_DIR / "bse_codes.csv")   # optional: columns ticker, bse_code
FIN_CACHE_ENABLED = True
FIN_CACHE_DIR = str(CACHE_DIR / "financials")     # one file per ticker per day
FIN_CACHE_SCHEMA = 2             # README v2.0 configuration reference

# ------------------------------------------------- filings and context builder
# Values from the README v2.0 configuration reference (recovered from the project files).
FILINGS_FOLDER = str(FILINGS_DIR)   # where TICKER_*.pdf files are looked up
MAX_PAGE_CHARS = 6_000              # characters kept per page
MAX_PROMPT_CHARS = 350_000          # interactive context budget
BATCH_MAX_PROMPT_CHARS = 120_000    # batch context budget (about 30k tokens)
PAGES_PER_SECTION = 6
CONTEXT_GOVERNANCE_SHARE = 0.30     # share of the budget reserved for governance pages
CONTEXT_BUSINESS_SHARE = 0.45       # share reserved for growth pages (plus unused governance share)

# What gets mirrored to the private data repository (paths relative to DATA_DIR).
# Step 11a: "manual_inputs/" added (saved manual values and their source documents).
PERSIST_PREFIXES = ("store/", "filings/", "manual_inputs/")
PERSIST_TREE_CACHE_SECONDS = 30

# Largest filing the upload box accepts (approved 2026-11-03).
MAX_UPLOAD_MB = 50

# ------------------------------------------------------ manual