"""ANSI standard and DEC private terminal mode definitions."""

from __future__ import annotations

import enum

DEC_PRIVATE_MASK = 0x8000


class Mode(enum.IntEnum):
    """ANSI standard modes and DEC private modes (``0x8000 | code``)."""

    IRM = 4
    LNM = 20

    DECCKM = DEC_PRIVATE_MASK | 1
    DECOM = DEC_PRIVATE_MASK | 6
    DECAWM = DEC_PRIVATE_MASK | 7
    DECTCEM = DEC_PRIVATE_MASK | 25
    ALT_SCREEN_47 = DEC_PRIVATE_MASK | 47
    DECLRMM = DEC_PRIVATE_MASK | 69
    ALT_SCREEN_1047 = DEC_PRIVATE_MASK | 1047
    DECSC_1048 = DEC_PRIVATE_MASK | 1048
    ALT_SCREEN = DEC_PRIVATE_MASK | 1049
    BRACKETED_PASTE = DEC_PRIVATE_MASK | 2004
    SYNCHRONIZED_OUTPUT = DEC_PRIVATE_MASK | 2026
    GRAPHEME_CLUSTERING = DEC_PRIVATE_MASK | 2027


class DecrpmValue(enum.IntEnum):
    """``DECRPM`` status codes returned in ``CSI ? Ps ; Pm $ y`` replies."""

    NOT_RECOGNIZED = 0
    SET = 1
    RESET = 2
    PERMANENTLY_SET = 3
    PERMANENTLY_RESET = 4


TRANSIENT_SNAPSHOT_MODES: frozenset[Mode] = frozenset(
    {
        Mode.SYNCHRONIZED_OUTPUT,
    },
)

_DEFAULT_ENABLED_MODES: frozenset[Mode] = frozenset(
    {
        Mode.DECAWM,
        Mode.DECTCEM,
        Mode.GRAPHEME_CLUSTERING,
    },
)

_RECOGNIZED_MODES: frozenset[int] = frozenset(int(m) for m in Mode)


class ModeState:
    """Mutable set of active ANSI and DEC private modes."""

    __slots__ = ("_enabled",)

    def __init__(self, enabled: set[Mode] | None = None) -> None:
        self._enabled: set[Mode] = (
            set(_DEFAULT_ENABLED_MODES) if enabled is None else set(enabled)
        )

    def is_set(self, mode: Mode) -> bool:
        """Return ``True`` when *mode* is enabled."""
        return mode in self._enabled

    def set_mode(self, mode: Mode, *, enabled: bool) -> None:
        """Enable or disable *mode*."""
        if enabled:
            self._enabled.add(mode)
        else:
            self._enabled.discard(mode)

    def decrpm(self, code: int, *, private: bool) -> DecrpmValue:
        """Return the ``DECRPM`` status value for raw mode number *code*."""
        raw = (DEC_PRIVATE_MASK | code) if private else code
        if raw not in _RECOGNIZED_MODES:
            return DecrpmValue.NOT_RECOGNIZED
        mode = Mode(raw)
        return DecrpmValue.SET if mode in self._enabled else DecrpmValue.RESET

    def enabled_modes(self) -> frozenset[Mode]:
        """Return an immutable snapshot of currently enabled modes."""
        return frozenset(self._enabled)

    def clone(self) -> ModeState:
        """Return an independent copy of this mode state."""
        return ModeState(self._enabled)


__all__ = [
    "DEC_PRIVATE_MASK",
    "TRANSIENT_SNAPSHOT_MODES",
    "DecrpmValue",
    "Mode",
    "ModeState",
]
