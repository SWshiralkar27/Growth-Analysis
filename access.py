"""
access.py
Version: Step B6, v0.1.0 (2026-11-09). Code version 2026-11-stepB6.

Purpose: the app's password check, and nothing else. No I/O, no Streamlit, no engine modules.

Rules:
  - A password is "required" only when the APP_PASSWORD secret holds some text (blank spaces alone do not
    count). With no password set the app opens as before, so nobody can be locked out by accident.
  - The comparison ignores blank spaces at both ends (a phone keyboard often adds one), is case sensitive,
    and takes the same time however many leading characters are right (hmac.compare_digest).
  - An empty, missing or non-text entry is never accepted.
  - The password itself is never logged, printed or stored here.
"""
from __future__ import annotations

import hmac
from typing import Any


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def password_required(expected: Any) -> bool:
    """True when a password has been set (the secret holds some text)."""
    return _clean(expected) != ""


def password_ok(entered: Any, expected: Any) -> bool:
    """True only when a password is set and the entry matches it."""
    if not password_required(expected):
        return False
    guess = _clean(entered)
    if not guess:
        return False
    try:
        return hmac.compare_digest(guess.encode("utf-8"), _clean(expected).encode("utf-8"))
    except UnicodeError:       # text that cannot be encoded is simply wrong
        return False
