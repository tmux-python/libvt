"""Sans-I/O virtual terminal state machine, immutable snapshots, and formatters."""

from __future__ import annotations

import base64
import binascii
import collections
import collections.abc
import enum
import html
import os
import typing as t
import urllib.parse
from dataclasses import dataclass

from libvt._grid import (
    FrozenRow,
    _CellExt,
    _Row,
    make_cell_ext,
    pack_pen_base,
    raw_width_bits,
    unpack_style,
)
from libvt._ucd import (
    ClusterState,
    WidthRole,
    char_width,
    sanitize_terminal_text,
    step_cluster,
)
from libvt.config import (
    DEFAULT_LIMITS,
    DEFAULT_POLICY,
    DEFAULT_PROFILE,
    Limits,
    Policy,
    Profile,
)
from libvt.events import (
    Bell,
    ClipboardWrite,
    ColorSchemeQueried,
    EraseCause,
    Event,
    InBandResize,
    KittyCommand,
    KittyGraphic,
    ProgramStatusChanged,
    PromptKind,
    PromptMark,
    PromptZone,
    Rect,
    RenderHoldChanged,
    ScreenErased,
    SixelData,
    SixelGraphic,
    TitleChanged,
    UnhandledSequence,
    WorkingDirectoryChanged,
)
from libvt.exc import ProtocolError
from libvt.input import InputEvent, KittyKeyboardFlags, encode_input
from libvt.modes import (
    ALT_SCREEN_MODES,
    DEC_PRIVATE_MASK,
    TRANSIENT_SNAPSHOT_MODES,
    Mode,
    ModeState,
)
from libvt.parser import CsiParams, Parser, ParserState, parse_sgr
from libvt.screen import (
    Cell,
    Cursor,
    CursorShape,
    RegionView,
    RowView,
    Screen,
    ScreenSet,
    ScrollRegion,
    format_rows_text,
    slice_grid,
)
from libvt.status import ProgramStatus, ProgramStatusStore
from libvt.style import (
    DEFAULT_STYLE,
    AnsiColor,
    Color,
    Hyperlink,
    IndexedColor,
    RgbColor,
    Style,
    UnderlineStyle,
)

# DEC Special Graphics ('0') mapping for 0x5F..0x7E (VT100 / xterm / tmux tty-acs.c).
_DEC_SPECIAL_GRAPHICS = str.maketrans(
    {
        "_": " ",
        "`": "◆",
        "a": "▒",
        "b": "␉",
        "c": "␌",
        "d": "␍",
        "e": "␊",
        "f": "°",
        "g": "±",
        "h": "␤",
        "i": "␋",
        "j": "┘",
        "k": "┐",
        "l": "┌",
        "m": "└",
        "n": "┼",
        "o": "⎺",
        "p": "⎻",
        "q": "─",
        "r": "⎼",
        "s": "⎽",
        "t": "├",
        "u": "┤",
        "v": "┴",
        "w": "┬",
        "x": "│",
        "y": "≤",
        "z": "≥",
        "{": "π",
        "|": "≠",
        "}": "£",
        "~": "·",
    },
)

_UK_CHARSET = str.maketrans({"#": "£"})

_SAFE_URI_PREFIXES = ("https://", "http://", "mailto:")


class StopOn(enum.IntFlag):
    """Boundary conditions for :meth:`Terminal.feed_until`."""

    NONE = 0
    EOF = 1
    GROUND = 2
    SYNC_START = 4
    SYNC_END = 8
    PROMPT_MARK = 16


@dataclass(frozen=True, slots=True)
class StepResult:
    """Return value from :meth:`Terminal.feed_until`.

    Attributes
    ----------
    offset : int
        Next unconsumed byte index inside the input buffer.
    stopped_on : StopOn
        Flag(s) that triggered termination of the step.
    """

    offset: int
    stopped_on: StopOn


class OutputFormat(enum.StrEnum):
    """Export target formats supported by :class:`Formatter`."""

    PLAIN = "plain"
    VT = "vt"
    HTML = "html"


class FlightRecorder:
    """Bounded raw-byte ring buffer capturing recent VT traffic for diagnostics."""

    __slots__ = ("_buf", "_max_bytes", "_total_bytes")

    def __init__(self, *, max_bytes: int = 4_096) -> None:
        self._max_bytes = max(64, max_bytes)
        self._buf = bytearray()
        self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        """Return total byte count observed since initialization or ``clear()``."""
        return self._total_bytes

    def record(self, data: collections.abc.Buffer) -> None:
        """Append *data*, retaining at most ``max_bytes`` trailing bytes."""
        mv = memoryview(data).cast("B")
        n = len(mv)
        if n == 0:
            return
        self._total_bytes += n
        if n >= self._max_bytes:
            self._buf[:] = mv[n - self._max_bytes :]
            return
        overflow = len(self._buf) + n - self._max_bytes
        if overflow > 0:
            del self._buf[:overflow]
        self._buf.extend(mv)

    def tail(self, max_bytes: int = 256) -> bytes:
        """Return up to *max_bytes* of the most recently recorded input bytes."""
        if max_bytes <= 0 or not self._buf:
            return b""
        return bytes(self._buf[-max_bytes:])

    def clear(self) -> None:
        """Clear recorded bytes and reset ``total_bytes``."""
        self._buf.clear()
        self._total_bytes = 0


@dataclass(frozen=True, slots=True)
class _SavedCursor:
    """Internal ``DECSC`` per-screen saved cursor and charset state.

    Attributes
    ----------
    row : int
        Saved zero-based row.
    col : int
        Saved zero-based column.
    pending_wrap : bool
        Saved pending-wrap state.
    style : Style
        Saved SGR pen.
    decom : bool
        Saved ``DECOM`` origin-mode flag.
    g_sets : tuple[str, str, str, str]
        Saved ``G0``..``G3`` character-set designations.
    gl : int
        Saved active locking-shift slot index (``0..3``).
    protected : bool
        Saved ``DECSCA`` character-protection pen attribute.
    """

    row: int = 0
    col: int = 0
    pending_wrap: bool = False
    style: Style = DEFAULT_STYLE
    decom: bool = False
    g_sets: tuple[str, str, str, str] = ("B", "B", "B", "B")
    gl: int = 0
    protected: bool = False


class Snapshot:
    """Immutable point-in-time capture of terminal screen, history, and metadata."""

    __slots__ = (
        "_cols",
        "_history",
        "_rows",
        "active_is_alt",
        "cursor",
        "cwd",
        "generation",
        "modes",
        "program_status",
        "program_statuses",
        "prompt_zones",
        "title",
    )

    def __init__(
        self,
        *,
        rows: tuple[_Row, ...],
        history: tuple[FrozenRow, ...],
        cols: int,
        cursor: Cursor,
        title: str,
        cwd: str,
        modes: frozenset[Mode],
        active_is_alt: bool,
        prompt_zones: tuple[PromptZone, ...],
        program_status: ProgramStatus | None,
        program_statuses: dict[str, ProgramStatus],
        generation: int,
    ) -> None:
        self._rows = rows
        self._history = history
        self._cols = cols
        self.cursor = cursor
        self.title = title
        self.cwd = cwd
        self.modes = modes
        self.active_is_alt = active_is_alt
        self.prompt_zones = prompt_zones
        self.program_status = program_status
        self.program_statuses = program_statuses
        self.generation = generation

    @property
    def rows(self) -> int:
        """Return snapshot height in rows."""
        return len(self._rows)

    @property
    def cols(self) -> int:
        """Return snapshot width in columns."""
        return self._cols

    @property
    def history(self) -> tuple[RowView, ...]:
        """Return immutable :class:`RowView` sequence over scrollback rows."""
        return tuple(RowView(fr) for fr in self._history)

    @property
    def lines(self) -> tuple[str, ...]:
        """Return right-trimmed text for every visible row in the snapshot."""
        return tuple(r.rstrip_text() for r in self._rows)

    @property
    def text(self) -> str:
        """Return visible snapshot text with trailing blank rows omitted."""
        return format_rows_text(self._rows)

    @t.overload
    def __getitem__(self, key: int) -> RowView: ...

    @t.overload
    def __getitem__(self, key: tuple[int, int]) -> Cell: ...

    @t.overload
    def __getitem__(
        self,
        key: slice | tuple[slice | int, slice | int],
    ) -> RegionView: ...

    def __getitem__(
        self,
        key: int | slice | tuple[slice | int, slice | int],
    ) -> RowView | Cell | RegionView:
        """Index a row, single ``(row, col)`` cell, or 2D region view."""
        return slice_grid(self._rows, self._cols, key)

    def format(
        self,
        output_format: OutputFormat = OutputFormat.PLAIN,
        *,
        policy: Policy = DEFAULT_POLICY,
    ) -> str:
        """Render this snapshot using :class:`Formatter`."""
        return Formatter(output_format, policy=policy).format_snapshot(self)

    def to_dict(self) -> dict[str, t.Any]:
        """Serialize snapshot state into a deterministic JSON-compatible dict."""
        return {
            "rows": self.rows,
            "cols": self.cols,
            "lines": list(self.lines),
            "history_lines": [fr.text for fr in self._history],
            "cursor": {
                "row": self.cursor.row,
                "col": self.cursor.col,
                "visible": self.cursor.visible,
                "shape": self.cursor.shape.value,
                "blinking": self.cursor.blinking,
                "pending_wrap": self.cursor.pending_wrap,
            },
            "title": self.title,
            "cwd": self.cwd,
            "active_is_alt": self.active_is_alt,
            "modes": sorted(m.name for m in self.modes if m.name is not None),
            "program_status": (
                {
                    "state": self.program_status.state.value,
                    "id": self.program_status.id,
                    "progress": self.program_status.progress,
                    "effective_app": self.program_status.effective_app,
                    "title": self.program_status.title,
                    "msg": self.program_status.msg,
                }
                if self.program_status is not None
                else None
            ),
        }


def _color_sgr_tokens(color: Color, *, fg: bool) -> list[str]:
    if color is None:
        return ["39" if fg else "49"]
    if isinstance(color, AnsiColor):
        idx = int(color)
        if idx < 8:
            return [str((30 if fg else 40) + idx)]
        return [str((90 if fg else 100) + (idx - 8))]
    prefix = "38" if fg else "48"
    if isinstance(color, IndexedColor):
        return [f"{prefix};5;{color.index}"]
    return [f"{prefix};2;{color.r};{color.g};{color.b}"]


def _style_to_sgr(style: Style) -> str:
    if style == DEFAULT_STYLE:
        return "\x1b[0m"
    parts: list[str] = ["0"]
    if style.bold:
        parts.append("1")
    if style.dim:
        parts.append("2")
    if style.italic:
        parts.append("3")
    if style.underline == UnderlineStyle.SINGLE:
        parts.append("4")
    elif style.underline != UnderlineStyle.NONE:
        parts.append(f"4:{int(style.underline)}")
    if style.blink:
        parts.append("5")
    if style.reverse:
        parts.append("7")
    if style.hidden:
        parts.append("8")
    if style.strikethrough:
        parts.append("9")
    if style.overline:
        parts.append("53")
    if style.fg is not None:
        parts.extend(_color_sgr_tokens(style.fg, fg=True))
    if style.bg is not None:
        parts.extend(_color_sgr_tokens(style.bg, fg=False))
    if style.underline_color is not None:
        ul = style.underline_color
        if isinstance(ul, AnsiColor):
            parts.append(f"58:5:{int(ul)}")
        elif isinstance(ul, IndexedColor):
            parts.append(f"58:5:{ul.index}")
        else:
            parts.append(f"58:2::{ul.r}:{ul.g}:{ul.b}")
    return f"\x1b[{';'.join(parts)}m"


def _is_safe_uri(uri: str) -> bool:
    low = uri.lstrip().lower()
    return low.startswith(_SAFE_URI_PREFIXES)


class Formatter:
    """Serialize a :class:`Snapshot` or :class:`Terminal` to text, VT, or HTML."""

    __slots__ = ("output_format", "policy")

    def __init__(
        self,
        output_format: OutputFormat = OutputFormat.PLAIN,
        *,
        policy: Policy = DEFAULT_POLICY,
    ) -> None:
        self.output_format = output_format
        self.policy = policy

    def format_snapshot(self, snapshot: Snapshot) -> str:
        """Render *snapshot* in :attr:`output_format`."""
        if self.output_format == OutputFormat.PLAIN:
            return snapshot.text
        if self.output_format == OutputFormat.VT:
            return self._render_vt(snapshot)
        return self._render_html(snapshot)

    def _render_vt(self, snapshot: Snapshot) -> str:
        out: list[str] = []
        export_modes = snapshot.modes - TRANSIENT_SNAPSHOT_MODES
        if not snapshot.active_is_alt:
            export_modes = export_modes - ALT_SCREEN_MODES
        for mode in sorted(export_modes, key=int):
            raw = int(mode)
            if raw & DEC_PRIVATE_MASK:
                code = raw & ~DEC_PRIVATE_MASK
                if mode not in (
                    Mode.DECAWM,
                    Mode.DECTCEM,
                    Mode.GRAPHEME_CLUSTERING,
                ):
                    out.append(f"\x1b[?{code}h")
            else:
                out.append(f"\x1b[{raw}h")
        if Mode.DECTCEM not in export_modes:
            out.append("\x1b[?25l")
        cur_style = DEFAULT_STYLE
        cur_link: Hyperlink | None = None
        for r_idx, row in enumerate(snapshot._rows):
            if r_idx > 0:
                out.append("\r\n" if not snapshot._rows[r_idx - 1].wrapped else "")
            for c in range(row.used):
                word = row.attr_at(c)
                rb = raw_width_bits(word)
                if rb in (int(WidthRole.SPACER_TAIL), int(WidthRole.SPACER_HEAD)):
                    continue
                ext = row.ext_at(c)
                style = unpack_style(word, ext)
                link = ext.hyperlink if ext is not None else None
                if link != cur_link:
                    if link is None:
                        out.append("\x1b]8;;\x1b\\")
                    else:
                        param = f"id={link.id}" if link.id else ""
                        out.append(f"\x1b]8;{param};{link.uri}\x1b\\")
                    cur_link = link
                if style != cur_style:
                    out.append(_style_to_sgr(style))
                    cur_style = style
                out.append(row.chars[c])
        if cur_link is not None:
            out.append("\x1b]8;;\x1b\\")
        if cur_style != DEFAULT_STYLE:
            out.append("\x1b[0m")
        out.append(f"\x1b[{snapshot.cursor.row + 1};{snapshot.cursor.col + 1}H")
        return "".join(out)

    def _render_html(self, snapshot: Snapshot) -> str:
        lines: list[str] = ['<pre class="libvt-screen">']
        allow_unsafe = self.policy.allow_unsafe_uris
        for row in snapshot._rows:
            view = RowView(row)
            chunks: list[str] = []
            for run in view.runs:
                escaped = html.escape(run.text)
                style = run.style
                css: list[str] = []
                if style.bold:
                    css.append("font-weight:bold")
                if style.italic:
                    css.append("font-style:italic")
                if isinstance(style.fg, RgbColor):
                    css.append(
                        f"color:rgb({style.fg.r},{style.fg.g},{style.fg.b})",
                    )
                if isinstance(style.bg, RgbColor):
                    css.append(
                        f"background-color:rgb({style.bg.r},{style.bg.g},{style.bg.b})",
                    )
                span = (
                    f'<span style="{";".join(css)}">{escaped}</span>'
                    if css
                    else escaped
                )
                if run.hyperlink is not None and (
                    allow_unsafe or _is_safe_uri(run.hyperlink.uri)
                ):
                    href = html.escape(run.hyperlink.uri, quote=True)
                    span = f'<a href="{href}">{span}</a>'
                chunks.append(span)
            lines.append("".join(chunks))
        lines.append("</pre>")
        return "\n".join(lines)


class _TerminalSink:
    """Private ``ActionSink`` forwarding parser actions into :class:`Terminal`."""

    __slots__ = ("_term",)

    def __init__(self, term: Terminal) -> None:
        self._term = term

    def on_print(self, text: str) -> None:
        self._term._handle_print(text)

    def on_execute(self, byte: int) -> None:
        self._term._handle_execute(byte)

    def on_esc(self, intermediates: bytes, final: int) -> None:
        self._term._handle_esc(intermediates, final)

    def on_csi(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
    ) -> None:
        self._term._handle_csi(prefix, params, intermediates, final)

    def on_osc(self, code: int, data: bytes, terminator: bytes) -> None:
        self._term._handle_osc(code, data, terminator)

    def on_dcs(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
        data: bytes,
    ) -> None:
        self._term._handle_dcs(prefix, params, intermediates, final, data)

    def on_apc(self, data: bytes) -> None:
        self._term._handle_apc(data)


def _hex_encode(text: str) -> str:
    return text.encode("utf-8").hex()


_TERMINFO_CAPS: dict[str, str] = {
    "TN": "xterm-256color",
    "Co": "256",
    "colors": "256",
    "RGB": "8/8/8",
    "Tc": "",
    "Smulx": "\x1b[4:%p1%dm",
    "Setulc": "\x1b[58:2::%p1%{65536}%/%d:%p1%{256}%/%{255}%&%d:%p1%{255}%&%dm",
    "Sync": "\x1b[?2026%?%p1%{1}%-%tl%eh%;",
}


class Terminal:
    """Sans-I/O virtual terminal emulator over a streaming VT500 parser."""

    def __init__(
        self,
        *,
        rows: int = 24,
        cols: int = 80,
        scrollback: int = 2_000,
        profile: Profile = DEFAULT_PROFILE,
        policy: Policy = DEFAULT_POLICY,
        limits: Limits = DEFAULT_LIMITS,
        on_event: collections.abc.Callable[[Event], None] | None = None,
        on_pty_write: collections.abc.Callable[[bytes], None] | None = None,
        record_events: bool = False,
        recorder: FlightRecorder | None = None,
        c1_controls: bool = False,
    ) -> None:
        self._profile = profile
        self._policy = policy
        self._limits = limits
        self._on_event = on_event
        self._on_pty_write = on_pty_write
        self._record_events = record_events
        self._recorder = recorder
        self._events: collections.deque[Event] = collections.deque(
            maxlen=limits.max_events,
        )
        self._screens = ScreenSet(
            rows=rows,
            cols=cols,
            scrollback=scrollback,
            keyboard_stack_depth=limits.max_keyboard_stack,
        )
        self._modes = ModeState()
        self._parser = Parser(c1_controls=c1_controls, limits=limits)
        self._sink = _TerminalSink(self)
        self._sgr_memo: dict[tuple[Style, CsiParams], Style] = {}
        self._style: Style = DEFAULT_STYLE
        self._protected: bool = False
        self._pen_word: int = 0
        self._hyperlink: Hyperlink | None = None
        self._cell_ext: _CellExt | None = None
        self._g_sets: list[str] = ["B", "B", "B", "B"]
        self._gl: int = 0
        self._single_shift: int | None = None
        self._saved_primary = _SavedCursor()
        self._saved_alt = _SavedCursor()
        self._last_printed: str = ""
        self._last_printed_pos: tuple[int, int] | None = None
        self._cluster_state: ClusterState = ClusterState.NONE
        self._cluster_len: int = 0
        self._title: str = ""
        self._title_stack: list[str] = []
        self._cwd: str = ""
        self._prompt_zones: collections.deque[PromptZone] = collections.deque(
            maxlen=limits.max_prompt_zones,
        )
        self._status_store = ProgramStatusStore(
            max_records=limits.max_status_records,
        )
        self._replies = bytearray()
        self._generation: int = 0
        self._sync_generation: int = 0
        self._sync_hold_pending: bool = False
        self._cached_snapshot: Snapshot | None = None
        self._held_snapshot: Snapshot | None = None
        self._step_event: StopOn = StopOn.NONE
        self._passthrough_depth: int = 0

    @classmethod
    def from_size(
        cls,
        size: os.terminal_size,
        *,
        scrollback: int = 2_000,
        profile: Profile = DEFAULT_PROFILE,
        policy: Policy = DEFAULT_POLICY,
        limits: Limits = DEFAULT_LIMITS,
    ) -> Terminal:
        """Construct a :class:`Terminal` from :class:`os.terminal_size`."""
        return cls(
            rows=size.lines,
            cols=size.columns,
            scrollback=scrollback,
            profile=profile,
            policy=policy,
            limits=limits,
        )

    @property
    def rows(self) -> int:
        """Return active screen height in rows."""
        return self._screens.active.rows

    @property
    def cols(self) -> int:
        """Return active screen width in columns."""
        return self._screens.active.cols

    @property
    def profile(self) -> Profile:
        """Return active :class:`~libvt.config.Profile`."""
        return self._profile

    @property
    def policy(self) -> Policy:
        """Return active :class:`~libvt.config.Policy`."""
        return self._policy

    @property
    def limits(self) -> Limits:
        """Return active :class:`~libvt.config.Limits`."""
        return self._limits

    @property
    def parser_state(self) -> ParserState:
        """Return current :class:`~libvt.parser.ParserState`."""
        return self._parser.state

    @property
    def recorder(self) -> FlightRecorder | None:
        """Return attached :class:`FlightRecorder`, if any."""
        return self._recorder

    @property
    def screen(self) -> Screen:
        """Return the currently active :class:`~libvt.screen.Screen`."""
        return self._screens.active

    @property
    def screens(self) -> ScreenSet:
        """Return the dual primary/alternate :class:`~libvt.screen.ScreenSet`."""
        return self._screens

    @property
    def history(self) -> tuple[RowView, ...]:
        """Return sealed scrollback :class:`~libvt.screen.RowView` lines."""
        return self._screens.primary.history

    @property
    def cursor(self) -> Cursor:
        """Return current zero-based :class:`~libvt.screen.Cursor`."""
        return self._screens.active.cursor

    @property
    def title(self) -> str:
        """Return window title set by ``OSC 0`` / ``OSC 2``."""
        return self._title

    @property
    def cwd(self) -> str:
        """Return working directory path reported by ``OSC 7`` / ``OSC 9;9``."""
        return self._cwd

    @property
    def modes(self) -> ModeState:
        """Return current :class:`~libvt.modes.ModeState`."""
        return self._modes

    @property
    def kitty_keyboard_flags(self) -> KittyKeyboardFlags:
        """Return active screen's :class:`~libvt.input.KittyKeyboardFlags`."""
        return self._screens.active.keyboard_stack.flags

    @property
    def synchronized(self) -> bool:
        """Return ``True`` while DEC mode ``?2026`` synchronized output is active."""
        return self._modes.is_set(Mode.SYNCHRONIZED_OUTPUT)

    @property
    def sync_generation(self) -> int:
        """Return monotonic ``?2026`` synchronized-update batch counter."""
        return self._sync_generation

    @property
    def prompt_zones(self) -> tuple[PromptZone, ...]:
        """Return recorded ``OSC 133`` prompt zones adjusted to current viewport."""
        scrolled = self._screens.primary.scrolled_total
        return tuple(
            PromptZone(
                kind=z.kind,
                row=z.abs_row - scrolled,
                col=z.col,
                exit_code=z.exit_code,
                abs_row=z.abs_row,
            )
            for z in self._prompt_zones
        )

    @property
    def program_status(self) -> ProgramStatus | None:
        """Return highest-urgency active ``OSC 7501`` / ``OSC 9;4`` status."""
        return self._status_store.most_urgent

    @property
    def program_statuses(self) -> dict[str, ProgramStatus]:
        """Return all active ``OSC 7501`` task records keyed by ``id``."""
        return self._status_store.records

    @property
    def lines(self) -> tuple[str, ...]:
        """Return right-trimmed visible lines of the active screen."""
        return self._screens.active.lines

    @property
    def text(self) -> str:
        """Return visible active-screen text with trailing blank rows omitted."""
        return self._screens.active.text

    @property
    def committed(self) -> Snapshot:
        """Return tear-free committed :class:`Snapshot` respecting ``?2026`` hold."""
        if self._sync_hold_pending:
            self._ensure_sync_snapshot()
        if self._held_snapshot is not None:
            return self._held_snapshot
        return self.snapshot()

    def _ensure_sync_snapshot(self) -> None:
        if self._sync_hold_pending and self._held_snapshot is None:
            self._held_snapshot = self.snapshot()
            self._sync_hold_pending = False

    def snapshot(self) -> Snapshot:
        """Capture an immutable :class:`Snapshot` of live terminal state."""
        if (
            self._cached_snapshot is not None
            and self._cached_snapshot.generation == self._generation
        ):
            return self._cached_snapshot
        scr = self._screens.active
        snap = Snapshot(
            rows=tuple(r.clone() for r in scr._rows),
            history=tuple(self._screens.primary._history),
            cols=scr.cols,
            cursor=scr.cursor,
            title=self._title,
            cwd=self._cwd,
            modes=self._modes.enabled_modes(),
            active_is_alt=self._screens.active_is_alt,
            prompt_zones=self.prompt_zones,
            program_status=self._status_store.most_urgent,
            program_statuses=self._status_store.records,
            generation=self._generation,
        )
        self._cached_snapshot = snap
        return snap

    @t.overload
    def __getitem__(self, key: int) -> RowView: ...

    @t.overload
    def __getitem__(self, key: tuple[int, int]) -> Cell: ...

    @t.overload
    def __getitem__(
        self,
        key: slice | tuple[slice | int, slice | int],
    ) -> RegionView: ...

    def __getitem__(
        self,
        key: int | slice | tuple[slice | int, slice | int],
    ) -> RowView | Cell | RegionView:
        """Index a row, single ``(row, col)`` cell, or 2D region view."""
        return self._screens.active[key]

    def _emit_event(self, event: Event) -> None:
        if self._record_events:
            self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)

    def _unhandled(self, kind: str, summary: str) -> None:
        if self._policy.strict:
            msg = f"unhandled {kind} sequence: {summary}"
            raise ProtocolError(msg)
        if self._record_events or self._on_event is not None:
            self._emit_event(UnhandledSequence(kind=kind, summary=summary))

    def drain_events(self) -> tuple[Event, ...]:
        """Return and clear buffered :data:`~libvt.events.Event` records."""
        if not self._events:
            return ()
        out = tuple(self._events)
        self._events.clear()
        return out

    def drain_replies(self) -> bytes:
        """Return and clear buffered sans-I/O query reply bytes."""
        if not self._replies:
            return b""
        out = bytes(self._replies)
        self._replies.clear()
        return out

    def encode(self, event: InputEvent, /) -> bytes:
        """Encode *event* into outbound PTY bytes using active terminal modes."""
        return encode_input(
            event,
            modes=self._modes,
            kitty_flags=self.kitty_keyboard_flags,
        )

    def expire_synchronized_output(
        self,
        sync_generation: int | None = None,
    ) -> bool:
        """Release a stalled ``?2026`` hold when *sync_generation* matches.

        Returns ``True`` if an active ``?2026`` batch was expired, or ``False``
        when ``?2026`` was already inactive or belonged to a newer batch.
        """
        if not self.synchronized:
            return False
        if sync_generation is not None and sync_generation != self._sync_generation:
            return False
        self._modes.set_mode(Mode.SYNCHRONIZED_OUTPUT, enabled=False)
        self._sync_hold_pending = False
        self._held_snapshot = None
        self._generation += 1
        self._emit_event(
            RenderHoldChanged(
                active=False,
                sync_generation=self._sync_generation,
                expired=True,
            ),
        )
        return True

    def notify_process_exit(self) -> None:
        """Prune transient ``working`` / ``blocked`` ``OSC 7501`` status records."""
        if self._status_store.on_process_exit():
            self._generation += 1
            self._emit_event(
                ProgramStatusChanged(status=self._status_store.most_urgent),
            )

    def _emit_reply(self, payload: bytes) -> None:
        if self._on_pty_write is not None:
            self._on_pty_write(payload)
            return
        if len(self._replies) + len(payload) <= self._limits.max_reply_bytes:
            self._replies.extend(payload)

    def _clear_cluster_anchor(self) -> None:
        self._last_printed_pos = None
        self._cluster_state = ClusterState.NONE
        self._cluster_len = 0

    def _sync_pen_cache(self) -> None:
        self._pen_word = pack_pen_base(self._style)
        self._cell_ext = make_cell_ext(
            self._style,
            self._hyperlink,
            protected=self._protected,
        )

    def _bce_pair(self) -> tuple[int, _CellExt | None]:
        if self._style.bg is None and self._hyperlink is None:
            return 0, None
        bce_style = Style(bg=self._style.bg)
        return pack_pen_base(bce_style), make_cell_ext(bce_style, self._hyperlink)

    def _notify_erase(self, rect: Rect, cause: EraseCause) -> None:
        if (
            (self._record_events or self._on_event is not None)
            and rect.rows > 0
            and rect.cols > 0
        ):
            self._emit_event(
                ScreenErased(
                    rect=rect,
                    cause=cause,
                    active_is_alt=self._screens.active_is_alt,
                ),
            )

    def resize(self, *, rows: int, cols: int, reflow: bool = True) -> None:
        """Resize terminal screens to ``(rows, cols)``."""
        self._before_mutate()
        self._clear_cluster_anchor()
        self._screens.resize(rows=rows, cols=cols, reflow=reflow)
        self._generation += 1
        if self._modes.is_set(Mode.IN_BAND_RESIZE):
            self._emit_reply(f"\x1b[48;{rows};{cols};0;0t".encode("ascii"))
            self._emit_event(InBandResize(rows=rows, cols=cols))

    def reset(self, *, soft: bool = False) -> None:
        """Reset terminal state (``DECSTR`` when *soft* is ``True``, else ``RIS``)."""
        self._before_mutate()
        self._style = DEFAULT_STYLE
        self._protected = False
        self._hyperlink = None
        self._sync_pen_cache()
        self._last_printed = ""
        self._clear_cluster_anchor()
        self._g_sets = ["B", "B", "B", "B"]
        self._gl = 0
        self._single_shift = None
        was_sync = self.synchronized
        was_alt = self._screens.active_is_alt
        self._modes = ModeState()
        self._sync_hold_pending = False
        self._held_snapshot = None
        if was_sync:
            self._emit_event(
                RenderHoldChanged(
                    active=False,
                    sync_generation=self._sync_generation,
                ),
            )
        if soft:
            if was_alt:
                self._modes.sync_alt_screen(active_is_alt=True)
            scr = self._screens.active
            scr.scroll_region = ScrollRegion(
                top=0,
                bottom=scr.rows - 1,
                left=0,
                right=scr.cols - 1,
            )
            scr._cur_visible = True
            scr._pending_wrap = False
            scr.keyboard_stack.reset()
        else:
            self._parser.reset()
            self._screens.active_is_alt = False
            self._screens.primary.reset()
            self._screens.alternate.reset()
            self._saved_primary = _SavedCursor()
            self._saved_alt = _SavedCursor()
            self._title = ""
            self._title_stack.clear()
            self._cwd = ""
            self._prompt_zones.clear()
            self._status_store.reset()
            self._notify_erase(
                Rect(0, 0, self.rows, self.cols),
                EraseCause.RESET,
            )
        self._generation += 1

    def _before_mutate(self) -> None:
        if self._sync_hold_pending:
            self._ensure_sync_snapshot()

    def feed_str(self, text: str, /) -> None:
        """Encode *text* as UTF-8 and feed into the terminal."""
        self.feed(text.encode("utf-8"))

    def feed(self, data: collections.abc.Buffer, /) -> None:
        """Consume all bytes in *data*, updating terminal state in place."""
        mv = memoryview(data)
        if not mv:
            return
        if self._recorder is not None:
            self._recorder.record(mv)
        self._parser.feed_to(mv, self._sink)
        self._generation += 1

    def feed_until(
        self,
        data: collections.abc.Buffer,
        *,
        offset: int = 0,
        stop: StopOn = StopOn.EOF | StopOn.SYNC_END,
    ) -> StepResult:
        """Feed bytes starting at *offset* until a *stop* boundary occurs."""
        mv = memoryview(data).cast("B")
        n = len(mv)
        if offset < 0 or offset > n:
            msg = f"offset {offset} out of range for buffer of length {n}"
            raise ValueError(msg)
        idx = offset
        while idx < n:
            self._step_event = StopOn.NONE
            state = self._parser.state

            # Bulk-advance GROUND printable ASCII when StopOn.GROUND is not requested.
            if (
                state == ParserState.GROUND
                and not self._parser.utf8_pending
                and not (stop & StopOn.GROUND)
            ):
                end = idx
                while end < n and 0x20 <= mv[end] < 0x7F:
                    end += 1
                if end > idx:
                    span = mv[idx:end]
                    if self._recorder is not None:
                        self._recorder.record(span)
                    self._parser.feed_to(span, self._sink)
                    idx = end
                    self._generation += 1
                    continue

            # Bulk-advance interior payload bytes inside OSC / DCS / APC strings!
            if state in (
                ParserState.OSC_STRING,
                ParserState.DCS_PASSTHROUGH,
                ParserState.SOS_PM_APC_STRING,
            ):
                end = idx
                while end < n and mv[end] not in (0x07, 0x18, 0x1A, 0x1B, 0x9C):
                    end += 1
                if end > idx:
                    span = mv[idx:end]
                    if self._recorder is not None:
                        self._recorder.record(span)
                    self._parser.feed_to(span, self._sink)
                    idx = end
                    self._generation += 1
                    continue

            one = mv[idx : idx + 1]
            if self._recorder is not None:
                self._recorder.record(one)
            self._parser.feed_to(one, self._sink)
            idx += 1
            self._generation += 1
            matched = self._step_event & stop
            if (
                (stop & StopOn.GROUND)
                and self._parser.state == ParserState.GROUND
                and not self._parser.utf8_pending
            ):
                matched |= StopOn.GROUND
            if matched:
                return StepResult(offset=idx, stopped_on=matched)
        return StepResult(
            offset=n,
            stopped_on=StopOn.EOF,
        )

    # --- Internal sink handlers ---

    def _effective_margins(self, scr: Screen) -> tuple[int, int, int, int]:
        sr = scr.scroll_region
        if self._modes.is_set(Mode.DECLRMM):
            return sr.top, sr.bottom, sr.left, sr.right
        return sr.top, sr.bottom, 0, scr.cols - 1

    def _set_cursor(
        self,
        scr: Screen,
        row: int,
        col: int,
        *,
        pending_wrap: bool = False,
        clear_cluster: bool = True,
    ) -> None:
        scr._cur_row = max(0, min(scr.rows - 1, row))
        scr._cur_col = max(0, min(scr.cols - 1, col))
        scr._pending_wrap = pending_wrap
        if clear_cluster:
            self._clear_cluster_anchor()

    def _linefeed(self, scr: Screen, *, carriage_return: bool = False) -> None:
        self._before_mutate()
        top, bottom, left, right = self._effective_margins(scr)
        col = left if carriage_return else scr._cur_col
        if scr._cur_row == bottom and left <= scr._cur_col <= right:
            bce_word, bce_ext = self._bce_pair()
            scr.scroll_up(
                1,
                top=top,
                bottom=bottom,
                left=left,
                right=right,
                save_history=not self._screens.active_is_alt,
                bce_word=bce_word,
                bce_ext=bce_ext,
            )
            self._notify_erase(
                Rect(bottom, left, 1, right - left + 1),
                EraseCause.SCROLL,
            )
            self._set_cursor(scr, bottom, col, pending_wrap=False)
        elif scr._cur_row < scr.rows - 1:
            self._set_cursor(scr, scr._cur_row + 1, col, pending_wrap=False)
        else:
            self._set_cursor(scr, scr._cur_row, col, pending_wrap=False)

    def _map_charset(self, text: str) -> str:
        if self._single_shift is not None:
            ch0_cs = self._g_sets[self._single_shift]
            self._single_shift = None
            first = (
                text[0].translate(_DEC_SPECIAL_GRAPHICS)
                if ch0_cs == "0"
                else (text[0].translate(_UK_CHARSET) if ch0_cs == "A" else text[0])
            )
            if len(text) == 1:
                return first
            return first + self._map_charset(text[1:])

        active_cs = self._g_sets[self._gl]
        if active_cs == "B":
            return text
        if active_cs == "0":
            return text.translate(_DEC_SPECIAL_GRAPHICS)
        if active_cs == "A":
            return text.translate(_UK_CHARSET)
        return text

    def _handle_print(self, text: str) -> None:
        self._before_mutate()
        if self._single_shift is not None or self._g_sets[self._gl] != "B":
            text = self._map_charset(text)

        scr = self._screens.active
        autowrap = self._modes.is_set(Mode.DECAWM)
        insert_mode = self._modes.is_set(Mode.IRM)
        _, _, left, right = self._effective_margins(scr)

        if text.isascii() and not insert_mode:
            pos = 0
            total = len(text)
            while pos < total:
                c_col = scr._cur_col
                max_col = right if c_col <= right else scr.cols - 1
                min_col = left if c_col >= left else 0
                if scr._pending_wrap and autowrap:
                    scr._rows[scr._cur_row].wrapped = True
                    self._linefeed(scr)
                    self._set_cursor(scr, scr._cur_row, min_col, pending_wrap=False)
                    c_col = scr._cur_col
                avail = max_col - c_col + 1
                seg = text[pos : pos + avail]
                seg_len = len(seg)
                scr._rows[scr._cur_row].write_ascii_slice(
                    c_col,
                    seg,
                    self._pen_word,
                    self._cell_ext,
                )
                pos += seg_len
                end_col = c_col + seg_len - 1
                if c_col + seg_len > max_col:
                    self._set_cursor(
                        scr,
                        scr._cur_row,
                        max_col,
                        pending_wrap=autowrap,
                        clear_cluster=False,
                    )
                else:
                    self._set_cursor(
                        scr,
                        scr._cur_row,
                        c_col + seg_len,
                        pending_wrap=False,
                        clear_cluster=False,
                    )
                self._last_printed = seg[-1]
                self._last_printed_pos = (scr._cur_row, end_col)
                self._cluster_state = ClusterState.BASE
                self._cluster_len = 1
            return

        mode_2027 = self._modes.is_set(Mode.GRAPHEME_CLUSTERING)
        max_cluster = self._limits.max_grapheme_codepoints

        for ch in text:
            cp = ord(ch)
            if self._last_printed_pos is not None:
                extends, next_state, w_override = step_cluster(
                    self._cluster_state,
                    cp,
                    mode_2027=mode_2027,
                )
                if extends:
                    self._cluster_state = next_state
                    if self._cluster_len >= max_cluster:
                        continue
                    pr, pc = self._last_printed_pos
                    if 0 <= pr < scr.rows and 0 <= pc < scr.cols:
                        row_obj = scr._rows[pr]
                        cur_role = raw_width_bits(row_obj.attr_at(pc))
                        # Right-margin VS16 / Regional Indicator widening wrap!
                        eff_right = right if pc <= right else scr.cols - 1
                        eff_left = left if pc >= left else 0
                        if (
                            w_override == 2
                            and cur_role == int(WidthRole.NARROW)
                            and pc == eff_right
                            and autowrap
                            and eff_right - eff_left >= 1
                        ):
                            combined = row_obj.chars[pc] + ch
                            bce_w, bce_e = self._bce_pair()
                            row_obj.write_spacer_head(pc, bce_w, bce_e)
                            row_obj.wrapped = True
                            self._linefeed(scr)
                            nr = scr._cur_row
                            scr._rows[nr].write_wide(
                                eff_left,
                                combined,
                                self._pen_word,
                                self._cell_ext,
                            )
                            self._cluster_len += 1
                            self._last_printed = combined
                            self._last_printed_pos = (nr, eff_left)
                            if eff_left + 1 >= eff_right:
                                self._set_cursor(
                                    scr,
                                    nr,
                                    eff_right,
                                    pending_wrap=autowrap,
                                    clear_cluster=False,
                                )
                            else:
                                self._set_cursor(
                                    scr,
                                    nr,
                                    eff_left + 2,
                                    pending_wrap=False,
                                    clear_cluster=False,
                                )
                            continue

                        row_obj.append_grapheme(
                            pc,
                            ch,
                            width_override=w_override,
                            pen_word=self._pen_word,
                            cell_ext=self._cell_ext,
                        )
                        self._cluster_len += 1
                        self._last_printed = row_obj.chars[pc]
                        if (
                            w_override == 2
                            and cur_role == int(WidthRole.NARROW)
                            and scr._cur_row == pr
                            and scr._cur_col == pc + 1
                        ):
                            if pc + 1 >= eff_right:
                                self._set_cursor(
                                    scr,
                                    pr,
                                    eff_right,
                                    pending_wrap=autowrap,
                                    clear_cluster=False,
                                )
                            else:
                                self._set_cursor(
                                    scr,
                                    pr,
                                    pc + 2,
                                    pending_wrap=False,
                                    clear_cluster=False,
                                )
                        elif (
                            w_override == 1
                            and cur_role == int(WidthRole.WIDE)
                            and scr._cur_row == pr
                        ):
                            self._set_cursor(
                                scr,
                                pr,
                                min(eff_right, pc + 1),
                                pending_wrap=False,
                                clear_cluster=False,
                            )
                        continue
            else:
                _, next_state, _ = step_cluster(
                    ClusterState.NONE,
                    cp,
                    mode_2027=mode_2027,
                )

            width = char_width(ch)
            if width == 0:
                continue

            c_row = scr._cur_row
            c_col = scr._cur_col
            max_col = right if c_col <= right else scr.cols - 1
            min_col = left if c_col >= left else 0

            if scr._pending_wrap and autowrap:
                scr._rows[c_row].wrapped = True
                self._linefeed(scr)
                self._set_cursor(scr, scr._cur_row, min_col, pending_wrap=False)
                c_row = scr._cur_row
                c_col = scr._cur_col

            if width == 2 and c_col == max_col:
                if autowrap and max_col - min_col >= 1:
                    bce_w, bce_e = self._bce_pair()
                    scr._rows[c_row].write_spacer_head(c_col, bce_w, bce_e)
                    scr._rows[c_row].wrapped = True
                    self._linefeed(scr)
                    self._set_cursor(scr, scr._cur_row, min_col, pending_wrap=False)
                    c_row = scr._cur_row
                    c_col = scr._cur_col
                else:
                    ch = "\ufffd"
                    width = 1

            row_obj = scr._rows[c_row]
            if insert_mode:
                bce_w, bce_e = self._bce_pair()
                row_obj.insert_blanks(
                    c_col,
                    width,
                    left=min_col,
                    right=max_col,
                    bce_word=bce_w,
                    bce_ext=bce_e,
                )

            if width == 2 and c_col + 1 <= max_col:
                row_obj.write_wide(
                    c_col,
                    ch,
                    self._pen_word,
                    self._cell_ext,
                )
                placed_col = c_col
                if c_col + 1 >= max_col:
                    self._set_cursor(
                        scr,
                        c_row,
                        max_col,
                        pending_wrap=autowrap,
                        clear_cluster=False,
                    )
                else:
                    self._set_cursor(
                        scr,
                        c_row,
                        c_col + 2,
                        pending_wrap=False,
                        clear_cluster=False,
                    )
            else:
                row_obj.write_narrow(
                    c_col,
                    ch,
                    self._pen_word,
                    self._cell_ext,
                )
                placed_col = c_col
                if c_col >= max_col:
                    self._set_cursor(
                        scr,
                        c_row,
                        max_col,
                        pending_wrap=autowrap,
                        clear_cluster=False,
                    )
                else:
                    self._set_cursor(
                        scr,
                        c_row,
                        c_col + 1,
                        pending_wrap=False,
                        clear_cluster=False,
                    )

            self._last_printed = ch
            self._last_printed_pos = (c_row, placed_col)
            self._cluster_state = next_state
            self._cluster_len = 1

    def _cursor_left(self, scr: Screen, count: int, *, is_backspace: bool) -> None:
        _, top, left, right = (
            scr.scroll_region.bottom,
            scr.scroll_region.top,
            *self._effective_margins(scr)[2:],
        )
        rev_wrap = self._modes.is_set(Mode.DECAWM) and (
            self._modes.is_set(Mode.REVERSE_WRAP)
            or self._profile.pending_wrap_phantom_col
        )
        n = max(1, count)
        if scr._pending_wrap and rev_wrap:
            scr._pending_wrap = False
            self._clear_cluster_anchor()
            n -= 1
            if n == 0:
                return

        floor = left if scr._cur_col >= left else 0
        if (
            is_backspace
            and rev_wrap
            and scr._cur_col == floor
            and scr._cur_row > top
            and scr._rows[scr._cur_row - 1].wrapped
        ):
            self._set_cursor(scr, scr._cur_row - 1, right, pending_wrap=False)
            return

        self._set_cursor(
            scr,
            scr._cur_row,
            max(floor, scr._cur_col - n),
            pending_wrap=False,
        )

    def _handle_execute(self, byte: int) -> None:
        scr = self._screens.active
        _, _, left, right = self._effective_margins(scr)
        if byte == 0x07:  # BEL
            self._emit_event(Bell())
        elif byte == 0x08:  # BS
            self._cursor_left(scr, 1, is_backspace=True)
        elif byte == 0x09:  # HT
            ceil = right if scr._cur_col <= right else scr.cols - 1
            nxt = next(
                (
                    t_col
                    for t_col in scr._tabstops_sorted
                    if scr._cur_col < t_col <= ceil
                ),
                ceil,
            )
            self._set_cursor(scr, scr._cur_row, nxt, pending_wrap=False)
        elif byte in (0x0A, 0x0B, 0x0C):  # LF, VT, FF
            self._linefeed(scr, carriage_return=self._modes.is_set(Mode.LNM))
        elif byte == 0x0D:  # CR
            floor = left if scr._cur_col >= left else 0
            self._set_cursor(scr, scr._cur_row, floor, pending_wrap=False)
        elif byte == 0x0E:  # SO -> G1
            self._gl = 1
        elif byte == 0x0F:  # SI -> G0
            self._gl = 0

    def _save_cursor(self) -> None:
        scr = self._screens.active
        saved = _SavedCursor(
            row=scr._cur_row,
            col=scr._cur_col,
            pending_wrap=scr._pending_wrap,
            style=self._style,
            decom=self._modes.is_set(Mode.DECOM),
            g_sets=(
                self._g_sets[0],
                self._g_sets[1],
                self._g_sets[2],
                self._g_sets[3],
            ),
            gl=self._gl,
            protected=self._protected,
        )
        if self._screens.active_is_alt:
            self._saved_alt = saved
        else:
            self._saved_primary = saved

    def _restore_cursor(self) -> None:
        scr = self._screens.active
        saved = self._saved_alt if self._screens.active_is_alt else self._saved_primary
        self._style = saved.style
        self._protected = saved.protected
        self._sync_pen_cache()
        self._g_sets = list(saved.g_sets)
        self._gl = saved.gl
        self._single_shift = None
        self._modes.set_mode(Mode.DECOM, enabled=saved.decom)
        self._set_cursor(
            scr,
            saved.row,
            saved.col,
            pending_wrap=saved.pending_wrap,
        )

    def _handle_esc(self, intermediates: bytes, final: int) -> None:
        scr = self._screens.active
        if not intermediates:
            if final == 0x37:  # ESC 7 DECSC
                self._save_cursor()
            elif final == 0x38:  # ESC 8 DECRC
                self._restore_cursor()
            elif final == 0x44:  # ESC D IND
                self._linefeed(scr)
            elif final == 0x45:  # ESC E NEL
                self._linefeed(scr, carriage_return=True)
            elif final == 0x48:  # ESC H HTS
                scr.add_tabstop(scr._cur_col)
            elif final == 0x4D:  # ESC M RI
                self._before_mutate()
                top, bottom, left, right = self._effective_margins(scr)
                if scr._cur_row == top and left <= scr._cur_col <= right:
                    bce_w, bce_e = self._bce_pair()
                    scr.scroll_down(
                        1,
                        top=top,
                        bottom=bottom,
                        left=left,
                        right=right,
                        bce_word=bce_w,
                        bce_ext=bce_e,
                    )
                    self._notify_erase(
                        Rect(top, left, 1, right - left + 1),
                        EraseCause.SCROLL,
                    )
                    self._clear_cluster_anchor()
                elif scr._cur_row > 0:
                    self._set_cursor(
                        scr, scr._cur_row - 1, scr._cur_col, pending_wrap=False
                    )
            elif final == 0x4E:  # ESC N SS2 -> G2
                self._single_shift = 2
            elif final == 0x4F:  # ESC O SS3 -> G3
                self._single_shift = 3
            elif final == 0x6E:  # ESC n LS2 -> G2
                self._gl = 2
            elif final == 0x6F:  # ESC o LS3 -> G3
                self._gl = 3
            elif final == 0x63:  # ESC c RIS
                self.reset(soft=False)
            elif final not in (0x3D, 0x3E):  # ignore DECKPAM / DECKPNM
                self._unhandled("esc", f"ESC {chr(final)}")
        elif intermediates in (b"(", b")", b"*", b"+"):
            slot = {b"(": 0, b")": 1, b"*": 2, b"+": 3}[intermediates]
            desig = chr(final)
            if desig in ("0", "A", "B"):
                self._g_sets[slot] = desig
            else:
                self._g_sets[slot] = "B"
        elif intermediates == b"#" and final == 0x38:  # DECALN
            self._before_mutate()
            for r in scr._rows:
                r.write_ascii_slice(0, "E" * scr.cols, 0, None)
            self._set_cursor(scr, 0, 0, pending_wrap=False)
        else:
            inter_s = intermediates.decode("ascii", errors="replace")
            self._unhandled("esc", f"ESC {inter_s}{chr(final)}")

    def _apply_mode(self, code: int, *, private: bool, enabled: bool) -> None:
        raw = (DEC_PRIVATE_MASK | code) if private else code
        try:
            mode = Mode(raw)
        except ValueError:
            return
        was_set = self._modes.is_set(mode)

        if mode in ALT_SCREEN_MODES:
            self._before_mutate()
            self._clear_cluster_anchor()
            if enabled and not self._screens.active_is_alt:
                if mode == Mode.ALT_SCREEN:
                    self._save_cursor()
                self._screens.alternate.reset()
                self._screens.active_is_alt = True
                self._modes.sync_alt_screen(active_is_alt=True, entered_via=mode)
            elif not enabled and self._screens.active_is_alt:
                if mode in (Mode.ALT_SCREEN_1047, Mode.ALT_SCREEN):
                    self._screens.alternate.reset()
                self._screens.active_is_alt = False
                self._modes.sync_alt_screen(active_is_alt=False)
                if mode == Mode.ALT_SCREEN:
                    self._restore_cursor()
            elif not enabled:
                self._modes.sync_alt_screen(active_is_alt=False)
            return

        self._modes.set_mode(mode, enabled=enabled)
        scr = self._screens.active

        if mode == Mode.DECTCEM:
            scr._cur_visible = enabled
        elif mode == Mode.DECOM:
            top, _, left, _ = self._effective_margins(scr)
            self._set_cursor(
                scr,
                top if enabled else 0,
                left if enabled else 0,
                pending_wrap=False,
            )
        elif mode == Mode.DECLRMM and not enabled:
            sr = scr.scroll_region
            scr.scroll_region = ScrollRegion(
                top=sr.top,
                bottom=sr.bottom,
                left=0,
                right=scr.cols - 1,
            )
        elif mode == Mode.DECSC_1048:
            if enabled:
                self._save_cursor()
            else:
                self._restore_cursor()
        elif mode == Mode.SYNCHRONIZED_OUTPUT and enabled != was_set:
            if enabled:
                self._sync_generation += 1
                self._sync_hold_pending = True
                self._held_snapshot = None
                self._step_event |= StopOn.SYNC_START
            else:
                self._sync_hold_pending = False
                self._held_snapshot = None
                self._step_event |= StopOn.SYNC_END
            self._emit_event(
                RenderHoldChanged(
                    active=enabled,
                    sync_generation=self._sync_generation,
                ),
            )

    def _erase_display(
        self,
        scr: Screen,
        mode: int,
        *,
        selective: bool,
    ) -> None:
        self._before_mutate()
        bce_w, bce_e = self._bce_pair()
        cause = EraseCause.DECSED if selective else EraseCause.ED
        c_row = scr._cur_row
        c_col = scr._cur_col
        if mode == 0:
            scr._rows[c_row].clear_range(
                c_col,
                scr.cols,
                bce_w,
                bce_e,
                selective=selective,
            )
            for r in range(c_row + 1, scr.rows):
                scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e, selective=selective)
            self._notify_erase(Rect(c_row, c_col, 1, scr.cols - c_col), cause)
            if c_row + 1 < scr.rows:
                self._notify_erase(
                    Rect(c_row + 1, 0, scr.rows - c_row - 1, scr.cols),
                    cause,
                )
        elif mode == 1:
            for r in range(c_row):
                scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e, selective=selective)
            scr._rows[c_row].clear_range(
                0,
                c_col + 1,
                bce_w,
                bce_e,
                selective=selective,
            )
            if c_row > 0:
                self._notify_erase(Rect(0, 0, c_row, scr.cols), cause)
            self._notify_erase(Rect(c_row, 0, 1, c_col + 1), cause)
        elif mode in (2, 3):
            for r in range(scr.rows):
                scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e, selective=selective)
            if mode == 3 and not selective:
                scr.clear_history()
            self._notify_erase(Rect(0, 0, scr.rows, scr.cols), cause)
        elif mode == 22 and not selective:
            if not self._screens.active_is_alt:
                scr.push_scrollback_and_clear(bce_w, bce_e)
            else:
                for r in range(scr.rows):
                    scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e)
            self._notify_erase(Rect(0, 0, scr.rows, scr.cols), cause)
        self._set_cursor(scr, c_row, c_col, pending_wrap=False)

    def _erase_line(
        self,
        scr: Screen,
        mode: int,
        *,
        selective: bool,
    ) -> None:
        self._before_mutate()
        bce_w, bce_e = self._bce_pair()
        cause = EraseCause.DECSEL if selective else EraseCause.EL
        c_row = scr._cur_row
        c_col = scr._cur_col
        if mode == 0:
            scr._rows[c_row].clear_range(
                c_col,
                scr.cols,
                bce_w,
                bce_e,
                selective=selective,
            )
            self._notify_erase(Rect(c_row, c_col, 1, scr.cols - c_col), cause)
        elif mode == 1:
            scr._rows[c_row].clear_range(
                0,
                c_col + 1,
                bce_w,
                bce_e,
                selective=selective,
            )
            self._notify_erase(Rect(c_row, 0, 1, c_col + 1), cause)
        elif mode == 2:
            scr._rows[c_row].clear_range(
                0,
                scr.cols,
                bce_w,
                bce_e,
                selective=selective,
            )
            self._notify_erase(Rect(c_row, 0, 1, scr.cols), cause)
        self._set_cursor(scr, c_row, c_col, pending_wrap=False)

    def _handle_csi(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
    ) -> None:
        scr = self._screens.active
        top, bottom, left, right = self._effective_margins(scr)

        # DA2: CSI > c
        if prefix == ord(">") and not intermediates and final == ord("c"):
            self._emit_reply(self._profile.da2)
            return

        # XTVERSION: CSI > q
        if prefix == ord(">") and not intermediates and final == ord("q"):
            self._emit_reply(f"\x1bP>|{self._profile.xtversion}\x1b\\".encode("ascii"))
            return

        # Kitty keyboard protocol: CSI > flags u | CSI < n u | CSI = flags;m u | CSI ? u
        if (
            not intermediates
            and final == ord("u")
            and prefix
            in (
                ord(">"),
                ord("<"),
                ord("="),
                ord("?"),
            )
        ):
            kbd = scr.keyboard_stack
            if prefix == ord("?"):
                self._emit_reply(f"\x1b[?{int(kbd.flags)}u".encode("ascii"))
            elif prefix == ord(">"):
                kbd.push(params.raw_or_default(0, 0))
            elif prefix == ord("<"):
                kbd.pop(params.get(0, 1))
            elif prefix == ord("="):
                kbd.set_flags(params.raw_or_default(0, 0), params.get(1, 1))
            return

        # DECRQM: CSI ? Ps $ p or CSI Ps $ p
        if intermediates == b"$" and final == ord("p"):
            private = prefix == ord("?")
            code = params.raw_or_default(0, 0)
            pm = int(
                self._modes.decrpm(
                    code,
                    private=private,
                    active_is_alt=self._screens.active_is_alt,
                ),
            )
            q = "?" if private else ""
            self._emit_reply(f"\x1b[{q}{code};{pm}$y".encode("ascii"))
            return

        # DECSTR: CSI ! p
        if prefix == 0 and intermediates == b"!" and final == ord("p"):
            self.reset(soft=True)
            return

        # DECSCUSR: CSI Ps SP q
        if prefix == 0 and intermediates == b" " and final == ord("q"):
            ps = params.raw_or_default(0, 0)
            shape = CursorShape.BLOCK
            blinking = True
            if ps == 2:
                shape, blinking = CursorShape.BLOCK, False
            elif ps == 3:
                shape, blinking = CursorShape.UNDERLINE, True
            elif ps == 4:
                shape, blinking = CursorShape.UNDERLINE, False
            elif ps == 5:
                shape, blinking = CursorShape.BAR, True
            elif ps == 6:
                shape, blinking = CursorShape.BAR, False
            scr._cur_shape = shape
            scr._cur_blinking = blinking
            return

        # DECSCA: CSI Ps " q (character protection attribute)
        if prefix == 0 and intermediates == b'"' and final == ord("q"):
            ps = params.raw_or_default(0, 0)
            self._protected = ps == 1
            self._sync_pen_cache()
            return

        if intermediates:
            inter_s = intermediates.decode("ascii", errors="replace")
            pfx_s = chr(prefix) if prefix else ""
            self._unhandled("csi", f"CSI {pfx_s}{inter_s}{chr(final)}")
            return

        if prefix == ord("?"):
            if final in (ord("h"), ord("l")):
                enable = final == ord("h")
                for val in params.values:
                    self._apply_mode(val, private=True, enabled=enable)
            elif final == ord("J"):  # DECSED
                self._erase_display(scr, params.raw_or_default(0, 0), selective=True)
            elif final == ord("K"):  # DECSEL
                self._erase_line(scr, params.raw_or_default(0, 0), selective=True)
            elif final == ord("n"):  # Private DSR (?6n DECXCPR, ?996n color scheme)
                ps = params.raw_or_default(0, 0)
                if ps == 6:
                    r_out = (
                        (scr._cur_row - top + 1)
                        if self._modes.is_set(Mode.DECOM)
                        else (scr._cur_row + 1)
                    )
                    c_out = (
                        (scr._cur_col - left + 1)
                        if self._modes.is_set(Mode.DECOM)
                        else (scr._cur_col + 1)
                    )
                    self._emit_reply(f"\x1b[?{r_out};{c_out}R".encode("ascii"))
                elif ps == 996 and self._policy.allow_color_queries:
                    scheme = self._profile.color_scheme
                    self._emit_reply(f"\x1b[?997;{int(scheme)}n".encode("ascii"))
                    self._emit_event(ColorSchemeQueried(scheme=scheme))
            return

        if prefix != 0:
            if prefix == ord(">") and final in (ord("m"), ord("n")):
                # XTMODKEYS: accept quietly
                return
            self._unhandled("csi", f"CSI {chr(prefix)}{chr(final)}")
            return

        ch_final = chr(final)
        c_row = scr._cur_row
        c_col = scr._cur_col

        if ch_final == "m":
            self._style = parse_sgr(params, self._style, memo=self._sgr_memo)
            self._sync_pen_cache()
        elif ch_final in ("h", "l"):
            enable = ch_final == "h"
            for val in params.values:
                self._apply_mode(val, private=False, enabled=enable)
        elif ch_final == "A":  # CUU
            n = params.get(0, 1)
            floor = top if c_row >= top else 0
            self._set_cursor(scr, max(floor, c_row - n), c_col)
        elif ch_final == "B":  # CUD
            n = params.get(0, 1)
            ceil = bottom if c_row <= bottom else scr.rows - 1
            self._set_cursor(scr, min(ceil, c_row + n), c_col)
        elif ch_final == "C":  # CUF
            n = params.get(0, 1)
            ceil = right if c_col <= right else scr.cols - 1
            self._set_cursor(scr, c_row, min(ceil, c_col + n))
        elif ch_final == "D":  # CUB
            self._cursor_left(scr, params.get(0, 1), is_backspace=False)
        elif ch_final == "E":  # CNL
            n = params.get(0, 1)
            ceil = bottom if c_row <= bottom else scr.rows - 1
            self._set_cursor(scr, min(ceil, c_row + n), left)
        elif ch_final == "F":  # CPL
            n = params.get(0, 1)
            floor = top if c_row >= top else 0
            self._set_cursor(scr, max(floor, c_row - n), left)
        elif ch_final in ("G", "`"):  # CHA / HPA
            col_1 = params.get(0, 1)
            if self._modes.is_set(Mode.DECOM):
                target_c = min(right, left + col_1 - 1)
            else:
                target_c = min(scr.cols - 1, col_1 - 1)
            self._set_cursor(scr, c_row, max(0, target_c))
        elif ch_final == "I":  # CHT (Cursor Forward Tabulation)
            count = params.get(0, 1)
            ceil = right if c_col <= right else scr.cols - 1
            col_iter = c_col
            for _ in range(count):
                col_iter = next(
                    (t_c for t_c in scr._tabstops_sorted if col_iter < t_c <= ceil),
                    ceil,
                )
                if col_iter >= ceil:
                    break
            self._set_cursor(scr, c_row, col_iter, pending_wrap=False)
        elif ch_final == "Z":  # CBT (Cursor Backward Tabulation)
            count = params.get(0, 1)
            floor = left if c_col >= left else 0
            col_iter = c_col
            for _ in range(count):
                prev_tabs = [
                    t_c for t_c in scr._tabstops_sorted if floor <= t_c < col_iter
                ]
                col_iter = prev_tabs[-1] if prev_tabs else floor
                if col_iter <= floor:
                    break
            self._set_cursor(scr, c_row, col_iter, pending_wrap=False)
        elif ch_final == "d":  # VPA
            row_1 = params.get(0, 1)
            if self._modes.is_set(Mode.DECOM):
                target_r = min(bottom, top + row_1 - 1)
            else:
                target_r = min(scr.rows - 1, row_1 - 1)
            self._set_cursor(scr, max(0, target_r), c_col)
        elif ch_final in ("H", "f"):  # CUP / HVP
            row_1 = params.get(0, 1)
            col_1 = params.get(1, 1)
            if self._modes.is_set(Mode.DECOM):
                r = min(bottom, max(top, top + row_1 - 1))
                c = min(right, max(left, left + col_1 - 1))
            else:
                r = min(scr.rows - 1, max(0, row_1 - 1))
                c = min(scr.cols - 1, max(0, col_1 - 1))
            self._set_cursor(scr, r, c)
        elif ch_final == "J":  # ED
            self._erase_display(scr, params.raw_or_default(0, 0), selective=False)
        elif ch_final == "K":  # EL
            self._erase_line(scr, params.raw_or_default(0, 0), selective=False)
        elif ch_final == "X":  # ECH
            self._before_mutate()
            n = min(scr.cols - c_col, params.get(0, 1))
            bce_w, bce_e = self._bce_pair()
            scr._rows[c_row].clear_range(c_col, c_col + n, bce_w, bce_e)
            self._notify_erase(Rect(c_row, c_col, 1, n), EraseCause.ECH)
            self._set_cursor(scr, c_row, c_col, pending_wrap=False)
        elif ch_final == "@":  # ICH
            self._before_mutate()
            n = params.get(0, 1)
            bce_w, bce_e = self._bce_pair()
            scr._rows[c_row].insert_blanks(
                c_col,
                n,
                left=left,
                right=right,
                bce_word=bce_w,
                bce_ext=bce_e,
            )
            self._set_cursor(scr, c_row, c_col, pending_wrap=False)
        elif ch_final == "P":  # DCH
            self._before_mutate()
            n = params.get(0, 1)
            bce_w, bce_e = self._bce_pair()
            scr._rows[c_row].delete_cells(
                c_col,
                n,
                left=left,
                right=right,
                bce_word=bce_w,
                bce_ext=bce_e,
            )
            self._set_cursor(scr, c_row, c_col, pending_wrap=False)
        elif ch_final == "L":  # IL
            if top <= c_row <= bottom and left <= c_col <= right:
                self._before_mutate()
                n = params.get(0, 1)
                bce_w, bce_e = self._bce_pair()
                scr.scroll_down(
                    n,
                    top=c_row,
                    bottom=bottom,
                    left=left,
                    right=right,
                    bce_word=bce_w,
                    bce_ext=bce_e,
                )
                self._notify_erase(
                    Rect(c_row, left, min(n, bottom - c_row + 1), right - left + 1),
                    EraseCause.SCROLL,
                )
                self._set_cursor(scr, c_row, left)
        elif ch_final == "M":  # DL
            if top <= c_row <= bottom and left <= c_col <= right:
                self._before_mutate()
                n = params.get(0, 1)
                bce_w, bce_e = self._bce_pair()
                scr.scroll_up(
                    n,
                    top=c_row,
                    bottom=bottom,
                    left=left,
                    right=right,
                    save_history=False,
                    bce_word=bce_w,
                    bce_ext=bce_e,
                )
                self._notify_erase(
                    Rect(
                        max(c_row, bottom - n + 1),
                        left,
                        min(n, bottom - c_row + 1),
                        right - left + 1,
                    ),
                    EraseCause.SCROLL,
                )
                self._set_cursor(scr, c_row, left)
        elif ch_final == "S":  # SU
            self._before_mutate()
            n = params.get(0, 1)
            bce_w, bce_e = self._bce_pair()
            scr.scroll_up(
                n,
                top=top,
                bottom=bottom,
                left=left,
                right=right,
                save_history=False,
                bce_word=bce_w,
                bce_ext=bce_e,
            )
            self._notify_erase(
                Rect(
                    max(top, bottom - n + 1),
                    left,
                    min(n, bottom - top + 1),
                    right - left + 1,
                ),
                EraseCause.SCROLL,
            )
            self._clear_cluster_anchor()
        elif ch_final == "T":  # SD
            self._before_mutate()
            n = params.get(0, 1)
            bce_w, bce_e = self._bce_pair()
            scr.scroll_down(
                n,
                top=top,
                bottom=bottom,
                left=left,
                right=right,
                bce_word=bce_w,
                bce_ext=bce_e,
            )
            self._notify_erase(
                Rect(top, left, min(n, bottom - top + 1), right - left + 1),
                EraseCause.SCROLL,
            )
            self._clear_cluster_anchor()
        elif ch_final == "b":  # REP
            if self._last_printed:
                count = min(self._limits.max_rep_count, params.get(0, 1))
                self._handle_print(self._last_printed * count)
        elif ch_final == "r":  # DECSTBM
            t_arg = params.get(0, 1) - 1
            b_arg = params.get(1, scr.rows) - 1
            t_clamped = max(0, min(scr.rows - 1, t_arg))
            b_clamped = max(0, min(scr.rows - 1, b_arg))
            if t_clamped < b_clamped:
                sr = scr.scroll_region
                scr.scroll_region = ScrollRegion(
                    top=t_clamped,
                    bottom=b_clamped,
                    left=sr.left,
                    right=sr.right,
                )
                decom = self._modes.is_set(Mode.DECOM)
                _, _, eff_left, _ = self._effective_margins(scr)
                self._set_cursor(
                    scr,
                    t_clamped if decom else 0,
                    eff_left if decom else 0,
                )
        elif ch_final == "s":  # DECSLRM (when ?69 set) or SCOSC
            if self._modes.is_set(Mode.DECLRMM):
                l_arg = params.get(0, 1) - 1
                r_arg = params.get(1, scr.cols) - 1
                l_clamped = max(0, min(scr.cols - 1, l_arg))
                r_clamped = max(0, min(scr.cols - 1, r_arg))
                if l_clamped < r_clamped:
                    sr = scr.scroll_region
                    scr.scroll_region = ScrollRegion(
                        top=sr.top,
                        bottom=sr.bottom,
                        left=l_clamped,
                        right=r_clamped,
                    )
                    decom = self._modes.is_set(Mode.DECOM)
                    self._set_cursor(
                        scr,
                        sr.top if decom else 0,
                        l_clamped if decom else 0,
                    )
            else:
                self._save_cursor()
        elif ch_final == "u":  # SCORC
            self._restore_cursor()
        elif ch_final == "g":  # TBC
            mode = params.raw_or_default(0, 0)
            if mode == 0:
                scr.clear_tabstop(c_col)
            elif mode == 3:
                scr.clear_all_tabstops()
        elif ch_final == "c":  # DA1
            if params.raw_or_default(0, 0) == 0:
                self._emit_reply(self._profile.da1)
        elif ch_final == "n":  # DSR
            ps = params.raw_or_default(0, 0)
            if ps == 5:
                self._emit_reply(b"\x1b[0n")
            elif ps == 6:
                r_out = (
                    (c_row - top + 1) if self._modes.is_set(Mode.DECOM) else (c_row + 1)
                )
                c_out = (
                    (c_col - left + 1)
                    if self._modes.is_set(Mode.DECOM)
                    else (c_col + 1)
                )
                self._emit_reply(f"\x1b[{r_out};{c_out}R".encode("ascii"))
        elif ch_final == "t":  # XTWINOPS
            op = params.raw_or_default(0, 0)
            if op == 18:  # report text area size in chars
                self._emit_reply(f"\x1b[8;{scr.rows};{scr.cols}t".encode("ascii"))
            elif op == 14:  # report text area size in pixels (16x8 nominal cell)
                self._emit_reply(
                    f"\x1b[4;{scr.rows * 16};{scr.cols * 8}t".encode("ascii"),
                )
            elif op == 21 and self._policy.allow_title_report:
                safe_title = "".join(
                    ch for ch in self._title if " " <= ch <= "~" and ch != "\\"
                )
                self._emit_reply(f"\x1b]l{safe_title}\x1b\\".encode("ascii"))
            elif op == 22:  # push title onto stack
                if len(self._title_stack) < 16:
                    self._title_stack.append(self._title)
            elif op == 23 and self._title_stack:  # pop title from stack
                self._title = self._title_stack.pop()
                self._emit_event(TitleChanged(title=self._title))
        else:
            self._unhandled("csi", f"CSI {ch_final}")

    def _update_cwd(self, raw_uri: str) -> None:
        cleaned = sanitize_terminal_text(raw_uri.strip())
        if cleaned.startswith("file://"):
            parsed = urllib.parse.urlsplit(cleaned)
            host = parsed.hostname or ""
            local_host = os.uname().nodename if hasattr(os, "uname") else ""
            if host not in ("", "localhost", local_host):
                return
            path = urllib.parse.unquote(parsed.path)
        else:
            path = cleaned
        self._cwd = path
        self._emit_event(WorkingDirectoryChanged(uri=cleaned, path=path))

    def _handle_osc(self, code: int, data: bytes, terminator: bytes) -> None:
        scr = self._screens.active
        out_term = b"\x07" if terminator == b"\x07" else b"\x1b\\"

        if code in (0, 2):
            text = data.decode("utf-8", errors="replace")
            self._title = text
            self._emit_event(TitleChanged(title=text))
        elif code == 1:
            return
        elif code == 7:
            self._update_cwd(data.decode("utf-8", errors="replace"))
        elif code == 8:
            text = data.decode("utf-8", errors="replace")
            sep = text.find(";")
            if sep == -1:
                return
            params_part = text[:sep]
            uri = text[sep + 1 :]
            if not uri:
                self._hyperlink = None
            else:
                link_id = ""
                for kv in params_part.split(":"):
                    if kv.startswith("id="):
                        link_id = kv[3:]
                        break
                self._hyperlink = Hyperlink(uri=uri, id=link_id)
            self._sync_pen_cache()
        elif code == 9:
            text = data.decode("utf-8", errors="replace")
            parts = text.split(";")
            if len(parts) >= 2 and parts[0] == "4" and parts[1].isdigit():
                sub = int(parts[1])
                prog = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                changed, status = self._status_store.apply_osc_9_4(sub, prog)
                if changed:
                    self._emit_event(ProgramStatusChanged(status=status, task_id=""))
            elif len(parts) >= 2 and parts[0] == "9":
                self._update_cwd(";".join(parts[1:]))
        elif code in (10, 11) and data == b"?" and self._policy.allow_color_queries:
            r, g, b = self._profile.fg_rgb if code == 10 else self._profile.bg_rgb
            reply = (
                f"\x1b]{code};rgb:{r:02x}{r:02x}/{g:02x}{g:02x}/{b:02x}{b:02x}".encode(
                    "ascii",
                )
            )
            self._emit_reply(reply + out_term)
        elif code == 52:
            semi = data.find(b";")
            if semi == -1:
                return
            sel = data[:semi].decode("ascii", errors="ignore") or "c"
            body = data[semi + 1 :]
            if body == b"?":
                if self._policy.allow_clipboard_read:
                    self._emit_reply(f"\x1b]52;{sel};\x1b\\".encode("ascii"))
                return
            if self._policy.allow_clipboard_write:
                try:
                    raw_clip = base64.b64decode(body, validate=False)
                    clip_text = raw_clip.decode("utf-8", errors="replace")
                except (binascii.Error, ValueError):
                    return
                self._emit_event(ClipboardWrite(selection=sel, text=clip_text))
        elif code == 133:
            text = data.decode("utf-8", errors="replace")
            parts = text.split(";")
            raw_kind = parts[0][:1] if parts and parts[0] else ""
            if raw_kind in ("A", "B", "C", "D"):
                kind = PromptKind(raw_kind)
                exit_code: int | None = None
                is_continuation = any(p.strip() == "k=s" for p in parts[1:])
                if (
                    kind == PromptKind.COMMAND_FINISHED
                    and len(parts) >= 2
                    and parts[1].lstrip("-").isdigit()
                ):
                    exit_code = int(parts[1])
                abs_r = self._screens.primary.scrolled_total + scr._cur_row
                zone = PromptZone(
                    kind=kind,
                    row=scr._cur_row,
                    col=scr._cur_col,
                    exit_code=exit_code,
                    abs_row=abs_r,
                )
                self._prompt_zones.append(zone)
                self._step_event |= StopOn.PROMPT_MARK
                self._emit_event(PromptMark(zone=zone))
                if (
                    kind == PromptKind.PROMPT_START
                    and not is_continuation
                    and self._status_store.on_prompt_start()
                ):
                    self._emit_event(
                        ProgramStatusChanged(status=self._status_store.most_urgent),
                    )
        elif code == 7501:
            if data == b"?":
                has_consumer = (
                    self._policy.allow_program_status_query
                    or self._on_event is not None
                    or self._record_events
                )
                if has_consumer:
                    cap = self._profile.osc_7501_capabilities
                    self._emit_reply(f"\x1b]7501;{cap}".encode("ascii") + out_term)
                return
            changed, rec = self._status_store.apply_osc_7501(data)
            if changed:
                tid = rec.id if rec is not None else ""
                self._emit_event(ProgramStatusChanged(status=rec, task_id=tid))
        else:
            self._unhandled("osc", f"OSC {code}")

    def _handle_decrqss(self, req: bytes) -> None:
        scr = self._screens.active
        if req == b"m":
            sgr_body = _style_to_sgr(self._style)[2:]  # strip leading ESC [
            self._emit_reply(f"\x1bP1$r{sgr_body}\x1b\\".encode("ascii"))
        elif req == b"r":
            sr = scr.scroll_region
            self._emit_reply(
                f"\x1bP1$r{sr.top + 1};{sr.bottom + 1}r\x1b\\".encode("ascii"),
            )
        elif req == b"s" and self._modes.is_set(Mode.DECLRMM):
            sr = scr.scroll_region
            self._emit_reply(
                f"\x1bP1$r{sr.left + 1};{sr.right + 1}s\x1b\\".encode("ascii"),
            )
        elif req == b" q":
            shape_map = {
                (CursorShape.BLOCK, True): 1,
                (CursorShape.BLOCK, False): 2,
                (CursorShape.UNDERLINE, True): 3,
                (CursorShape.UNDERLINE, False): 4,
                (CursorShape.BAR, True): 5,
                (CursorShape.BAR, False): 6,
            }
            code = shape_map[(scr._cur_shape, scr._cur_blinking)]
            self._emit_reply(f"\x1bP1$r{code} q\x1b\\".encode("ascii"))
        elif req == b'"q':
            code = 1 if self._protected else 0
            self._emit_reply(f'\x1bP1$r{code}"q\x1b\\'.encode("ascii"))
        else:
            self._emit_reply(b"\x1bP0$r\x1b\\")

    def _handle_xtgettcap(self, data: bytes) -> None:
        for hex_name in data.split(b";"):
            if not hex_name:
                continue
            try:
                cap_name = bytes.fromhex(hex_name.decode("ascii")).decode("ascii")
            except ValueError:
                self._emit_reply(b"\x1bP0+r\x1b\\")
                continue
            if cap_name in _TERMINFO_CAPS:
                val = _TERMINFO_CAPS[cap_name]
                enc_k = _hex_encode(cap_name)
                if val:
                    enc_v = _hex_encode(val)
                    self._emit_reply(f"\x1bP1+r{enc_k}={enc_v}\x1b\\".encode("ascii"))
                else:
                    self._emit_reply(f"\x1bP1+r{enc_k}\x1b\\".encode("ascii"))
            else:
                self._emit_reply(b"\x1bP0+r\x1b\\")

    def _handle_dcs(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
        data: bytes,
    ) -> None:
        # DECRQSS: DCS $ q <req> ST
        if prefix == 0 and intermediates == b"$" and final == ord("q"):
            self._handle_decrqss(data)
            return

        # XTGETTCAP: DCS + q <hex-names> ST
        if prefix == 0 and intermediates == b"+" and final == ord("q"):
            self._handle_xtgettcap(data)
            return

        # tmux passthrough: DCS tmux; ESC ESC ... ST (final='t', data=b"mux;...")
        if (
            self._policy.unwrap_tmux_passthrough
            and self._passthrough_depth < 2
            and prefix == 0
            and not intermediates
            and final == ord("t")
            and data.startswith(b"mux;")
        ):
            unwrapped = memoryview(data)[4:]
            inner_parser = Parser(limits=self._limits)
            self._passthrough_depth += 1
            try:
                inner_parser.feed_to(unwrapped, self._sink)
            finally:
                self._passthrough_depth -= 1
            return

        # SIXEL: DCS Ps... q <sixel> ST
        if prefix == 0 and not intermediates and final == ord("q"):
            if self._record_events or self._on_event is not None:
                scr = self._screens.active
                self._emit_event(
                    SixelGraphic(
                        data=SixelData(
                            row=scr._cur_row,
                            col=scr._cur_col,
                            params=params.values,
                            payload=data,
                        ),
                    ),
                )
            return

        inter_s = intermediates.decode("ascii", errors="replace")
        self._unhandled("dcs", f"DCS {inter_s}{chr(final)}")

    def _handle_apc(self, data: bytes) -> None:
        if data.startswith(b"G"):
            if self._record_events or self._on_event is not None:
                body = data[1:]
                sep = body.find(b";")
                ctrl_bytes = body if sep == -1 else body[:sep]
                payload = b"" if sep == -1 else body[sep + 1 :]
                control: dict[str, str] = {}
                for pair in ctrl_bytes.decode("ascii", errors="ignore").split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        control[k] = v
                scr = self._screens.active
                self._emit_event(
                    KittyGraphic(
                        command=KittyCommand(
                            row=scr._cur_row,
                            col=scr._cur_col,
                            control=control,
                            payload=payload,
                        ),
                    ),
                )
            return
        self._unhandled("apc", f"APC len={len(data)}")


def render(
    data: collections.abc.Buffer | str,
    /,
    *,
    rows: int = 24,
    cols: int = 80,
    profile: Profile = DEFAULT_PROFILE,
) -> str:
    """Emulate *data* on a fresh ``(rows, cols)`` terminal and return ``text``."""
    term = Terminal(rows=rows, cols=cols, scrollback=0, profile=profile)
    if isinstance(data, str):
        term.feed_str(data)
    else:
        term.feed(data)
    return term.text


class _StripSink:
    __slots__ = ("parts",)

    def __init__(self) -> None:
        self.parts: list[str] = []

    def on_print(self, text: str) -> None:
        self.parts.append(text)

    def on_execute(self, byte: int) -> None:
        if byte == 0x0A:
            self.parts.append("\n")
        elif byte == 0x09:
            self.parts.append("\t")

    def on_esc(self, intermediates: bytes, final: int) -> None:
        pass

    def on_csi(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
    ) -> None:
        pass

    def on_osc(self, code: int, data: bytes, terminator: bytes) -> None:
        pass

    def on_dcs(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
        data: bytes,
    ) -> None:
        pass

    def on_apc(self, data: bytes) -> None:
        pass


def strip(data: collections.abc.Buffer | str, /) -> str:
    """Strip VT escape, CSI, OSC, DCS, and APC sequences, keeping plain text."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    sink = _StripSink()
    Parser().feed_to(raw, sink)
    return "".join(sink.parts)


__all__ = [
    "FlightRecorder",
    "Formatter",
    "OutputFormat",
    "Snapshot",
    "StepResult",
    "StopOn",
    "Terminal",
    "render",
    "strip",
]
