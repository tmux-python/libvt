"""Exceptions raised by libvt."""

from __future__ import annotations


class LibVtError(Exception):
    """Base exception for all libvt errors."""


class ProtocolError(LibVtError):
    """Raised when ``Policy.strict`` rejects a malformed or unsupported sequence."""


class UnsafePasteError(LibVtError):
    """Raised when pasted text contains control or bracket-escape bytes."""


class TerminalTimeoutError(LibVtError, TimeoutError):
    """Raised when an async terminal stream wait exceeds its deadline."""


class ScreenAssertionError(LibVtError, AssertionError):
    """Raised when :func:`~libvt.testing.assert_screen` detects a screen mismatch."""


__all__ = [
    "LibVtError",
    "ProtocolError",
    "ScreenAssertionError",
    "TerminalTimeoutError",
    "UnsafePasteError",
]
