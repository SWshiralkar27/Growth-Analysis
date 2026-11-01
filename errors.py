"""
errors.py
Version: Step 1, v0.1.0 (2026-11-01). Code version 2026-11-step1.
Spec: README v3.1 DRAFT, sections 5.2 and 9.

Purpose: typed errors shared by all modules. Plain exceptions, no logic.
This is the only module (with config.py) that engine modules may import
besides the orchestrator, because exception types must be shared.
"""


class EngineError(Exception):
    """Base class for all engine errors."""


class TransientError(EngineError):
    """A retryable failure such as HTTP 429 or 503.

    The orchestrator keeps the ticker's prior analysis when it sees this.
    """


class PermanentError(EngineError):
    """A failure that retrying will not fix."""


class ConfigError(EngineError):
    """The configuration is inconsistent (for example a broken 55/45 split)."""
