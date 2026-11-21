"""Terminal identity profiles, security policies, and resource ceilings."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ColorScheme(enum.IntEnum):
    """``CSI ? 997 ; Ps n`` color-scheme values (``DEC ?2031`` / ``DSR ?996n``)."""

    DARK = 1
    LIGHT = 2


@dataclass(frozen=True, slots=True)
class Profile:
    """Terminal identity responses and compatibility switches.

    Attributes
    ----------
    name : str
        Human-readable profile identifier.
    da1 : bytes
        Primary Device Attributes (``CSI c``) response bytes.
    da2 : bytes
        Secondary Device Attributes (``CSI > c``) response bytes.
    xtversion : str
        Version banner emitted in ``DCS > | <xtversion> ST`` (``CSI > q``).
    osc_7501_capabilities : str
        Payload body returned for ``OSC 7501 ; ?`` queries when enabled
        (for example
        ``"?:states=idle,working,done,blocked,error:kinds=permission,question,auth"``
        or ``"?"`` for bare ``tmux`` compatibility).
    color_scheme : ColorScheme
        Default scheme reported for ``CSI ? 996 n`` color-scheme queries.
    fg_rgb : tuple[int, int, int]
        Default foreground 8-bit RGB reported for ``OSC 10 ; ?``.
    bg_rgb : tuple[int, int, int]
        Default background 8-bit RGB reported for ``OSC 11 ; ?``.
    pending_wrap_phantom_col : bool
        When ``True`` (``TMUX_PROFILE``), ``pending_wrap`` treats the cursor as
        occupying phantom column ``cols`` on ``CUB`` / ``BS`` and allows ``BS``
        at column ``0`` to step onto the end of a soft-wrapped predecessor row,
        matching ``tmux``'s ``screen-write.c``. When ``False`` (default),
        ``CUB`` / ``BS`` follow ``xterm`` / ``Ghostty`` rules.
    """

    name: str = "xterm"
    da1: bytes = b"\x1b[?62;22c"
    da2: bytes = b"\x1b[>1;10;0c"
    xtversion: str = "libvt 0.1"
    osc_7501_capabilities: str = (
        "?:states=idle,working,done,blocked,error:kinds=permission,question,auth"
    )
    color_scheme: ColorScheme = ColorScheme.DARK
    fg_rgb: tuple[int, int, int] = (0xE5, 0xE5, 0xE5)
    bg_rgb: tuple[int, int, int] = (0x1E, 0x1E, 0x1E)
    pending_wrap_phantom_col: bool = False


_DEFAULT_OSC7_HOSTS: frozenset[str] = frozenset({"", "localhost"})


@dataclass(frozen=True, slots=True)
class Policy:
    """Default-deny host security and strictness policy.

    Attributes
    ----------
    allow_title_report : bool
        Permit ``CSI 21 t`` window-title report replies (default ``False``).
    allow_checksum_report : bool
        Permit ``DECRQCRA`` rectangular checksum replies (default ``False``).
    allow_clipboard_read : bool
        Permit ``OSC 52 ; ... ; ?`` clipboard read replies (default ``False``).
    allow_clipboard_write : bool
        Emit :class:`~libvt.events.ClipboardWrite` events on ``OSC 52`` writes
        (default ``True``; no host clipboard is ever mutated by ``libvt``).
    allow_color_queries : bool
        Permit ``OSC 10 ; ?``, ``OSC 11 ; ?``, and ``CSI ? 996 n`` palette and
        color-scheme replies (default ``True``).
    allow_program_status_query : bool
        Force ``OSC 7501 ; ?`` capability replies even when neither ``on_event``
        nor ``record_events`` is enabled (default ``False``, matching
        ``libghostty-vt``).
    allow_unsafe_uris : bool
        Allow schemes outside ``http://``, ``https://``, and ``mailto:`` in
        HTML ``<a href>`` output (default ``False``).
    allow_passthrough : bool
        Re-feed unwrapped ``DCS tmux; ESC ESC ... ST`` inner byte streams into
        :class:`~libvt.terminal.Terminal` in addition to emitting
        :class:`~libvt.events.Passthrough` (default ``False``, matching
        ``tmux``'s ``allow-passthrough off``).
    osc7_hosts : frozenset[str]
        Lowercase hostnames in ``OSC 7`` ``file://<host>/...`` URIs permitted
        to update :attr:`~libvt.terminal.Terminal.cwd` (default
        ``frozenset({"", "localhost"})``; :func:`~libvt.aio.open_pty` adds the
        local machine hostname automatically).
    strict : bool
        Raise :exc:`~libvt.exc.ProtocolError` on unrecognized escape sequences
        instead of ignoring or recording :class:`~libvt.events.UnhandledSequence`
        (default ``False``).
    """

    allow_title_report: bool = False
    allow_checksum_report: bool = False
    allow_clipboard_read: bool = False
    allow_clipboard_write: bool = True
    allow_color_queries: bool = True
    allow_program_status_query: bool = False
    allow_unsafe_uris: bool = False
    allow_passthrough: bool = False
    osc7_hosts: frozenset[str] = _DEFAULT_OSC7_HOSTS
    strict: bool = False


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource ceilings protecting memory and CPU against hostile byte streams.

    Attributes
    ----------
    max_grapheme_codepoints : int
        Maximum Unicode codepoints stored in a single grid cell (default ``32``).
    max_csi_params : int
        Maximum parameter slots retained in a ``CSI`` or ``DCS`` header.
    max_osc_bytes : int
        Maximum payload bytes buffered for an ``OSC`` sequence.
    max_dcs_bytes : int
        Maximum payload bytes buffered for a ``DCS`` sequence.
    max_apc_bytes : int
        Maximum payload bytes buffered for an ``APC`` sequence.
    max_reply_bytes : int
        Maximum unread bytes queued in :meth:`~libvt.terminal.Terminal.drain_replies`.
    max_rep_count : int
        Maximum repeat count executed by ``REP`` (``CSI Ps b``).
    max_prompt_zones : int
        Maximum ``OSC 133`` prompt zones retained on a terminal.
    max_status_records : int
        Maximum concurrent ``OSC 7501`` task records retained in the store.
    max_events : int
        Maximum unread :data:`~libvt.events.Event` items retained when
        ``record_events=True``.
    max_keyboard_stack : int
        Maximum entries per screen in the Kitty ``CSI u`` keyboard stack.
    max_recorder_bytes : int
        Maximum raw input bytes retained by :class:`~libvt.terminal.FlightRecorder`.
    """

    max_grapheme_codepoints: int = 32
    max_csi_params: int = 32
    max_osc_bytes: int = 131_072
    max_dcs_bytes: int = 4_194_304
    max_apc_bytes: int = 4_194_304
    max_reply_bytes: int = 65_536
    max_rep_count: int = 4_096
    max_prompt_zones: int = 1_024
    max_status_records: int = 64
    max_events: int = 4_096
    max_keyboard_stack: int = 8
    max_recorder_bytes: int = 4_096


XTERM_PROFILE = Profile()
DEFAULT_PROFILE = XTERM_PROFILE
TMUX_PROFILE = Profile(
    name="tmux",
    osc_7501_capabilities="?",
    pending_wrap_phantom_col=True,
)
DEFAULT_POLICY = Policy()
DEFAULT_LIMITS = Limits()

__all__ = [
    "DEFAULT_LIMITS",
    "DEFAULT_POLICY",
    "DEFAULT_PROFILE",
    "TMUX_PROFILE",
    "XTERM_PROFILE",
    "ColorScheme",
    "Limits",
    "Policy",
    "Profile",
]
