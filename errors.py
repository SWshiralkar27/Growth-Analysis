"""
errors.py
Version: Step 5, v0.5.0 (2026-11-03). Code version 2026-11-step5.
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


class StoreError(PermanentError):
    """The store could not read or write a record safely.

    The prior record on disk is never changed when this is raised.
    """


class FilingError(EngineError):
    """A filing upload or removal was refused; nothing on disk was changed."""
