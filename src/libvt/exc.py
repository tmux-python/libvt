"""Exceptions raised by libvt."""

from __future__ import annotations

import collections.abc
import typing as t

if t.TYPE_CHECKING:
    from libvt.parser import ParserState


class LibVtError(Exception):
    """Base exception for all libvt errors."""


class ProtocolError(LibVtError):
    """Raised when ``Policy.strict`` rejects a malformed or unsupported sequence."""


class UnsafePasteError(LibVtError):
    """Raised when pasted text contains control or bracket-escape bytes."""


def _rebuild_terminal_timeout_error(
    message: str,
    parser_state: ParserState | None,
    screen_text: str,
    recent_bytes: bytes,
    eof: bool,
) -> TerminalTimeoutError:
    return TerminalTimeoutError(
        message,
        parser_state=parser_state,
        screen_text=screen_text,
        recent_bytes=recent_bytes,
        eof=eof,
    )


class TerminalTimeoutError(LibVtError, TimeoutError):
    """Raised when an async terminal stream wait exceeds its deadline."""

    def __init__(
        self,
        message: str,
        *,
        parser_state: ParserState | None = None,
        screen_text: str = "",
        recent_bytes: bytes = b"",
        eof: bool = False,
    ) -> None:
        super().__init__(message)
        self._message = message
        self.parser_state = parser_state
        self.screen_text = screen_text
        self.recent_bytes = recent_bytes
        self.eof = eof

    def __reduce__(
        self,
    ) -> tuple[
        collections.abc.Callable[..., TerminalTimeoutError],
        tuple[str, ParserState | None, str, bytes, bool],
    ]:
        """Preserve message and diagnostic attributes across ``pickle``."""
        return (
            _rebuild_terminal_timeout_error,
            (
                self._message,
                self.parser_state,
                self.screen_text,
                self.recent_bytes,
                self.eof,
            ),
        )


class ScreenAssertionError(LibVtError, AssertionError):
    """Raised when :func:`~libvt.testing.assert_screen` detects a screen mismatch."""


__all__ = [
    "LibVtError",
    "ProtocolError",
    "ScreenAssertionError",
    "TerminalTimeoutError",
    "UnsafePasteError",
]
