"""Terminal identity profiles, security policies, and resource ceilings."""

from __future__ import annotations

import enum
import re
import typing as t
from dataclasses import dataclass

type Charset = t.Literal["B", "0", "A"]

_TMUX_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)[a-z]?\s*$")


class ColorScheme(enum.IntEnum):
    """``CSI ? 997 ; Ps n`` color-scheme values (``DEC ?2031`` / ``DSR ?996n``)."""

    DARK = 1
    LIGHT = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class Profile:
    r"""Terminal identity responses and compatibility switches.

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
        (``""`` when unsupported, ``"?"`` for ``tmux``, or full capability
        list on ``DEFAULT_PROFILE``).
    color_scheme : ColorScheme
        Default scheme reported for ``CSI ? 996 n`` color-scheme queries.
    fg_rgb : tuple[int, int, int]
        Default foreground 8-bit RGB reported for ``OSC 10 ; ?``.
    bg_rgb : tuple[int, int, int]
        Default background 8-bit RGB reported for ``OSC 11 ; ?``.
    pending_wrap_phantom_col : bool
        When ``True`` (``TMUX_PROFILE``), ``pending_wrap`` treats the cursor as
        occupying phantom column ``cols`` across ``EL``/``ED``, ``ECH``/``ICH``/
        ``DCH``, vertical motions, ``DECRC``, ``CPR``, and ``CUB`` / ``BS``,
        matching ``tmux``'s ``screen-write.c``.
    vs15_narrows_wide_emoji : bool
        When ``True`` (default), ``U+FE0E`` narrows standalone wide emoji
        listed in ``emoji-variation-sequences.txt`` from ``2`` columns to ``1``.
        ``False`` on ``TMUX_PROFILE`` to match ``tmux 3.8a``.
    vs16_always_wide : bool
        When ``True``, ``U+FE0F`` widens any narrow spacing base rather than
        only ``emoji-variation-sequences.txt`` bases.
    tmux_window_rename : bool
        When ``True`` (``TMUX_PROFILE``), ``ESC k <name> ESC \`` is consumed as
        a window-rename control string rather than printed onto the grid.
    reflow_on_resize : bool
        Default reflow behavior for :meth:`~libvt.terminal.Terminal.resize`
        when ``reflow=None`` (``True`` on ``DEFAULT_PROFILE`` and
        ``TMUX_PROFILE``, ``False`` on ``XTERM_PROFILE``).
    ed3_clears_scrollback_on_alt : bool
        When ``True`` (``XTERM_PROFILE`` and ``TMUX_PROFILE``), ``CSI 3 J``
        executed while the alternate screen is active still clears primary
        scrollback history.
    hpr_vpr_enabled : bool
        When ``True``, ``HPR`` (``CSI Ps a``) and ``VPR`` (``CSI Ps e``) move
        the cursor relatively; when ``False`` (``TMUX_PROFILE``), both are
        quietly ignored.
    il_dl_resets_col : bool
        When ``True`` (ECMA-48 / ``xterm`` / ``Ghostty``), ``IL`` and ``DL``
        move the cursor to the active left margin; when ``False``
        (``TMUX_PROFILE``), the cursor column is preserved.
    rep_wraps : bool
        When ``True`` (default and ``tmux >= 3.9``), ``REP`` (``CSI Ps b``)
        autowraps across the right margin; when ``False`` (``tmux <= 3.8``),
        ``REP`` halts at phantom column ``cols``.
    default_g1_charset : Charset
        Initial ``G1`` character-set designation on startup and ``RIS``
        (``"B"`` US-ASCII by default).
    """

    name: str = "libvt"
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
    vs15_narrows_wide_emoji: bool = True
    vs16_always_wide: bool = False
    tmux_window_rename: bool = False
    reflow_on_resize: bool = True
    ed3_clears_scrollback_on_alt: bool = False
    hpr_vpr_enabled: bool = True
    il_dl_resets_col: bool = True
    rep_wraps: bool = True
    default_g1_charset: Charset = "B"

    @classmethod
    def tmux(cls, version: tuple[int, int] | str = "3.8a") -> Profile:
        """Build a version-specific ``tmux`` compatibility :class:`Profile`."""
        if isinstance(version, tuple):
            major, minor = version
        else:
            m = _TMUX_VERSION_RE.match(version)
            if m is None:
                msg = (
                    f"invalid tmux version {version!r}; expected e.g. '3.8a' or (3, 8)"
                )
                raise ValueError(msg)
            major, minor = int(m.group(1)), int(m.group(2))
        at_least_3_9 = (major, minor) >= (3, 9)
        return cls(
            name="tmux",
            osc_7501_capabilities="?",
            pending_wrap_phantom_col=True,
            vs15_narrows_wide_emoji=False,
            tmux_window_rename=True,
            ed3_clears_scrollback_on_alt=True,
            hpr_vpr_enabled=False,
            il_dl_resets_col=False,
            rep_wraps=at_least_3_9,
        )


DEFAULT_OSC7_HOSTS: frozenset[str] = frozenset({"", "localhost"})


@dataclass(frozen=True, slots=True, kw_only=True)
class Policy:
    r"""Default-deny host security and strictness policy.

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
        Permit ``OSC 7501 ; ?`` capability replies whenever
        ``profile.osc_7501_capabilities`` is non-empty (default ``True``).
    allow_unsafe_uris : bool
        Allow schemes outside ``http://``, ``https://``, and ``mailto:`` in
        HTML ``<a href>`` output (default ``False``).
    allow_passthrough : bool
        Replay unwrapped ``DCS tmux; ESC ESC ... ST`` inner byte streams
        through a persistent passthrough parser in addition to emitting
        :class:`~libvt.events.Passthrough` (default ``False``, matching
        ``tmux``'s ``allow-passthrough off``).
    abort_dispatch_osc : bool
        When ``True``, dispatch an accumulated ``OSC`` string if interrupted by
        a non-``\`` ``ESC`` byte (matching ``tmux`` / ``Ghostty`` state-exit
        behavior). Defaults to ``False`` (DEC STD 070 abort-and-drop).
    osc7_hosts : frozenset[str] | None
        Lowercase hostnames in ``OSC 7`` ``file://<host>/...`` URIs permitted
        to update :attr:`~libvt.terminal.Terminal.cwd`. ``None`` (default)
        uses ``{"", "localhost"}`` in sans-I/O :class:`~libvt.terminal.Terminal`
        and automatically adds the local hostname in
        :func:`~libvt.aio.open_pty`.
    strict : bool
        Raise :exc:`~libvt.exc.ProtocolError` on unrecognized or overflowing
        escape sequences instead of ignoring or recording
        :class:`~libvt.events.UnhandledSequence` (default ``False``).
    """

    allow_title_report: bool = False
    allow_checksum_report: bool = False
    allow_clipboard_read: bool = False
    allow_clipboard_write: bool = True
    allow_color_queries: bool = True
    allow_program_status_query: bool = True
    allow_unsafe_uris: bool = False
    allow_passthrough: bool = False
    abort_dispatch_osc: bool = False
    osc7_hosts: frozenset[str] | None = None
    strict: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
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
        Maximum payload bytes buffered for a ``DCS`` sequence (default
        ``8_388_608``, sized above ``max_apc_bytes`` so 4 MiB graphics wrapped
        in ``DCS tmux;`` fit without truncation).
    max_apc_bytes : int
        Maximum payload bytes buffered for an ``APC`` sequence.
    max_reply_bytes : int
        Maximum unread bytes queued in :meth:`~libvt.terminal.Terminal.drain_replies`.
    max_rep_count : int
        Maximum repeat count executed by ``REP`` (``CSI Ps b``).
    max_prompt_zones : int
        Maximum ``OSC 133`` prompt zones retained on a terminal.
    max_status_records : int
        Maximum concurrent ``OSC 7501`` task records retained in the store
        (default ``256``).
    max_events : int
        Maximum unread :data:`~libvt.events.Event` items retained when
        ``record_events=True``.
    max_event_bytes : int
        Maximum cumulative payload bytes retained across unread events when
        ``record_events=True`` (default ``16_777_216``).
    max_keyboard_stack : int
        Maximum entries per screen in the Kitty ``CSI u`` keyboard stack.
    max_recorder_bytes : int
        Maximum raw input bytes retained by :class:`~libvt.terminal.FlightRecorder`.
    """

    max_grapheme_codepoints: int = 32
    max_csi_params: int = 32
    max_osc_bytes: int = 131_072
    max_dcs_bytes: int = 8_388_608
    max_apc_bytes: int = 4_194_304
    max_reply_bytes: int = 65_536
    max_rep_count: int = 4_096
    max_prompt_zones: int = 1_024
    max_status_records: int = 256
    max_events: int = 4_096
    max_event_bytes: int = 16_777_216
    max_keyboard_stack: int = 8
    max_recorder_bytes: int = 4_096


DEFAULT_PROFILE = Profile()
XTERM_PROFILE = Profile(
    name="xterm",
    osc_7501_capabilities="",
    reflow_on_resize=False,
    ed3_clears_scrollback_on_alt=True,
)
TMUX_PROFILE = Profile.tmux("3.8a")
DEFAULT_POLICY = Policy()
TMUX_CAPTURE_POLICY = Policy(allow_passthrough=True)
DEFAULT_LIMITS = Limits()

__all__ = [
    "DEFAULT_LIMITS",
    "DEFAULT_OSC7_HOSTS",
    "DEFAULT_POLICY",
    "DEFAULT_PROFILE",
    "TMUX_CAPTURE_POLICY",
    "TMUX_PROFILE",
    "XTERM_PROFILE",
    "Charset",
    "ColorScheme",
    "Limits",
    "Policy",
    "Profile",
]
