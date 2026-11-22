"""Exception hierarchy for libvt."""

from __future__ import annotations


class LibVtError(Exception):
    """Base exception for all ``libvt`` errors."""


class TerminalTimeoutError(LibVtError, TimeoutError):
    """Raised when waiting for a terminal state predicate exceeds its timeout."""


class ScreenAssertionError(LibVtError, AssertionError):
    """Raised by :func:`libvt.testing.assert_screen` when screen state differs."""


__all__ = [
    "LibVtError",
    "ScreenAssertionError",
    "TerminalTimeoutError",
]
