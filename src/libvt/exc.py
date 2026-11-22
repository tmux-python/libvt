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


def _iter_slot_names(cls: type[BaseException]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for base in cls.__mro__:
        raw = base.__dict__.get("__slots__")
        if raw is None:
            continue
        slots = (raw,) if isinstance(raw, str) else raw
        for slot in slots:
            if slot in ("__dict__", "__weakref__"):
                continue
            attr = (
                f"_{base.__name__.lstrip('_')}{slot}"
                if slot.startswith("__") and not slot.endswith("__")
                else slot
            )
            if attr not in seen:
                seen.add(attr)
                names.append(attr)
    return names


def _reconstruct_timeout_error(
    cls: type[TerminalTimeoutError],
    os_args: tuple[object, ...],
) -> TerminalTimeoutError:
    obj = OSError.__new__(cls, *os_args)
    if os_args:
        OSError.__init__(obj, *os_args)
    return obj


class TerminalTimeoutError(LibVtError, TimeoutError):
    """Raised when an async terminal stream or PTY wait exceeds its deadline.

    Parameters
    ----------
    message : str | None
        Concise single-line error summary; omitted when ``None``.
    parser_state : ParserState | None
        Parser state at timeout or premature EOF.
    screen_text : str
        Visible screen text at failure time.
    recent_bytes : collections.abc.Buffer
        Trailing raw bytes from :class:`~libvt.terminal.FlightRecorder`.
    eof : bool
        ``True`` when the stream closed before the predicate matched.
    diagnostics : str
        Pre-rendered viewport box and flight-recorder diagnostic block.
    attach_note : bool
        When ``True`` (default), attach formatted diagnostics via PEP 678
        :meth:`BaseException.add_note` while keeping ``str(exc)`` single-line.

    Attributes
    ----------
    parser_state : ParserState | None
        Parser state at timeout or premature EOF.
    screen_text : str
        Visible screen text at failure time.
    recent_bytes : bytes
        Trailing raw bytes recorded prior to failure.
    eof : bool
        ``True`` when end-of-stream occurred before matching.
    diagnostics : str
        Explicit multi-line diagnostic text, if supplied.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        parser_state: ParserState | None = None,
        screen_text: str = "",
        recent_bytes: collections.abc.Buffer = b"",
        eof: bool = False,
        diagnostics: str = "",
        attach_note: bool = True,
    ) -> None:
        if message is None:
            super().__init__()
        else:
            super().__init__(message)
        self.parser_state = parser_state
        self.screen_text = screen_text
        self.recent_bytes = bytes(recent_bytes)
        self.eof = eof
        self.diagnostics = diagnostics
        if attach_note:
            note = self.format_diagnostics()
            if note:
                self.add_note(note)

    def format_diagnostics(self) -> str:
        """Format structured terminal state and flight-recorder diagnostics.

        Returns
        -------
        str
            Multi-line diagnostic block, or ``""`` when no diagnostic fields are
            populated.
        """
        if self.diagnostics:
            return self.diagnostics
        parts: list[str] = []
        if self.parser_state is not None or self.eof:
            st_name = (
                self.parser_state.name if self.parser_state is not None else "UNKNOWN"
            )
            parts.append(f"parser_state={st_name} eof={self.eof}")
        if self.screen_text:
            parts.append(f"screen_text={self.screen_text!r}")
        if self.recent_bytes:
            parts.append(f"recent_bytes={self.recent_bytes!r}")
        return "\n".join(parts)

    def __reduce__(self) -> tuple[object, ...]:
        """Serialize args, ``__dict__``, ``__notes__``, and subclass ``__slots__``."""
        os_args = OSError.__reduce__(self)[1]
        dict_state = dict(self.__dict__)
        if isinstance(dict_state.get("__notes__"), list):
            dict_state["__notes__"] = list(dict_state["__notes__"])
        slot_state = {
            name: getattr(self, name)
            for name in _iter_slot_names(type(self))
            if hasattr(self, name)
        }
        state: object = (dict_state, slot_state) if slot_state else dict_state
        return (_reconstruct_timeout_error, (type(self), os_args), state)

    def __setstate__(self, state: object) -> None:
        """Restore instance ``__dict__``, ``__notes__`` copy, and ``__slots__``."""
        if isinstance(state, tuple) and len(state) == 2:
            dict_state, slot_state = state
        else:
            dict_state, slot_state = state, None
        self.__dict__.clear()
        if isinstance(dict_state, dict):
            self.__dict__.update(dict_state)
            if isinstance(self.__dict__.get("__notes__"), list):
                self.__dict__["__notes__"] = list(self.__dict__["__notes__"])
        if isinstance(slot_state, dict):
            for key, val in slot_state.items():
                setattr(self, key, val)


class ScreenAssertionError(LibVtError, AssertionError):
    """Raised when :func:`~libvt.testing.assert_screen` detects a screen mismatch."""


__all__ = [
    "LibVtError",
    "ProtocolError",
    "ScreenAssertionError",
    "TerminalTimeoutError",
    "UnsafePasteError",
]
