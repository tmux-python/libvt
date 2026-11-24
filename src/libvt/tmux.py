r"""Sans-I/O ``tmux`` ``capture-pane`` and control-mode (``-CC``) decoders."""

from __future__ import annotations

import collections.abc
import contextlib
import enum
import re
import secrets
from dataclasses import dataclass

from libvt._grid import WidthRole, _Row, raw_width_bits
from libvt.config import (
    DEFAULT_LIMITS,
    TMUX_CAPTURE_POLICY,
    TMUX_PROFILE,
    Limits,
    Policy,
    Profile,
    _parse_tmux_version,
)
from libvt.events import PromptKind, PromptZone
from libvt.modes import DEC_PRIVATE_MASK, Mode
from libvt.parser import CsiParams, Parser, _parse_ascii_int, _parse_ascii_uint
from libvt.screen import Screen, ScrollRegion
from libvt.style import DEFAULT_STYLE
from libvt.terminal import Snapshot, Terminal

#: Accepted input payload shapes for ``tmux capture-pane`` decoders.
type CaptureInput = str | bytes | bytearray | collections.abc.Sequence[str]

#: Format string passed to ``tmux display-message -p`` for pane metadata.
TMUX_METADATA_FORMAT: str = (
    "#{pane_width}\t#{pane_height}\t#{cursor_x}\t#{cursor_y}\t"
    "#{cursor_flag}\t#{alternate_on}\t#{alternate_saved_x}\t"
    "#{alternate_saved_y}\t#{scroll_region_upper}\t#{scroll_region_lower}\t"
    "#{insert_flag}\t#{origin_flag}\t#{wrap_flag}\t#{keypad_cursor_flag}\t"
    "#{bracket_paste_flag}\t#{mouse_standard_flag}\t#{mouse_button_flag}\t"
    "#{mouse_all_flag}\t#{mouse_sgr_flag}\t#{pane_private_modes}\t"
    "#{pane_tabs}\t#{history_size}\t#{history_limit}\t"
    "#{synchronized_output_flag}\t#{pane_output_generation}\t"
    "#{pane_current_path}\t#{pane_title}"
)

_FLAG_TOKEN_RE = re.compile(r"^(?:-|D?H?O?P?W?X?)\Z", re.ASCII)
_NONCE_RE = re.compile(r"^[0-9A-Za-z_-]{8,128}\Z", re.ASCII)
_OCTAL_ESCAPE_RE = re.compile(rb"\\([0-3][0-7]{2})")
_TMUX_UINT_MAX = 0xFFFF_FFFF
_TMUX_MAX_DIM = 10_000
_TMUX_MAX_HISTORY = 1_000_000

_BASE_UNEXPOSED_FIELDS: frozenset[str] = frozenset(
    {
        "active_pen_sgr",
        "cursor_shape",
        "decom_saved_cursor",
        "decsc_saved_cursor",
        "g0_g3_charsets",
        "gl_shift",
        "kitty_keyboard_flags",
    },
)


class TmuxLineFlags(enum.IntFlag):
    """Per-line flags emitted by ``tmux capture-pane -F`` (``tmux >= 3.8``)."""

    #: No per-line flags set (``-``).
    NONE = 0
    #: Pane exited / dead marker (``D``).
    DEAD = 1
    #: Line contains an ``OSC 8`` hyperlink (``H``).
    HYPERLINK = 2
    #: ``OSC 133;C`` command output start (``O``).
    START_OUTPUT = 4
    #: ``OSC 133;A`` shell prompt start (``P``).
    START_PROMPT = 8
    #: Line soft-wrapped onto the next grid row (``W``).
    WRAPPED = 16
    #: Line uses extended grid cell storage (``X``).
    EXTENDED = 32


_CHAR_TO_LINE_FLAG: dict[str, TmuxLineFlags] = {
    "D": TmuxLineFlags.DEAD,
    "H": TmuxLineFlags.HYPERLINK,
    "O": TmuxLineFlags.START_OUTPUT,
    "P": TmuxLineFlags.START_PROMPT,
    "W": TmuxLineFlags.WRAPPED,
    "X": TmuxLineFlags.EXTENDED,
}


def parse_flagged_line(line: str) -> tuple[TmuxLineFlags, str]:
    r"""Split a ``tmux capture-pane -F`` line into ``(flags, body)``.

    Parameters
    ----------
    line : str
        Single line emitted by ``tmux capture-pane -F`` (without trailing
        ``\n``).

    Returns
    -------
    tuple[TmuxLineFlags, str]
        Parsed :class:`TmuxLineFlags` and remaining SGR/text payload.

    Raises
    ------
    ValueError
        If *line* does not start with a valid ``<flags>`` space-delimited prefix.
    """
    raw_flags, sep, body = line.partition(" ")
    if not sep or not raw_flags or _FLAG_TOKEN_RE.match(raw_flags) is None:
        msg = f"invalid tmux capture-pane -F line prefix: {line[:24]!r}"
        raise ValueError(msg)
    flags = TmuxLineFlags.NONE
    if raw_flags != "-":
        for ch in raw_flags:
            flags |= _CHAR_TO_LINE_FLAG[ch]
    return flags, body


def unescape_tmux_control_output(data: collections.abc.Buffer | str) -> bytes:
    r"""Decode ``tmux -CC`` ``%output`` octal escapes into raw PTY bytes.

    ``tmux`` control mode escapes bytes ``< 0x20`` and backslash (``0x5c`` ->
    ``\134``) as three-digit octal sequences ``\ooo`` while passing printable
    ASCII and UTF-8 bytes verbatim (including split multi-byte UTF-8 sequences).

    Parameters
    ----------
    data : collections.abc.Buffer | str
        Escaped payload following ``%output %<pane_id>``.

    Returns
    -------
    bytes
        Unescaped raw VT byte stream ready for :meth:`~libvt.terminal.Terminal.feed`.
    """
    raw = (
        data.encode("utf-8", errors="surrogateescape")
        if isinstance(data, str)
        else bytes(data)
    )
    if b"\\" not in raw:
        return raw
    return _OCTAL_ESCAPE_RE.sub(lambda m: bytes((int(m.group(1), 8),)), raw)


def _opt_int(raw: str, default: int) -> int:
    val = _parse_ascii_int(raw.strip(), min_val=-2_147_483_648, max_val=2_147_483_647)
    return default if val is None else val


def _opt_saved_coord(raw: str) -> int | None:
    return _parse_ascii_uint(raw.strip(), max_val=_TMUX_UINT_MAX - 1)


def _opt_bool(raw: str, *, default: bool = False) -> bool:
    s = raw.strip()
    if not s:
        return default
    return s != "0"


@dataclass(frozen=True, slots=True)
class TmuxPaneMetadata:
    """Out-of-band ``tmux`` pane state queried via :data:`TMUX_METADATA_FORMAT`.

    Attributes
    ----------
    cols : int
        Pane width in columns (``#{pane_width}``).
    rows : int
        Pane height in rows (``#{pane_height}``).
    cursor_x : int
        Raw zero-based ``#{cursor_x}`` (equals ``cols`` when ``pending_wrap``
        is active in ``tmux``).
    cursor_y : int
        Zero-based ``#{cursor_y}``.
    cursor_visible : bool
        ``True`` when ``#{cursor_flag}`` is non-zero.
    alternate_on : bool
        ``True`` when ``#{alternate_on}`` indicates an active alternate screen.
    alternate_saved_x : int | None
        Saved primary-screen ``x`` (``#{alternate_saved_x}``; ``None`` when
        ``tmux`` reports ``UINT_MAX`` under ``?47``/``?1047``).
    alternate_saved_y : int | None
        Saved primary-screen ``y`` (``#{alternate_saved_y}``; ``None`` when
        unset).
    scroll_region_upper : int
        Zero-based top scroll margin (``#{scroll_region_upper}``).
    scroll_region_lower : int | None
        Zero-based bottom scroll margin (``None`` defaults to ``rows - 1``).
    insert_flag : bool
        ``IRM`` insert mode (``#{insert_flag}``).
    origin_flag : bool
        ``DECOM`` origin mode (``#{origin_flag}``).
    wrap_flag : bool
        ``DECAWM`` autowrap mode (``#{wrap_flag}``).
    keypad_cursor_flag : bool
        ``DECCKM`` application cursor keys (``#{keypad_cursor_flag}``).
    bracketed_paste_flag : bool
        ``?2004`` bracketed paste mode (``#{bracket_paste_flag}``).
    mouse_standard_flag : bool
        ``?1000`` VT200 mouse reporting (``#{mouse_standard_flag}``).
    mouse_button_flag : bool
        ``?1002`` button-event mouse reporting (``#{mouse_button_flag}``).
    mouse_any_flag : bool
        ``?1003`` all-motion mouse reporting (``#{mouse_all_flag}``).
    mouse_sgr_flag : bool
        ``?1006`` SGR mouse encoding (``#{mouse_sgr_flag}``).
    focus_events_flag : bool
        ``?1004`` focus-in/out reporting (from ``#{pane_private_modes}``).
    private_modes : frozenset[int]
        DEC private mode numbers parsed from ``#{pane_private_modes}``.
    pane_tabs : tuple[int, ...] | None
        Sorted zero-based tabstop columns from ``#{pane_tabs}``, or ``None`` to
        use standard 8-column tabstops.
    history_size : int
        Current scrollback line count (``#{history_size}``).
    history_limit : int
        Pane ``history-limit`` (``#{history_limit}``).
    synchronized : bool
        ``?2026`` synchronized-output hold state
        (``#{synchronized_output_flag}``).
    output_generation : int | None
        ``#{pane_output_generation}`` counter on ``tmux >= 3.8``, else ``None``.
    cwd : str
        ``#{pane_current_path}``, if available.
    title : str
        ``#{pane_title}``, if available.
    """

    cols: int = 80
    rows: int = 24
    cursor_x: int = 0
    cursor_y: int = 0
    cursor_visible: bool = True
    alternate_on: bool = False
    alternate_saved_x: int | None = None
    alternate_saved_y: int | None = None
    scroll_region_upper: int = 0
    scroll_region_lower: int | None = None
    insert_flag: bool = False
    origin_flag: bool = False
    wrap_flag: bool = True
    keypad_cursor_flag: bool = False
    bracketed_paste_flag: bool = False
    mouse_standard_flag: bool = False
    mouse_button_flag: bool = False
    mouse_any_flag: bool = False
    mouse_sgr_flag: bool = False
    focus_events_flag: bool = False
    private_modes: frozenset[int] = frozenset()
    pane_tabs: tuple[int, ...] | None = None
    history_size: int = 0
    history_limit: int = 2_000
    synchronized: bool = False
    output_generation: int | None = None
    cwd: str = ""
    title: str = ""

    @property
    def pending_wrap(self) -> bool:
        """Return ``True`` when ``cursor_x >= cols`` encodes ``pending_wrap``."""
        return self.cursor_x >= self.cols and self.wrap_flag

    @property
    def clamped_cursor_col(self) -> int:
        """Return zero-based cursor column clamped into ``[0, cols - 1]``."""
        return min(max(0, self.cursor_x), max(0, self.cols - 1))

    @property
    def clamped_cursor_row(self) -> int:
        """Return zero-based cursor row clamped into ``[0, rows - 1]``."""
        return min(max(0, self.cursor_y), max(0, self.rows - 1))

    @classmethod
    def from_format_line(cls, line: str) -> TmuxPaneMetadata:
        """Parse one line emitted by ``display-message -p TMUX_METADATA_FORMAT``."""
        cleaned = line.rstrip("\r\n")
        parts = cleaned.split("\t", 26)
        if len(parts) < 27:
            parts.extend([""] * (27 - len(parts)))
        cols = min(_TMUX_MAX_DIM, max(1, _opt_int(parts[0], 80)))
        rows = min(_TMUX_MAX_DIM, max(1, _opt_int(parts[1], 24)))

        priv_raw = parts[19].strip()
        priv_modes: set[int] = set()
        focus_on = False
        if priv_raw in ("0", "1"):
            focus_on = priv_raw == "1"
        elif priv_raw:
            for tok in priv_raw.split(","):
                p_str = tok.strip().lstrip("?")
                p_val = _parse_ascii_uint(p_str, max_val=65_535)
                if p_val is not None:
                    priv_modes.add(p_val)
            focus_on = 1004 in priv_modes

        tabs_raw = parts[20].strip()
        tabs: tuple[int, ...] | None = None
        if tabs_raw:
            parsed_tabs: list[int] = []
            for tok in tabs_raw.split(","):
                t_val = _parse_ascii_uint(tok.strip(), max_val=65_535)
                if t_val is not None:
                    parsed_tabs.append(t_val)
            tabs = tuple(sorted(set(parsed_tabs)))
        gen_raw = parts[24].strip()
        out_gen = _parse_ascii_uint(gen_raw, max_val=(1 << 64) - 1)
        sr_low_raw = parts[9].strip()
        sr_val = _parse_ascii_uint(sr_low_raw, max_val=65_535)
        sr_lower = sr_val if sr_val is not None else (rows - 1)
        return cls(
            cols=cols,
            rows=rows,
            cursor_x=_opt_int(parts[2], 0),
            cursor_y=_opt_int(parts[3], 0),
            cursor_visible=_opt_bool(parts[4], default=True),
            alternate_on=_opt_bool(parts[5]),
            alternate_saved_x=_opt_saved_coord(parts[6]),
            alternate_saved_y=_opt_saved_coord(parts[7]),
            scroll_region_upper=_opt_int(parts[8], 0),
            scroll_region_lower=sr_lower,
            insert_flag=_opt_bool(parts[10]),
            origin_flag=_opt_bool(parts[11]),
            wrap_flag=_opt_bool(parts[12], default=True),
            keypad_cursor_flag=_opt_bool(parts[13]),
            bracketed_paste_flag=_opt_bool(parts[14]) or (2004 in priv_modes),
            mouse_standard_flag=_opt_bool(parts[15]) or (1000 in priv_modes),
            mouse_button_flag=_opt_bool(parts[16]) or (1002 in priv_modes),
            mouse_any_flag=_opt_bool(parts[17]) or (1003 in priv_modes),
            mouse_sgr_flag=_opt_bool(parts[18]) or (1006 in priv_modes),
            focus_events_flag=focus_on,
            private_modes=frozenset(priv_modes),
            pane_tabs=tabs,
            history_size=max(0, min(_TMUX_MAX_HISTORY, _opt_int(parts[21], 0))),
            history_limit=max(0, min(_TMUX_MAX_HISTORY, _opt_int(parts[22], 2_000))),
            synchronized=_opt_bool(parts[23]) or (2026 in priv_modes),
            output_generation=out_gen,
            cwd=parts[25],
            title=parts[26],
        )


@dataclass(frozen=True, slots=True)
class CaptureCompleteness:
    """Explicit fidelity report for a decoded ``tmux capture-pane`` snapshot.

    Attributes
    ----------
    exact_cells : bool
        ``True`` when cell glyphs, styles, and tab expansions were decoded from
        ``-e -p -N`` (or ``-e -p -N -T``) output.
    exact_wraps : bool
        ``True`` when soft-wrap flags came from ``-F`` or an exact ``-J``
        companion capture.
    exact_generation_bracket : bool
        ``True`` when pre- and post-capture ``pane_output_generation`` counters
        were supplied and matched.
    unexposed_fields : frozenset[str]
        Internal terminal state fields that ``tmux`` does not expose through
        format variables or ``capture-pane``.
    """

    exact_cells: bool
    exact_wraps: bool
    exact_generation_bracket: bool
    unexposed_fields: frozenset[str]


@dataclass(frozen=True, slots=True)
class TmuxCaptureResult:
    """Decoded ``tmux`` capture bundle ready for inspection or live streaming.

    Attributes
    ----------
    terminal : Terminal
        Reconstructed :class:`~libvt.terminal.Terminal` configured with
        ``profile`` and ready for subsequent :meth:`~libvt.terminal.Terminal.feed`
        bytes.
    snapshot : Snapshot
        Immutable :class:`~libvt.terminal.Snapshot` of *terminal*.
    completeness : CaptureCompleteness
        Fidelity flags and terminal fields not exposed by ``tmux``.
    line_flags : tuple[TmuxLineFlags, ...]
        Per-line flags across ``history + viewport`` (all ``NONE`` when
        ``flagged=False``).
    """

    terminal: Terminal
    snapshot: Snapshot
    completeness: CaptureCompleteness
    line_flags: tuple[TmuxLineFlags, ...]


def _rewrite_tmux_overline_sgr(params: CsiParams) -> CsiParams:
    """Rewrite ``tmux`` ``grid_string_cells_code`` ``5:3`` SGR groups to ``53``."""
    if params.colon_mask == 0 or 5 not in params.values:
        return params
    vals = params.values
    n = len(vals)
    out_vals: list[int] = []
    out_colon = 0
    out_omitted = 0
    changed = False
    idx = 0
    while idx < n:
        g_end = idx + 1
        while g_end < n and ((params.colon_mask >> g_end) & 1):
            g_end += 1
        g_len = g_end - idx
        head = vals[idx]
        head_omitted = bool((params.omitted_mask >> idx) & 1)
        if g_len == 1 and not head_omitted and head in (38, 48, 58) and g_end < n:
            nxt_end = g_end + 1
            while nxt_end < n and ((params.colon_mask >> nxt_end) & 1):
                nxt_end += 1
            mode_val = vals[g_end]
            extra_groups = 2 if mode_val == 5 else (4 if mode_val == 2 else 1)
            skip_end = nxt_end
            complete = True
            for _ in range(extra_groups - 1):
                if skip_end >= n:
                    complete = False
                    break
                skip_end += 1
                while skip_end < n and ((params.colon_mask >> skip_end) & 1):
                    skip_end += 1
            if not complete:
                skip_end = nxt_end
            for k in range(idx, skip_end):
                pos = len(out_vals)
                out_vals.append(vals[k])
                if (params.colon_mask >> k) & 1:
                    out_colon |= 1 << pos
                if (params.omitted_mask >> k) & 1:
                    out_omitted |= 1 << pos
            idx = skip_end
            continue
        if (
            g_len == 2
            and not head_omitted
            and head == 5
            and vals[idx + 1] == 3
            and not ((params.omitted_mask >> (idx + 1)) & 1)
        ):
            out_vals.append(53)
            changed = True
            idx = g_end
            continue
        for k in range(idx, g_end):
            pos = len(out_vals)
            out_vals.append(vals[k])
            if (params.colon_mask >> k) & 1:
                out_colon |= 1 << pos
            if (params.omitted_mask >> k) & 1:
                out_omitted |= 1 << pos
        idx = g_end
    return CsiParams(tuple(out_vals), out_colon, out_omitted) if changed else params


class _CaptureRowSink:
    r"""Internal parser sink expanding ``GRID_FLAG_TAB`` ``\t`` with active SGR."""

    __slots__ = ("_cols", "_scratch", "_tabstops")

    def __init__(
        self,
        scratch: Terminal,
        cols: int,
        tabstops: tuple[int, ...],
    ) -> None:
        self._scratch = scratch
        self._cols = cols
        self._tabstops = tabstops

    def on_print(self, text: str) -> None:
        self._scratch._handle_print(text)

    def on_execute(self, byte: int) -> None:
        if byte == 0x09:
            scr = self._scratch.screen
            eff_col = 0 if scr._pending_wrap else scr._cur_col
            mod_col = eff_col % self._cols if self._cols > 0 else eff_col
            nxt_mod = next(
                (t_col for t_col in self._tabstops if mod_col < t_col < self._cols),
                max(mod_col, self._cols - 1),
            )
            span = max(1, nxt_mod - mod_col)
            self._scratch._handle_print(" " * span)
            return
        if byte in (0x0E, 0x0F):
            self._scratch._handle_execute(byte)

    def on_esc(self, intermediates: bytes, final: int) -> None:
        return

    def on_csi(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
    ) -> None:
        if prefix == 0 and not intermediates and final == 0x6D:
            self._scratch._handle_csi(
                prefix,
                _rewrite_tmux_overline_sgr(params),
                intermediates,
                final,
            )

    def on_osc(self, code: int, data: bytes, terminator: bytes) -> None:
        if code == 8:
            self._scratch._handle_osc(code, data, terminator)

    def on_dcs(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
        data: bytes,
    ) -> None:
        return

    def on_apc(self, data: bytes) -> None:
        return


def _split_capture_lines(raw: CaptureInput) -> list[str]:
    if isinstance(raw, collections.abc.Sequence) and not isinstance(
        raw, (str, bytes, bytearray)
    ):
        return [ln.rstrip("\r\n") for ln in raw]
    text = (
        bytes(raw).decode("utf-8", errors="replace")
        if isinstance(raw, (bytes, bytearray))
        else raw
    )
    text = text.removesuffix("\n").removesuffix("\r")
    if not text:
        return []
    return [ln.removesuffix("\r") for ln in text.split("\n")]


def _row_trimmed_text_and_cols(row: _Row) -> tuple[str, int]:
    t_end = row.text_used
    while t_end > 0 and row.char_at(t_end - 1) == " ":
        t_end -= 1
    return row.slice_text(0, t_end), t_end


def _infer_wraps_from_joined(
    plain_rows: list[str],
    joined_raw: CaptureInput,
    *,
    stripped_trailing: int = 0,
    trim_trailing: bool = False,
    cols: int,
    tabstops: tuple[int, ...],
    profile: Profile,
    policy: Policy,
    limits: Limits,
) -> tuple[list[bool], list[bool]] | None:
    n = len(plain_rows)
    raw_lines = _split_capture_lines(joined_raw)
    if (
        stripped_trailing > 0
        and isinstance(joined_raw, collections.abc.Sequence)
        and not isinstance(joined_raw, (str, bytes, bytearray))
    ):
        raw_lines.extend([""] * stripped_trailing)
    if n == 0:
        return ([], []) if not raw_lines else None
    if not raw_lines or len(raw_lines) > n:
        return None

    eff_cols = max(1, cols)
    sb_cap = max(64, n + 4)
    max_line_bytes = max(65_536, eff_cols * 64 * min(64, n - len(raw_lines) + 2))
    scratch = Terminal(
        rows=1,
        cols=eff_cols,
        scrollback=sb_cap,
        profile=profile,
        policy=policy,
        limits=limits,
    )
    scratch.modes.set_mode(Mode.DECAWM, enabled=True)
    scratch._g_sets = ["B", "0", "B", "B"]
    scratch._gl = 0
    parser = Parser(limits=limits)
    sink = _CaptureRowSink(scratch, eff_cols, tabstops)
    segments: list[tuple[str, bool, bool]] = []

    for ln in raw_lines:
        encoded = ln.encode("utf-8", errors="surrogateescape")
        if len(encoded) > max_line_bytes:
            return None
        scratch.screen._history.clear()
        scratch.screen._scrolled_total = 0
        scratch.screen._evicted_total = 0
        row = scratch.screen._rows[0]
        row.reset_blank(eff_cols)
        row.wrapped = False
        scratch._set_cursor(scratch.screen, 0, 0, pending_wrap=False)
        parser.reset()
        mv = memoryview(encoded)
        for off in range(0, len(mv), 4_096):
            parser.feed_to(mv[off : off + 4_096], sink)
            if (
                scratch.screen._evicted_total > 0
                or len(segments) + len(scratch.screen._history) + 1 > n
            ):
                return None
        for h in scratch.screen._history:
            raw_h = "".join(h.cells) if h.cells is not None else h.text
            seg_txt = raw_h if trim_trailing else raw_h.rstrip(" ")
            is_sp = cols > 0 and raw_width_bits(h.attr_at(cols - 1)) == int(
                WidthRole.SPACER_HEAD
            )
            segments.append((seg_txt, True, is_sp))
        cur_row = scratch.screen._rows[0]
        tail_txt = (
            cur_row.slice_text(0, cur_row.text_used)
            if trim_trailing
            else _row_trimmed_text_and_cols(cur_row)[0]
        )
        segments.append((tail_txt, False, False))

    if len(segments) != n:
        return None
    for (seg_txt, _, _), plain in zip(segments, plain_rows, strict=True):
        if seg_txt != plain:
            return None
    return [s[1] for s in segments], [s[2] for s in segments]


def _trim_unwrapped_row(row: _Row) -> None:
    row.trim_trailing_blanks()


def _apply_wide_spacer_heads(
    decoded_rows: list[_Row],
    *,
    cols: int,
    trim_trailing: bool,
    inferred_spacers: list[bool] | None = None,
    indices: collections.abc.Iterable[int] | None = None,
) -> None:
    if cols <= 0 or len(decoded_rows) < 2:
        return
    scan = range(len(decoded_rows) - 1) if indices is None else indices
    for i in scan:
        cur_r = decoded_rows[i]
        nxt_r = decoded_rows[i + 1]
        if cur_r.wrapped and raw_width_bits(nxt_r.attr_at(0)) == int(WidthRole.WIDE):
            if inferred_spacers is not None:
                is_spacer = inferred_spacers[i]
            elif trim_trailing:
                is_spacer = cur_r.text_used == cols - 1
            else:
                _, trimmed_cols = _row_trimmed_text_and_cols(cur_r)
                is_spacer = trimmed_cols == cols - 1
            if is_spacer:
                cur_r.write_spacer_head(
                    cols - 1,
                    cur_r.attr_at(cols - 1),
                    cur_r.ext_at(cols - 1),
                )
                cur_r.wrapped = True


def _decode_capture_rows(
    raw_lines: list[str],
    *,
    cols: int,
    tabstops: tuple[int, ...],
    flagged: bool,
    trim_trailing: bool = False,
    joined_capture: CaptureInput | None,
    stripped_trailing: int = 0,
    profile: Profile,
    policy: Policy,
    limits: Limits,
) -> tuple[list[_Row], list[TmuxLineFlags], bool]:
    flags_list: list[TmuxLineFlags] = []
    bodies: list[str] = []
    if flagged:
        for ln in raw_lines:
            fl, body = parse_flagged_line(ln)
            flags_list.append(fl)
            bodies.append(body)
    else:
        flags_list = [TmuxLineFlags.NONE] * len(raw_lines)
        bodies = list(raw_lines)

    scratch = Terminal(
        rows=1,
        cols=cols,
        scrollback=0,
        profile=profile,
        policy=policy,
        limits=limits,
    )
    scratch.modes.set_mode(Mode.DECAWM, enabled=False)
    # Pre-seed G0='B' and G1='0' on the capture scratch decoder so bare
    # 0x0e / 0x0f (SO / SI) emitted by tmux grid_string_cells_code decode ACS.
    scratch._g_sets = ["B", "0", "B", "B"]
    scratch._gl = 0

    line_parser = Parser(limits=limits)
    sink = _CaptureRowSink(scratch, cols, tabstops)
    decoded_rows: list[_Row] = []
    for body in bodies:
        scratch_row = scratch.screen._rows[0]
        scratch_row.reset_blank(cols)
        scratch._set_cursor(scratch.screen, 0, 0, pending_wrap=False)
        line_parser.reset()
        line_parser.feed_to(body.encode("utf-8", errors="surrogateescape"), sink)
        decoded_rows.append(scratch_row.clone())

    row_wraps: list[bool]
    inferred_spacers: list[bool] | None = None
    exact_wraps = flagged
    if flagged:
        row_wraps = [bool(fl & TmuxLineFlags.WRAPPED) for fl in flags_list]
    elif joined_capture is not None:
        plain_texts = (
            [r.slice_text(0, r.text_used) for r in decoded_rows]
            if trim_trailing
            else [_row_trimmed_text_and_cols(r)[0] for r in decoded_rows]
        )
        inferred = _infer_wraps_from_joined(
            plain_texts,
            joined_capture,
            stripped_trailing=stripped_trailing,
            trim_trailing=trim_trailing,
            cols=cols,
            tabstops=tabstops,
            profile=profile,
            policy=policy,
            limits=limits,
        )
        if inferred is not None:
            exact_wraps = True
            row_wraps, inferred_spacers = inferred
        else:
            row_wraps = [False] * len(decoded_rows)
    else:
        row_wraps = [False] * len(decoded_rows)

    for idx, row_obj in enumerate(decoded_rows):
        row_obj.wrapped = row_wraps[idx]
        if not row_wraps[idx] and not trim_trailing:
            _trim_unwrapped_row(row_obj)

    _apply_wide_spacer_heads(
        decoded_rows,
        cols=cols,
        trim_trailing=trim_trailing,
        inferred_spacers=inferred_spacers,
    )
    return decoded_rows, flags_list, exact_wraps


def _install_rows_into_screen(
    target: Screen,
    decoded_rows: list[_Row],
    flags_list: list[TmuxLineFlags],
    *,
    rows: int,
    cols: int,
) -> list[PromptZone]:
    total = len(decoded_rows)
    hist_count = max(0, total - rows)
    target._history.clear()
    maxlen = target._history.maxlen
    keep_hist = min(hist_count, maxlen) if (maxlen is not None and maxlen > 0) else 0
    dropped_hist = hist_count - keep_hist
    if keep_hist > 0:
        for r_h in decoded_rows[dropped_hist:hist_count]:
            target._history.append(r_h.freeze())
    target._scrolled_total = hist_count
    target._evicted_total = dropped_hist

    view_rows = decoded_rows[hist_count : hist_count + rows]
    while len(view_rows) < rows:
        view_rows.append(_Row(cols, gen_counter=target._gen_counter))
    for r_v in view_rows:
        r_v.bind_counter(target._gen_counter)
    target._rows = view_rows

    zones: list[PromptZone] = []
    for idx in range(dropped_hist, len(flags_list)):
        fl = flags_list[idx]
        if fl & TmuxLineFlags.START_PROMPT:
            zones.append(
                PromptZone(
                    kind=PromptKind.PROMPT_START,
                    abs_row=idx,
                    col=0,
                ),
            )
        if fl & TmuxLineFlags.START_OUTPUT:
            zones.append(
                PromptZone(
                    kind=PromptKind.COMMAND_EXECUTED,
                    abs_row=idx,
                    col=0,
                ),
            )
    return zones


def _populate_screen_from_lines(
    target: Screen,
    raw_lines: list[str],
    *,
    rows: int,
    cols: int,
    tabstops: tuple[int, ...],
    flagged: bool,
    trim_trailing: bool = False,
    joined_capture: CaptureInput | None,
    stripped_trailing: int = 0,
    profile: Profile,
    policy: Policy,
    limits: Limits,
) -> tuple[tuple[TmuxLineFlags, ...], bool, list[PromptZone]]:
    decoded_rows, flags_list, exact_wraps = _decode_capture_rows(
        raw_lines,
        cols=cols,
        tabstops=tabstops,
        flagged=flagged,
        trim_trailing=trim_trailing,
        joined_capture=joined_capture,
        stripped_trailing=stripped_trailing,
        profile=profile,
        policy=policy,
        limits=limits,
    )
    zones = _install_rows_into_screen(
        target,
        decoded_rows,
        flags_list,
        rows=rows,
        cols=cols,
    )
    return tuple(flags_list), exact_wraps, zones


def decode_tmux_capture(
    captured: CaptureInput,
    *,
    metadata: TmuxPaneMetadata | str | None = None,
    rows: int = 24,
    cols: int = 80,
    scrollback: int | None = None,
    flagged: bool = False,
    trim_trailing: bool = False,
    joined_capture: CaptureInput | None = None,
    saved_primary_capture: CaptureInput | None = None,
    saved_primary_flagged: bool | None = None,
    saved_primary_trim_trailing: bool | None = None,
    saved_primary_joined_capture: CaptureInput | None = None,
    post_metadata: TmuxPaneMetadata | str | None = None,
    profile: Profile = TMUX_PROFILE,
    policy: Policy = TMUX_CAPTURE_POLICY,
    limits: Limits = DEFAULT_LIMITS,
) -> TmuxCaptureResult:
    r"""Reconstruct a :class:`~libvt.terminal.Terminal` from ``tmux capture-pane``.

    Handles bare ``SO``/``SI`` (``0x0e``/``0x0f``) ACS box-drawing runs without
    polluting the reconstructed terminal's live ``G1`` charset, decodes
    ``tmux``'s ``CSI 5:3 m`` overline serialization strictly on the capture
    scratch grid, expands ``GRID_FLAG_TAB`` ``\t`` cells against
    ``#{pane_tabs}`` while preserving active SGR background/attributes, carries
    SGR across rows just like ``tmux``'s ``grid_string_cells(&gc)``,
    reconstructs ``pending_wrap`` from ``cursor_x >= pane_width``, and
    optionally populates backgrounded primary history when ``alternate_on`` is
    active.

    Parameters
    ----------
    captured : CaptureInput
        Output of ``tmux capture-pane -S - -E - -e -p -N [-T] [-F]`` as
        ``str``, ``bytes``, or a line sequence (e.g. ``libtmux``
        ``Pane.capture_pane``).
    metadata : TmuxPaneMetadata | str | None
        Parsed :class:`TmuxPaneMetadata` or raw format line emitted by
        ``display-message -p TMUX_METADATA_FORMAT``; when ``None``, *rows*,
        *cols*, and *scrollback* defaults are used.
    rows : int
        Fallback pane height when *metadata* is ``None``.
    cols : int
        Fallback pane width when *metadata* is ``None``.
    scrollback : int | None
        Explicit scrollback line cap; when ``None``, defaults to
        ``metadata.history_limit`` if *metadata* is provided, else ``2000``.
    flagged : bool
        ``True`` when *captured* includes ``capture-pane -F`` line prefixes.
    trim_trailing : bool
        ``True`` when *captured* used ``capture-pane -N -T`` (``tmux >= 3.4``)
        to stop at ``gl->cellused`` without empty-cell padding.
    joined_capture : CaptureInput | None
        Optional ``capture-pane -S - -E - -e -p -J`` output used to infer soft
        wraps on ``tmux < 3.8`` when ``flagged=False``.
    saved_primary_capture : CaptureInput | None
        Optional ``capture-pane -a -q -S - -E - -e -p -N [-T] [-F]`` output
        restoring the backgrounded primary screen and scrollback when
        ``alternate_on`` is ``True``.
    saved_primary_flagged : bool | None
        Whether *saved_primary_capture* carries ``-F`` prefixes (defaults to
        *flagged*).
    saved_primary_trim_trailing : bool | None
        Whether *saved_primary_capture* used ``-T`` (defaults to
        *trim_trailing*).
    saved_primary_joined_capture : CaptureInput | None
        Optional ``capture-pane -a -q -S - -E - -e -p -J`` companion capture
        for *saved_primary_capture* when ``saved_primary_flagged`` is ``False``.
    post_metadata : TmuxPaneMetadata | str | None
        Optional second metadata sample taken immediately after ``capture-pane``
        to verify ``pane_output_generation`` stability.
    profile : Profile
        Compatibility profile for the reconstructed terminal.
    policy : Policy
        Security policy for the reconstructed terminal.
    limits : Limits
        Resource ceilings for the reconstructed terminal.

    Returns
    -------
    TmuxCaptureResult
        Reconstructed terminal, immutable snapshot, completeness report, and
        parsed per-line flags.
    """
    fallback_sb = 2_000 if scrollback is None else scrollback
    if isinstance(metadata, str):
        meta = TmuxPaneMetadata.from_format_line(metadata)
    elif metadata is not None:
        meta = metadata
    else:
        meta = TmuxPaneMetadata(cols=cols, rows=rows, history_limit=fallback_sb)

    post_meta = (
        TmuxPaneMetadata.from_format_line(post_metadata)
        if isinstance(post_metadata, str)
        else post_metadata
    )

    eff_rows = min(_TMUX_MAX_DIM, max(1, meta.rows))
    eff_cols = min(_TMUX_MAX_DIM, max(1, meta.cols))
    raw_sb = (
        scrollback
        if scrollback is not None
        else (meta.history_limit if metadata is not None else 2_000)
    )
    eff_scrollback = max(0, min(_TMUX_MAX_HISTORY, raw_sb))
    tabstops = (
        tuple(sorted({t for t in meta.pane_tabs if 0 < t < eff_cols}))
        if meta.pane_tabs is not None
        else tuple(range(8, eff_cols, 8))
    )

    term = Terminal(
        rows=eff_rows,
        cols=eff_cols,
        scrollback=eff_scrollback,
        profile=profile,
        policy=policy,
        limits=limits,
    )

    term._tabstops_from_list(term.screens.primary, tabstops)
    term._tabstops_from_list(term.screens.alternate, tabstops)

    raw_lines = _split_capture_lines(captured)
    stripped_trailing = 0
    if (
        metadata is not None
        and not flagged
        and meta.history_size > 0
        and len(raw_lines) >= meta.history_size
        and len(raw_lines) < meta.history_size + eff_rows
    ):
        stripped_trailing = meta.history_size + eff_rows - len(raw_lines)
        raw_lines.extend([""] * stripped_trailing)
    if meta.alternate_on:
        all_rows, all_flags, exact_wraps = _decode_capture_rows(
            raw_lines,
            cols=eff_cols,
            tabstops=tabstops,
            flagged=flagged,
            trim_trailing=trim_trailing,
            joined_capture=joined_capture,
            stripped_trailing=stripped_trailing,
            profile=profile,
            policy=policy,
            limits=limits,
        )
        alt_split = max(0, len(all_rows) - eff_rows)
        prim_hist_rows = all_rows[:alt_split]
        prim_hist_flags = all_flags[:alt_split]
        alt_view_rows = all_rows[alt_split:]
        alt_view_flags = all_flags[alt_split:]

        if saved_primary_capture is not None:
            prim_lines = _split_capture_lines(saved_primary_capture)
            prim_flagged = (
                flagged if saved_primary_flagged is None else saved_primary_flagged
            )
            prim_trim = (
                trim_trailing
                if saved_primary_trim_trailing is None
                else saved_primary_trim_trailing
            )
            saved_rows, saved_flags, saved_exact = _decode_capture_rows(
                prim_lines,
                cols=eff_cols,
                tabstops=tabstops,
                flagged=prim_flagged,
                trim_trailing=prim_trim,
                joined_capture=saved_primary_joined_capture,
                profile=profile,
                policy=policy,
                limits=limits,
            )
            exact_wraps = exact_wraps and saved_exact
            if prim_hist_rows:
                had_first_saved = bool(saved_rows)
                if len(saved_rows) < eff_rows:
                    pad_n = eff_rows - len(saved_rows)
                    saved_rows = [
                        *saved_rows,
                        *(
                            _Row(
                                eff_cols, gen_counter=term.screens.primary._gen_counter
                            )
                            for _ in range(pad_n)
                        ),
                    ]
                    saved_flags = [*saved_flags, *([TmuxLineFlags.NONE] * pad_n)]
                elif len(saved_rows) > eff_rows:
                    saved_rows = saved_rows[:eff_rows]
                    saved_flags = saved_flags[:eff_rows]
                full_prim_rows = [*prim_hist_rows, *saved_rows]
                full_prim_flags = [*prim_hist_flags, *saved_flags]
                if had_first_saved:
                    _apply_wide_spacer_heads(
                        full_prim_rows,
                        cols=eff_cols,
                        trim_trailing=prim_trim,
                        indices=(len(prim_hist_rows) - 1,),
                    )
            else:
                full_prim_rows = saved_rows
                full_prim_flags = saved_flags
            prim_zones = _install_rows_into_screen(
                term.screens.primary,
                full_prim_rows,
                full_prim_flags,
                rows=eff_rows,
                cols=eff_cols,
            )
            for z in prim_zones:
                term._prompt_zones.append(z)
        elif prim_hist_rows:
            blank_prim_view = [
                _Row(eff_cols, gen_counter=term.screens.primary._gen_counter)
                for _ in range(eff_rows)
            ]
            prim_zones = _install_rows_into_screen(
                term.screens.primary,
                [*prim_hist_rows, *blank_prim_view],
                [*prim_hist_flags, *([TmuxLineFlags.NONE] * eff_rows)],
                rows=eff_rows,
                cols=eff_cols,
            )
            for z in prim_zones:
                term._prompt_zones.append(z)

        has_saved_xy = (
            meta.alternate_saved_x is not None or meta.alternate_saved_y is not None
        )
        saved_x = 0 if meta.alternate_saved_x is None else meta.alternate_saved_x
        saved_y = 0 if meta.alternate_saved_y is None else meta.alternate_saved_y
        term.screens.primary._cur_row = min(eff_rows - 1, max(0, saved_y))
        term.screens.primary._cur_col = min(eff_cols - 1, max(0, saved_x))
        term.screens.primary._pending_wrap = saved_x >= eff_cols and meta.wrap_flag
        term._saved_primary = term._saved_primary.__class__(
            row=term.screens.primary._cur_row,
            col=term.screens.primary._cur_col,
            pending_wrap=term.screens.primary._pending_wrap,
            style=DEFAULT_STYLE,
            decom=False,
            g_sets=("B", profile.default_g1_charset, "B", "B"),
            gl=0,
            protected=False,
        )
        term.screens.active_is_alt = True
        entered_mode = Mode.ALT_SCREEN if has_saved_xy else Mode.ALT_SCREEN_1047
        term.modes.sync_alt_screen(active_is_alt=True, entered_via=entered_mode)
        _install_rows_into_screen(
            term.screens.alternate,
            alt_view_rows,
            alt_view_flags,
            rows=eff_rows,
            cols=eff_cols,
        )
        line_flags = tuple(alt_view_flags)
    else:
        line_flags, exact_wraps, zones = _populate_screen_from_lines(
            term.screens.primary,
            raw_lines,
            rows=eff_rows,
            cols=eff_cols,
            tabstops=tabstops,
            flagged=flagged,
            trim_trailing=trim_trailing,
            joined_capture=joined_capture,
            stripped_trailing=stripped_trailing,
            profile=profile,
            policy=policy,
            limits=limits,
        )
        for z in zones:
            term._prompt_zones.append(z)

    active = term.screens.active
    sr_top = max(0, min(eff_rows - 1, meta.scroll_region_upper))
    sr_bot_raw = (
        (eff_rows - 1) if meta.scroll_region_lower is None else meta.scroll_region_lower
    )
    sr_bot = max(sr_top, min(eff_rows - 1, sr_bot_raw))
    active.scroll_region = ScrollRegion(
        top=sr_top,
        bottom=sr_bot,
        left=0,
        right=eff_cols - 1,
    )

    term.modes.set_mode(Mode.DECAWM, enabled=meta.wrap_flag)
    term.modes.set_mode(Mode.IRM, enabled=meta.insert_flag)
    term.modes.set_mode(Mode.DECOM, enabled=meta.origin_flag)
    term.modes.set_mode(Mode.DECTCEM, enabled=meta.cursor_visible)
    term.modes.set_mode(Mode.DECCKM, enabled=meta.keypad_cursor_flag)
    term.modes.set_mode(Mode.BRACKETED_PASTE, enabled=meta.bracketed_paste_flag)
    term.modes.set_mode(Mode.MOUSE_VT200, enabled=meta.mouse_standard_flag)
    term.modes.set_mode(Mode.MOUSE_BUTTON_EVENT, enabled=meta.mouse_button_flag)
    term.modes.set_mode(Mode.MOUSE_ANY_EVENT, enabled=meta.mouse_any_flag)
    term.modes.set_mode(Mode.MOUSE_SGR, enabled=meta.mouse_sgr_flag)
    term.modes.set_mode(Mode.FOCUS_EVENT, enabled=meta.focus_events_flag)
    for priv_code in meta.private_modes:
        if priv_code not in (47, 1047, 1049, 2026):
            raw_m = DEC_PRIVATE_MASK | priv_code
            with contextlib.suppress(ValueError):
                term.modes.set_mode(Mode(raw_m), enabled=True)

    active._cur_visible = meta.cursor_visible
    active._cur_row = meta.clamped_cursor_row
    active._cur_col = meta.clamped_cursor_col
    active._pending_wrap = meta.pending_wrap

    term._g_sets = ["B", profile.default_g1_charset, "B", "B"]
    term._gl = 0
    term._single_shift = None
    term._style = DEFAULT_STYLE
    term._hyperlink = None
    term._protected = False
    term._sync_pen_cache()
    if meta.title:
        term._title = meta.title
    if meta.cwd:
        term._cwd = meta.cwd
    if term._prompt_zones or meta.title or meta.cwd:
        term._aux_revision += 1
    term._generation += 1
    term._cached_snapshot = None
    if meta.synchronized:
        term.modes.set_mode(Mode.SYNCHRONIZED_OUTPUT, enabled=True)
        term._sync_generation = 1
        term._sync_hold_pending = True

    unexposed = set(_BASE_UNEXPOSED_FIELDS)
    if not exact_wraps:
        unexposed.add("soft_wraps")
    if meta.alternate_on and saved_primary_capture is None:
        unexposed.add("saved_primary_screen")

    exact_gen = (
        meta.output_generation is not None
        and post_meta is not None
        and post_meta.output_generation == meta.output_generation
        and (
            post_meta.cols,
            post_meta.rows,
            post_meta.alternate_on,
            post_meta.history_size,
        )
        == (meta.cols, meta.rows, meta.alternate_on, meta.history_size)
    )
    completeness = CaptureCompleteness(
        exact_cells=True,
        exact_wraps=exact_wraps,
        exact_generation_bracket=exact_gen,
        unexposed_fields=frozenset(unexposed),
    )
    return TmuxCaptureResult(
        terminal=term,
        snapshot=term.snapshot(),
        completeness=completeness,
        line_flags=line_flags,
    )


@dataclass(frozen=True, slots=True)
class TmuxCaptureCommand:
    """Sans-I/O chained ``tmux`` capture command specification and bundle parser.

    Attributes
    ----------
    argv : tuple[str, ...]
        Chained ``tmux`` subcommand arguments (``display-message`` +
        ``capture-pane`` separated by ``";"``). Pass ``["tmux", *cmd.argv]`` to
        :func:`subprocess.run` or ``server.cmd(cmd.argv[0], *cmd.argv[1:])`` in
        ``libtmux``.
    nonce : str
        Boundary sentinel token bracketing each output section.
    flagged : bool
        ``True`` when ``capture-pane -F`` is included (``tmux >= 3.8``).
    trim_trailing : bool
        ``True`` when ``capture-pane -T`` is included (``tmux >= 3.4``).
    has_joined : bool
        ``True`` when companion ``capture-pane -J`` sections are included
        (``tmux < 3.8``).
    include_saved_primary : bool
        ``True`` when ``capture-pane -a -q`` sections are included.
    """

    argv: tuple[str, ...]
    nonce: str
    flagged: bool
    trim_trailing: bool
    has_joined: bool
    include_saved_primary: bool

    def parse(
        self,
        output: CaptureInput,
        *,
        scrollback: int | None = None,
        profile: Profile = TMUX_PROFILE,
        policy: Policy = TMUX_CAPTURE_POLICY,
        limits: Limits = DEFAULT_LIMITS,
    ) -> TmuxCaptureResult:
        """Split chained ``tmux`` stdout and decode into a :class:`TmuxCaptureResult`.

        Parameters
        ----------
        output : CaptureInput
            Raw stdout ``str``/``bytes`` or line sequence (e.g. ``libtmux``
            ``tmux_cmd.stdout``) produced by executing :attr:`argv`.
        scrollback : int | None
            Explicit scrollback ceiling override.
        profile : Profile
            Compatibility profile for the reconstructed terminal.
        policy : Policy
            Security policy for the reconstructed terminal.
        limits : Limits
            Resource ceilings for the reconstructed terminal.

        Returns
        -------
        TmuxCaptureResult
            Decoded terminal, snapshot, completeness report, and line flags.

        Raises
        ------
        ValueError
            If *output* is missing expected nonce boundary markers.
        """
        lines = _split_capture_lines(output)
        pre_prefix = f"{self.nonce}:PRE:"
        post_prefix = f"{self.nonce}:POST:"
        if not lines or not lines[0].startswith(pre_prefix):
            msg = "missing tmux capture bundle PRE header"
            raise ValueError(msg)

        expected_markers: list[str] = [f"{self.nonce}:CAPTURE"]
        if self.has_joined:
            expected_markers.append(f"{self.nonce}:JOINED")
        if self.include_saved_primary:
            expected_markers.append(f"{self.nonce}:SAVED")
            if self.has_joined:
                expected_markers.append(f"{self.nonce}:SAVED_JOINED")

        marker_indices: dict[str, int] = {}
        search_from = 1
        for marker in expected_markers:
            found = -1
            for idx in range(search_from, len(lines)):
                if lines[idx] == marker:
                    found = idx
                    break
            if found < 0:
                msg = f"missing tmux capture bundle marker: {marker}"
                raise ValueError(msg)
            marker_indices[marker] = found
            search_from = found + 1

        post_idx = -1
        for idx in range(len(lines) - 1, search_from - 1, -1):
            if lines[idx].startswith(post_prefix):
                post_idx = idx
                break
        if post_idx < 0:
            msg = "missing tmux capture bundle POST footer"
            raise ValueError(msg)

        cap_marker = f"{self.nonce}:CAPTURE"
        cap_start = marker_indices[cap_marker]
        pre_raw = "\n".join([lines[0][len(pre_prefix) :], *lines[1:cap_start]])
        post_raw = "\n".join(
            [lines[post_idx][len(post_prefix) :], *lines[post_idx + 1 :]]
        )
        pre_meta = TmuxPaneMetadata.from_format_line(pre_raw)
        post_meta = TmuxPaneMetadata.from_format_line(post_raw)

        ordered_stops = [
            *(marker_indices[m] for m in expected_markers[1:]),
            post_idx,
        ]
        cap_lines = lines[cap_start + 1 : ordered_stops[0]]
        stop_cursor = 0
        joined_lines: list[str] | None = None
        if self.has_joined:
            j_idx = marker_indices[f"{self.nonce}:JOINED"]
            stop_cursor += 1
            joined_lines = lines[j_idx + 1 : ordered_stops[stop_cursor]]

        saved_lines: list[str] | None = None
        saved_joined_lines: list[str] | None = None
        if self.include_saved_primary:
            s_idx = marker_indices[f"{self.nonce}:SAVED"]
            stop_cursor += 1
            raw_saved = lines[s_idx + 1 : ordered_stops[stop_cursor]]
            if self.has_joined:
                sj_idx = marker_indices[f"{self.nonce}:SAVED_JOINED"]
                stop_cursor += 1
                raw_saved_j = lines[sj_idx + 1 : ordered_stops[stop_cursor]]
            else:
                raw_saved_j = None
            if pre_meta.alternate_on and not (self.flagged and raw_saved == [""]):
                saved_lines = raw_saved
                saved_joined_lines = raw_saved_j

        return decode_tmux_capture(
            cap_lines,
            metadata=pre_meta,
            post_metadata=post_meta,
            scrollback=scrollback,
            flagged=self.flagged,
            trim_trailing=self.trim_trailing,
            joined_capture=joined_lines,
            saved_primary_capture=saved_lines,
            saved_primary_flagged=self.flagged,
            saved_primary_trim_trailing=self.trim_trailing,
            saved_primary_joined_capture=saved_joined_lines,
            profile=profile,
            policy=policy,
            limits=limits,
        )


def _escape_tmux_arg(value: str) -> str:
    return f"{value[:-1]}\\;" if value.endswith(";") else value


def build_tmux_capture_command(
    target: str | None = None,
    *,
    tmux_version: tuple[int, int] | str = "3.8a",
    include_saved_primary: bool = True,
    nonce: str | None = None,
) -> TmuxCaptureCommand:
    """Build a single-invocation ``tmux`` command chaining metadata and capture.

    Chains ``display-message`` and ``capture-pane -S - -E - -e -p -N -T -F`` on
    ``tmux >= 3.8`` (falling back to ``-e -p -N -T`` + ``-e -p -J`` on
    ``3.4``-``3.7`` and ``-e -p -N`` + ``-e -p -J`` on ``3.3a``) inside one
    atomic ``tmux`` command-queue batch so pane output cannot drift between
    commands and trailing blank viewport rows are preserved ahead of the
    closing ``POST`` footer.

    Parameters
    ----------
    target : str | None
        Optional ``tmux`` target pane (e.g. ``"%0"``); when ``None``, ``-t`` is
        omitted so ``tmux`` targets the current pane.
    tmux_version : tuple[int, int] | str
        Target ``tmux`` version as ``(major, minor)`` or ``tmux -V`` string
        (e.g. ``"3.8a"``, ``"3.6"``, ``"3.3a"``).
    include_saved_primary : bool
        Include ``capture-pane -a -q`` sections to restore backgrounded primary
        history when ``alternate_on`` is active.
    nonce : str | None
        Explicit ASCII boundary token matching ``[0-9A-Za-z_-]{8,128}``;
        generated via :func:`secrets.token_hex` when ``None``.

    Returns
    -------
    TmuxCaptureCommand
        Command specification with :attr:`~TmuxCaptureCommand.argv` and
        :meth:`~TmuxCaptureCommand.parse`.

    Raises
    ------
    ValueError
        If *target*, *tmux_version*, or *nonce* is invalid.
    """
    if target is not None and (
        not target or any(ch in target for ch in ("\x00", "\r", "\n"))
    ):
        msg = f"invalid tmux target {target[:64]!r}"
        raise ValueError(msg)
    eff_nonce = f"__LIBVT_CAP_{secrets.token_hex(16)}__" if nonce is None else nonce
    if _NONCE_RE.match(eff_nonce) is None:
        msg = (
            f"invalid tmux capture nonce {eff_nonce[:64]!r}; expected 8..128 "
            "[0-9A-Za-z_-] characters"
        )
        raise ValueError(msg)

    ver = _parse_tmux_version(tmux_version)
    flagged = ver >= (3, 8)
    trim_trailing = ver >= (3, 4)
    has_joined = not flagged
    t_args: tuple[str, ...] = (
        ("-t", _escape_tmux_arg(target)) if target is not None else ()
    )

    cap_flags: list[str] = ["-S", "-", "-E", "-", "-e", "-p", "-N"]
    if trim_trailing:
        cap_flags.append("-T")
    if flagged:
        cap_flags.append("-F")

    joined_flags = ["-S", "-", "-E", "-", "-e", "-p", "-J"]

    argv: list[str] = [
        "display-message",
        "-p",
        *t_args,
        f"{eff_nonce}:PRE:{TMUX_METADATA_FORMAT}",
        ";",
        "display-message",
        "-p",
        *t_args,
        f"{eff_nonce}:CAPTURE",
        ";",
        "capture-pane",
        *cap_flags,
        *t_args,
    ]
    if has_joined:
        argv.extend(
            [
                ";",
                "display-message",
                "-p",
                *t_args,
                f"{eff_nonce}:JOINED",
                ";",
                "capture-pane",
                *joined_flags,
                *t_args,
            ]
        )
    if include_saved_primary:
        argv.extend(
            [
                ";",
                "display-message",
                "-p",
                *t_args,
                f"{eff_nonce}:SAVED",
                ";",
                "capture-pane",
                "-a",
                "-q",
                *cap_flags,
                *t_args,
            ]
        )
        if has_joined:
            argv.extend(
                [
                    ";",
                    "display-message",
                    "-p",
                    *t_args,
                    f"{eff_nonce}:SAVED_JOINED",
                    ";",
                    "capture-pane",
                    "-a",
                    "-q",
                    *joined_flags,
                    *t_args,
                ]
            )
    argv.extend(
        [
            ";",
            "display-message",
            "-p",
            *t_args,
            f"{eff_nonce}:POST:{TMUX_METADATA_FORMAT}",
        ]
    )
    return TmuxCaptureCommand(
        argv=tuple(argv),
        nonce=eff_nonce,
        flagged=flagged,
        trim_trailing=trim_trailing,
        has_joined=has_joined,
        include_saved_primary=include_saved_primary,
    )


__all__ = [
    "TMUX_METADATA_FORMAT",
    "CaptureCompleteness",
    "CaptureInput",
    "TmuxCaptureCommand",
    "TmuxCaptureResult",
    "TmuxLineFlags",
    "TmuxPaneMetadata",
    "build_tmux_capture_command",
    "decode_tmux_capture",
    "parse_flagged_line",
    "unescape_tmux_control_output",
]
