"""
config.py
Version: Step 1, v0.1.0 (2026-11-01). Code version 2026-11-step1.
Spec: README v3.1 DRAFT (v2.0 is binding where it differs).

Purpose: constants, paths, versions, secret lookup, start-up checks.
Rules:
  - No secret value is ever written here. Secrets come from environment
    variables (Streamlit Community Cloud also exposes its secrets as
    environment variables).
  - Anything marked [FILL FROM v2.0] must be copied from README v2.0,
    never guessed.
  - No scoring weight, cap or threshold is set in this step.
PROMPT_VERSION: no prompt exists yet, so no bump is needed. Set it at step 5.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from errors import ConfigError

# ---------------------------------------------------------------- versions
CODE_VERSION = "2026-11-step1"
SPEC_VERSION = "v3.1-draft"
PROMPT_VERSION = "UNSET"  # set at step 5 (gemini_analyser); v2.0 value [FILL FROM v2.0]
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

# ---------------------------------------------------------------- statuses
STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

# ----------------------------------------------------------------- scoring
# APPROVED: long-term vs short-term weighting of 55 to 45.
LONG_TERM_POINTS = 55
SHORT_TERM_POINTS = 45

# component name -> (block, points), block is "long" or "short".
# Deliberately EMPTY: weights are not approved (OD-1, OD-17) and README v2.0
# values are not available. score_card.py (step 3) fills this once approved.
COMPONENT_WEIGHTS: dict[str, tuple[str, float]] = {}

# ---------------------------------------------------------------- secrets
# Logical name -> environment variable name. [FILL FROM v2.0]
SECRET_ENV_NAMES = {"gemini_api_key": "GEMINI_API_KEY"}


def get_secret(key: str) -> str | None:
    """Return a secret from the environment, or None. Never log the value."""
    env_name = SECRET_ENV_NAMES.get(key)
    value = os.environ.get(env_name) if env_name else None
    return value or None


def has_secret(key: str) -> bool:
    return get_secret(key) is not None


# ------------------------------------------------------- start-up checks
def validate_config() -> list[str]:
    """Fail loudly on a broken split or weights; return non-fatal notices.

    Raises ConfigError if the 55/45 split is broken or if weights are set but
    do not sum to 55 (long) and 45 (short).
    """
    notices: list[str] = []

    if (LONG_TERM_POINTS, SHORT_TERM_POINTS) != (55, 45) \
            or LONG_TERM_POINTS + SHORT_TERM_POINTS != 100:
        raise ConfigError(
            f"Score split must be 55/45 and total 100; got "
            f"{LONG_TERM_POINTS}/{SHORT_TERM_POINTS}."
        )

    if not COMPONENT_WEIGHTS:
        notices.append(
            "Component weights are not set (awaiting owner approval and "
            "README v2.0). Scoring is stubbed and no total is produced."
        )
        return notices

    long_sum = sum(p for block, p in COMPONENT_WEIGHTS.values() if block == "long")
    short_sum = sum(p for block, p in COMPONENT_WEIGHTS.values() if block == "short")
    unknown = {b for b, _ in COMPONENT_WEIGHTS.values()} - {"long", "short"}
    if unknown:
        raise ConfigError(f"Unknown weight block(s): {sorted(unknown)}")
    if abs(long_sum - LONG_TERM_POINTS) > 1e-9 or abs(short_sum - SHORT_TERM_POINTS) > 1e-9:
        raise ConfigError(
            f"Weights must sum to {LONG_TERM_POINTS} (long) and "
            f"{SHORT_TERM_POINTS} (short); got {long_sum} and {short_sum}."
        )
    return notices
