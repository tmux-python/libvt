"""Sans-I/O virtual terminal state machine, immutable snapshots, and formatters."""

from __future__ import annotations

import collections.abc
import enum
import html
import typing as t
from dataclasses import dataclass

from libvt._grid import (
    _CellExt,
    _Row,
    make_cell_ext,
    pack_pen_base,
    unpack_style,
    unpack_width_role,
)
from libvt._width import (
    WidthRole,
    char_width,
    cluster_prefers_wide,
    is_grapheme_extender,
)
from libvt.modes import (
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
    EraseCause,
    PromptZone,
    Rect,
    RegionView,
    RowView,
    Screen,
    ScreenSet,
    ScrollRegion,
    format_rows_text,
    slice_grid,
)
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

_MAX_REPLY_BYTES = 65_536
_MAX_REP_COUNT = 4_096


@dataclass(frozen=True, slots=True)
class ProgramStatus:
    """Semantic program progress status emitted via ``OSC 7501``.

    Attributes
    ----------
    value : str
        Raw status token or label reported by the foreground program.
    progress : int | None
        Optional percentage in ``0..100``, when supplied.
    """

    value: str
    progress: int | None = None


@dataclass(frozen=True, slots=True)
class SixelData:
    """Zero-dependency ``DCS ... q`` SIXEL payload notification.

    Attributes
    ----------
    row : int
        Zero-based cursor row at SIXEL entry.
    col : int
        Zero-based cursor column at SIXEL entry.
    params : tuple[int, ...]
        Parsed ``DCS`` numeric header parameters.
    payload : bytes
        Raw SIXEL data bytes up to ``ST``.
    """

    row: int
    col: int
    params: tuple[int, ...]
    payload: bytes


@dataclass(frozen=True, slots=True)
class KittyCommand:
    """Zero-dependency ``APC G`` Kitty graphics protocol notification.

    Attributes
    ----------
    row : int
        Zero-based cursor row when the command arrived.
    col : int
        Zero-based cursor column when the command arrived.
    control : dict[str, str]
        Key-value control header pairs before ``;``.
    payload : bytes
        Encoded payload bytes following ``;``.
    """

    row: int
    col: int
    control: dict[str, str]
    payload: bytes


@dataclass(frozen=True, slots=True)
class Effects:
    """Read-only-by-default callback and query-policy configuration.

    Attributes
    ----------
    on_pty_write : Callable[[bytes], None] | None
        Optional sink receiving query reply bytes immediately as generated.
    on_render_hold : Callable[[bool], None] | None
        Called with ``True`` on ``?2026`` sync start and ``False`` on sync end.
    on_bell : Callable[[], None] | None
        Called when ``BEL`` (``0x07``) executes in ``GROUND``.
    on_title : Callable[[str], None] | None
        Called when ``OSC 0`` or ``OSC 2`` updates the window title.
    on_osc_133 : Callable[[str, PromptZone], None] | None
        Called when an ``OSC 133`` semantic prompt marker is recorded.
    on_osc_7501 : Callable[[ProgramStatus], None] | None
        Called when ``OSC 7501`` updates foreground program status.
    on_sixel : Callable[[SixelData], None] | None
        Called when a ``DCS ... q`` SIXEL sequence completes.
    on_kitty_graphics : Callable[[KittyCommand], None] | None
        Called when an ``APC G`` Kitty graphics sequence completes.
    on_erase : Callable[[Rect, EraseCause, bool], None] | None
        Called with ``(rect, cause, is_alt_screen)`` when cells clear or scroll.
    allow_title_report : bool
        Permit ``CSI 21 t`` window-title report replies (default ``False``).
    allow_checksum_report : bool
        Permit ``DECRQCRA`` rectangular checksum replies (default ``False``).
    allow_clipboard_read : bool
        Permit ``OSC 52 ; c ; ?`` clipboard query replies (default ``False``).
    """

    on_pty_write: collections.abc.Callable[[bytes], None] | None = None
    on_render_hold: collections.abc.Callable[[bool], None] | None = None
    on_bell: collections.abc.Callable[[], None] | None = None
    on_title: collections.abc.Callable[[str], None] | None = None
    on_osc_133: collections.abc.Callable[[str, PromptZone], None] | None = None
    on_osc_7501: collections.abc.Callable[[ProgramStatus], None] | None = None
    on_sixel: collections.abc.Callable[[SixelData], None] | None = None
    on_kitty_graphics: collections.abc.Callable[[KittyCommand], None] | None = None
    on_erase: collections.abc.Callable[[Rect, EraseCause, bool], None] | None = None
    allow_title_report: bool = False
    allow_checksum_report: bool = False
    allow_clipboard_read: bool = False


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


@dataclass(frozen=True, slots=True)
class _SavedCursor:
    """Internal ``DECSC`` saved cursor slot.

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
    """

    row: int = 0
    col: int = 0
    pending_wrap: bool = False
    style: Style = DEFAULT_STYLE
    decom: bool = False


class Snapshot:
    """Immutable point-in-time capture of terminal screen and metadata."""

    __slots__ = (
        "_cols",
        "_rows",
        "active_is_alt",
        "cursor",
        "generation",
        "modes",
        "program_status",
        "prompt_zones",
        "title",
    )

    def __init__(
        self,
        *,
        rows: tuple[_Row, ...],
        cols: int,
        cursor: Cursor,
        title: str,
        modes: frozenset[Mode],
        active_is_alt: bool,
        prompt_zones: tuple[PromptZone, ...],
        program_status: ProgramStatus | None,
        generation: int,
    ) -> None:
        self._rows = rows
        self._cols = cols
        self.cursor = cursor
        self.title = title
        self.modes = modes
        self.active_is_alt = active_is_alt
        self.prompt_zones = prompt_zones
        self.program_status = program_status
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
    def lines(self) -> tuple[str, ...]:
        """Return right-trimmed text for every row in the snapshot."""
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
    ) -> str:
        """Render this snapshot using :class:`Formatter`."""
        return Formatter(output_format).format_snapshot(self)


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


class Formatter:
    """Serialize a :class:`Snapshot` or :class:`Terminal` to text, VT, or HTML."""

    __slots__ = ("output_format",)

    def __init__(
        self,
        output_format: OutputFormat = OutputFormat.PLAIN,
    ) -> None:
        self.output_format = output_format

    def format_snapshot(self, snapshot: Snapshot) -> str:
        """Render *snapshot* in :attr:`output_format`."""
        if self.output_format == OutputFormat.PLAIN:
            return snapshot.text
        if self.output_format == OutputFormat.VT:
            return self._render_vt(snapshot)
        return self._render_html(snapshot)

    def _render_vt(self, snapshot: Snapshot) -> str:
        out: list[str] = []
        # Never re-assert transient ?2026 in static VT exports.
        export_modes = snapshot.modes - TRANSIENT_SNAPSHOT_MODES
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
                role = unpack_width_role(row.attrs[c])
                if role == WidthRole.SPACER_TAIL:
                    continue
                ext = row.ext.get(c)
                style = unpack_style(row.attrs[c], ext)
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
        for row in snapshot._rows:
            chunks: list[str] = []
            for c in range(row.used):
                if unpack_width_role(row.attrs[c]) == WidthRole.SPACER_TAIL:
                    continue
                ext = row.ext.get(c)
                style = unpack_style(row.attrs[c], ext)
                escaped = html.escape(row.chars[c])
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
                if ext is not None and ext.hyperlink is not None:
                    href = html.escape(ext.hyperlink.uri, quote=True)
                    span = f'<a href="{href}">{span}</a>'
                chunks.append(span)
            lines.append("".join(chunks))
        lines.append("</pre>")
        return "\n".join(lines)


class Terminal:
    """Sans-I/O virtual terminal emulator over a streaming VT500 parser."""

    def __init__(
        self,
        *,
        rows: int = 24,
        cols: int = 80,
        scrollback: int = 2_000,
        effects: Effects | None = None,
    ) -> None:
        self._screens = ScreenSet(rows=rows, cols=cols, scrollback=scrollback)
        self._modes = ModeState()
        self._parser = Parser()
        self._effects = effects if effects is not None else Effects()
        self._style: Style = DEFAULT_STYLE
        self._pen_word: int = 0
        self._hyperlink: Hyperlink | None = None
        self._cell_ext: _CellExt | None = None
        self._saved_primary = _SavedCursor()
        self._saved_alt = _SavedCursor()
        self._last_printed: str = ""
        self._last_printed_pos: tuple[int, int] | None = None
        self._title: str = ""
        self._prompt_zones: list[PromptZone] = []
        self._program_status: ProgramStatus | None = None
        self._replies = bytearray()
        self._generation: int = 0
        self._cached_snapshot: Snapshot | None = None
        self._held_snapshot: Snapshot | None = None
        self._step_event: StopOn = StopOn.NONE

    @property
    def rows(self) -> int:
        """Return active screen height in rows."""
        return self._screens.active.rows

    @property
    def cols(self) -> int:
        """Return active screen width in columns."""
        return self._screens.active.cols

    @property
    def screen(self) -> Screen:
        """Return the currently active :class:`~libvt.screen.Screen`."""
        return self._screens.active

    @property
    def screens(self) -> ScreenSet:
        """Return the dual primary/alternate :class:`~libvt.screen.ScreenSet`."""
        return self._screens

    @property
    def cursor(self) -> Cursor:
        """Return current zero-based :class:`~libvt.screen.Cursor`."""
        return self._screens.active.cursor

    @property
    def title(self) -> str:
        """Return window title set by ``OSC 0`` / ``OSC 2``."""
        return self._title

    @property
    def modes(self) -> ModeState:
        """Return current :class:`~libvt.modes.ModeState`."""
        return self._modes

    @property
    def synchronized(self) -> bool:
        """Return ``True`` while DEC mode ``?2026`` synchronized output is active."""
        return self._modes.is_set(Mode.SYNCHRONIZED_OUTPUT)

    @property
    def prompt_zones(self) -> tuple[PromptZone, ...]:
        """Return recorded ``OSC 133`` prompt zones."""
        return tuple(self._prompt_zones)

    @property
    def program_status(self) -> ProgramStatus | None:
        """Return latest ``OSC 7501`` program status, if any."""
        return self._program_status

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
        if self._held_snapshot is not None:
            return self._held_snapshot
        return self.snapshot()

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
            cols=scr.cols,
            cursor=scr.cursor,
            title=self._title,
            modes=self._modes.enabled_modes(),
            active_is_alt=self._screens.active_is_alt,
            prompt_zones=tuple(self._prompt_zones),
            program_status=self._program_status,
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

    def drain_replies(self) -> bytes:
        """Return and clear buffered sans-I/O query reply bytes."""
        if not self._replies:
            return b""
        out = bytes(self._replies)
        self._replies.clear()
        return out

    def _emit_reply(self, payload: bytes) -> None:
        if self._effects.on_pty_write is not None:
            self._effects.on_pty_write(payload)
            return
        if len(self._replies) + len(payload) <= _MAX_REPLY_BYTES:
            self._replies.extend(payload)

    def _sync_pen_cache(self) -> None:
        self._pen_word = pack_pen_base(self._style)
        self._cell_ext = make_cell_ext(self._style, self._hyperlink)

    def _bce_pair(self) -> tuple[int, _CellExt | None]:
        if self._style.bg is None and self._hyperlink is None:
            return 0, None
        bce_style = Style(bg=self._style.bg)
        return pack_pen_base(bce_style), make_cell_ext(bce_style, self._hyperlink)

    def _notify_erase(self, rect: Rect, cause: EraseCause) -> None:
        if self._effects.on_erase is not None and rect.rows > 0 and rect.cols > 0:
            self._effects.on_erase(rect, cause, self._screens.active_is_alt)

    def resize(self, *, rows: int, cols: int) -> None:
        """Resize terminal screens to ``(rows, cols)``."""
        self._screens.resize(rows=rows, cols=cols)
        self._generation += 1

    def reset(self, *, soft: bool = False) -> None:
        """Reset terminal state (``DECSTR`` when *soft* is ``True``, else ``RIS``)."""
        self._style = DEFAULT_STYLE
        self._hyperlink = None
        self._sync_pen_cache()
        self._last_printed = ""
        self._last_printed_pos = None
        was_sync = self.synchronized
        self._modes = ModeState()
        self._held_snapshot = None
        if was_sync and self._effects.on_render_hold is not None:
            self._effects.on_render_hold(False)
        if soft:
            scr = self._screens.active
            scr.scroll_region = ScrollRegion(
                top=0,
                bottom=scr.rows - 1,
                left=0,
                right=scr.cols - 1,
            )
            scr.cursor = Cursor(
                row=scr.cursor.row,
                col=scr.cursor.col,
                visible=True,
                shape=scr.cursor.shape,
                blinking=scr.cursor.blinking,
                pending_wrap=False,
            )
        else:
            self._parser.reset()
            self._screens.active_is_alt = False
            self._screens.primary.reset()
            self._screens.alternate.reset()
            self._title = ""
            self._prompt_zones.clear()
            self._program_status = None
            self._notify_erase(
                Rect(0, 0, self.rows, self.cols),
                EraseCause.RESET,
            )
        self._generation += 1

    def feed_str(self, text: str) -> None:
        """Encode *text* as UTF-8 and feed into the terminal."""
        self.feed(text.encode("utf-8"))

    def feed(self, data: collections.abc.Buffer) -> None:
        """Consume all bytes in *data*, updating terminal state in place."""
        mv = memoryview(data)
        if not mv:
            return
        self._parser.feed_to(mv, self)
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
            # Fast-advance ground printable runs when GROUND stop is not requested.
            if self._parser.state == ParserState.GROUND and not (stop & StopOn.GROUND):
                end = idx
                while end < n and mv[end] >= 0x20 and mv[end] != 0x7F:
                    end += 1
                if end > idx:
                    self._parser.feed_to(mv[idx:end], self)
                    idx = end
                    self._generation += 1
                    continue
            self._parser.feed_to(mv[idx : idx + 1], self)
            idx += 1
            self._generation += 1
            matched = self._step_event & stop
            if (stop & StopOn.GROUND) and self._parser.state == ParserState.GROUND:
                matched |= StopOn.GROUND
            if matched:
                return StepResult(offset=idx, stopped_on=matched)
        return StepResult(
            offset=n,
            stopped_on=StopOn.EOF if (stop & StopOn.EOF) else StopOn.NONE,
        )

    # --- ActionSink callbacks invoked by Parser.feed_to ---

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
    ) -> None:
        cur = scr.cursor
        scr.cursor = Cursor(
            row=max(0, min(scr.rows - 1, row)),
            col=max(0, min(scr.cols - 1, col)),
            visible=cur.visible,
            shape=cur.shape,
            blinking=cur.blinking,
            pending_wrap=pending_wrap,
        )

    def _linefeed(self, scr: Screen, *, carriage_return: bool = False) -> None:
        top, bottom, left, right = self._effective_margins(scr)
        cur = scr.cursor
        col = left if carriage_return else cur.col
        if cur.row == bottom and left <= cur.col <= right:
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
        elif cur.row < scr.rows - 1:
            self._set_cursor(scr, cur.row + 1, col, pending_wrap=False)
        else:
            self._set_cursor(scr, cur.row, col, pending_wrap=False)

    def on_print(self, text: str) -> None:
        """Write printable Unicode characters onto the active screen."""
        scr = self._screens.active
        autowrap = self._modes.is_set(Mode.DECAWM)
        insert_mode = self._modes.is_set(Mode.IRM)
        _, _, left, right = self._effective_margins(scr)

        if text.isascii() and not insert_mode:
            pos = 0
            total = len(text)
            while pos < total:
                cur = scr.cursor
                max_col = right if cur.col <= right else scr.cols - 1
                min_col = left if cur.col >= left else 0
                if cur.pending_wrap and autowrap:
                    scr._rows[cur.row].wrapped = True
                    self._linefeed(scr)
                    cur = scr.cursor
                    self._set_cursor(scr, cur.row, min_col, pending_wrap=False)
                    cur = scr.cursor
                avail = max_col - cur.col + 1
                seg = text[pos : pos + avail]
                seg_len = len(seg)
                scr._rows[cur.row].write_ascii_slice(
                    cur.col,
                    seg,
                    self._pen_word,
                    self._cell_ext,
                )
                pos += seg_len
                end_col = cur.col + seg_len - 1
                self._last_printed = seg[-1]
                self._last_printed_pos = (cur.row, end_col)
                if cur.col + seg_len > max_col:
                    self._set_cursor(
                        scr,
                        cur.row,
                        max_col,
                        pending_wrap=autowrap,
                    )
                else:
                    self._set_cursor(
                        scr,
                        cur.row,
                        cur.col + seg_len,
                        pending_wrap=False,
                    )
            return

        mode_2027 = self._modes.is_set(Mode.GRAPHEME_CLUSTERING)

        for ch in text:
            if self._last_printed_pos is not None and is_grapheme_extender(
                ch,
                self._last_printed,
                mode_2027=mode_2027,
            ):
                pr, pc = self._last_printed_pos
                if 0 <= pr < scr.rows and 0 <= pc < scr.cols:
                    new_cluster = self._last_printed + ch
                    widen = cluster_prefers_wide(
                        new_cluster,
                        mode_2027=mode_2027,
                    )
                    scr._rows[pr].append_grapheme(
                        pc,
                        ch,
                        widen=widen,
                        pen_word=self._pen_word,
                        cell_ext=self._cell_ext,
                    )
                    self._last_printed = new_cluster
                    if widen and scr.cursor.row == pr and scr.cursor.col == pc + 1:
                        if pc + 1 >= right:
                            self._set_cursor(
                                scr,
                                pr,
                                right,
                                pending_wrap=autowrap,
                            )
                        else:
                            self._set_cursor(scr, pr, pc + 2, pending_wrap=False)
                    continue

            width = char_width(ch)
            if width == 0:
                continue

            cur = scr.cursor
            max_col = right if cur.col <= right else scr.cols - 1
            min_col = left if cur.col >= left else 0

            if cur.pending_wrap and autowrap:
                scr._rows[cur.row].wrapped = True
                self._linefeed(scr)
                cur = scr.cursor
                self._set_cursor(scr, cur.row, min_col, pending_wrap=False)
                cur = scr.cursor

            if width == 2 and cur.col == max_col:
                if autowrap and max_col - min_col >= 1:
                    bce_w, bce_e = self._bce_pair()
                    scr._rows[cur.row].clear_range(
                        cur.col,
                        cur.col + 1,
                        bce_w,
                        bce_e,
                    )
                    scr._rows[cur.row].wrapped = True
                    self._linefeed(scr)
                    cur = scr.cursor
                    self._set_cursor(scr, cur.row, min_col, pending_wrap=False)
                    cur = scr.cursor
                else:
                    # Single-column region cannot fit a wide glyph; write replacement.
                    ch = "\ufffd"
                    width = 1

            row_obj = scr._rows[cur.row]
            if insert_mode:
                bce_w, _ = self._bce_pair()
                row_obj.insert_blanks(
                    cur.col,
                    width,
                    left=min_col,
                    right=max_col,
                    bce_word=bce_w,
                )

            if width == 2 and cur.col + 1 <= max_col:
                row_obj.write_wide(
                    cur.col,
                    ch,
                    self._pen_word,
                    self._cell_ext,
                )
                self._last_printed = ch
                self._last_printed_pos = (cur.row, cur.col)
                if cur.col + 1 >= max_col:
                    self._set_cursor(
                        scr,
                        cur.row,
                        max_col,
                        pending_wrap=autowrap,
                    )
                else:
                    self._set_cursor(scr, cur.row, cur.col + 2, pending_wrap=False)
            else:
                row_obj.write_narrow(
                    cur.col,
                    ch,
                    self._pen_word,
                    self._cell_ext,
                )
                self._last_printed = ch
                self._last_printed_pos = (cur.row, cur.col)
                if cur.col >= max_col:
                    self._set_cursor(
                        scr,
                        cur.row,
                        max_col,
                        pending_wrap=autowrap,
                    )
                else:
                    self._set_cursor(scr, cur.row, cur.col + 1, pending_wrap=False)

    def on_execute(self, byte: int) -> None:
        """Execute a C0 control character."""
        scr = self._screens.active
        cur = scr.cursor
        _, _, left, right = self._effective_margins(scr)
        if byte == 0x07:  # BEL
            if self._effects.on_bell is not None:
                self._effects.on_bell()
        elif byte == 0x08:  # BS
            floor = left if cur.col >= left else 0
            self._set_cursor(scr, cur.row, max(floor, cur.col - 1), pending_wrap=False)
        elif byte == 0x09:  # HT
            ceil = right if cur.col <= right else scr.cols - 1
            nxt = next(
                (t for t in sorted(scr.tabstops) if cur.col < t <= ceil),
                ceil,
            )
            self._set_cursor(scr, cur.row, nxt, pending_wrap=False)
        elif byte in (0x0A, 0x0B, 0x0C):  # LF, VT, FF
            self._linefeed(
                scr,
                carriage_return=self._modes.is_set(Mode.LNM),
            )
        elif byte == 0x0D:  # CR
            floor = left if cur.col >= left else 0
            self._set_cursor(scr, cur.row, floor, pending_wrap=False)

    def _save_cursor(self) -> None:
        scr = self._screens.active
        saved = _SavedCursor(
            row=scr.cursor.row,
            col=scr.cursor.col,
            pending_wrap=scr.cursor.pending_wrap,
            style=self._style,
            decom=self._modes.is_set(Mode.DECOM),
        )
        if self._screens.active_is_alt:
            self._saved_alt = saved
        else:
            self._saved_primary = saved

    def _restore_cursor(self) -> None:
        scr = self._screens.active
        saved = self._saved_alt if self._screens.active_is_alt else self._saved_primary
        self._style = saved.style
        self._sync_pen_cache()
        self._modes.set_mode(Mode.DECOM, enabled=saved.decom)
        self._set_cursor(
            scr,
            saved.row,
            saved.col,
            pending_wrap=saved.pending_wrap,
        )

    def on_esc(self, intermediates: bytes, final: int) -> None:
        """Execute a two- or multi-byte ``ESC`` sequence."""
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
                scr.tabstops.add(scr.cursor.col)
            elif final == 0x4D:  # ESC M RI
                top, bottom, left, right = self._effective_margins(scr)
                cur = scr.cursor
                if cur.row == top and left <= cur.col <= right:
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
                elif cur.row > 0:
                    self._set_cursor(scr, cur.row - 1, cur.col, pending_wrap=False)
            elif final == 0x63:  # ESC c RIS
                self.reset(soft=False)
        elif intermediates == b"#" and final == 0x38:  # DECALN
            for r in scr._rows:
                for c in range(scr.cols):
                    r.write_narrow(c, "E", 0, None)

    def _apply_mode(self, code: int, *, private: bool, enabled: bool) -> None:
        raw = (DEC_PRIVATE_MASK | code) if private else code
        try:
            mode = Mode(raw)
        except ValueError:
            return
        was_set = self._modes.is_set(mode)
        self._modes.set_mode(mode, enabled=enabled)
        scr = self._screens.active

        if mode == Mode.DECTCEM:
            c = scr.cursor
            scr.cursor = Cursor(
                row=c.row,
                col=c.col,
                visible=enabled,
                shape=c.shape,
                blinking=c.blinking,
                pending_wrap=c.pending_wrap,
            )
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
        elif mode in (Mode.ALT_SCREEN_47, Mode.ALT_SCREEN_1047, Mode.ALT_SCREEN):
            if enabled and not self._screens.active_is_alt:
                if mode == Mode.ALT_SCREEN:
                    self._save_cursor()
                self._screens.alternate.reset()
                self._screens.active_is_alt = True
            elif not enabled and self._screens.active_is_alt:
                if mode in (Mode.ALT_SCREEN_1047, Mode.ALT_SCREEN):
                    self._screens.alternate.reset()
                self._screens.active_is_alt = False
                if mode == Mode.ALT_SCREEN:
                    self._restore_cursor()
        elif mode == Mode.SYNCHRONIZED_OUTPUT and enabled != was_set:
            if enabled:
                self._held_snapshot = self.snapshot()
                self._step_event |= StopOn.SYNC_START
            else:
                self._held_snapshot = None
                self._step_event |= StopOn.SYNC_END
            if self._effects.on_render_hold is not None:
                self._effects.on_render_hold(enabled)

    def on_csi(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
    ) -> None:
        """Execute a ``CSI`` control sequence."""
        scr = self._screens.active
        cur = scr.cursor
        top, bottom, left, right = self._effective_margins(scr)

        # Device Attributes DA2: CSI > c
        if prefix == ord(">") and not intermediates and final == ord("c"):
            self._emit_reply(b"\x1b[>1;10;0c")
            return

        # DECRQM: CSI ? Ps $ p or CSI Ps $ p
        if intermediates == b"$" and final == ord("p"):
            private = prefix == ord("?")
            code = params.raw_or_default(0, 0)
            pm = int(self._modes.decrpm(code, private=private))
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
            scr.cursor = Cursor(
                row=cur.row,
                col=cur.col,
                visible=cur.visible,
                shape=shape,
                blinking=blinking,
                pending_wrap=cur.pending_wrap,
            )
            return

        if intermediates:
            return

        if prefix == ord("?"):
            if final in (ord("h"), ord("l")):
                enable = final == ord("h")
                for val in params.values:
                    self._apply_mode(val, private=True, enabled=enable)
            return

        if prefix != 0:
            return

        ch_final = chr(final)
        if ch_final == "m":
            self._style = parse_sgr(params, self._style)
            self._sync_pen_cache()
        elif ch_final in ("h", "l"):
            enable = ch_final == "h"
            for val in params.values:
                self._apply_mode(val, private=False, enabled=enable)
        elif ch_final == "A":  # CUU
            n = params.get(0, 1)
            floor = top if cur.row >= top else 0
            self._set_cursor(scr, max(floor, cur.row - n), cur.col)
        elif ch_final == "B":  # CUD
            n = params.get(0, 1)
            ceil = bottom if cur.row <= bottom else scr.rows - 1
            self._set_cursor(scr, min(ceil, cur.row + n), cur.col)
        elif ch_final == "C":  # CUF
            n = params.get(0, 1)
            ceil = right if cur.col <= right else scr.cols - 1
            self._set_cursor(scr, cur.row, min(ceil, cur.col + n))
        elif ch_final == "D":  # CUB
            n = params.get(0, 1)
            floor = left if cur.col >= left else 0
            self._set_cursor(scr, cur.row, max(floor, cur.col - n))
        elif ch_final == "E":  # CNL
            n = params.get(0, 1)
            ceil = bottom if cur.row <= bottom else scr.rows - 1
            self._set_cursor(scr, min(ceil, cur.row + n), left)
        elif ch_final == "F":  # CPL
            n = params.get(0, 1)
            floor = top if cur.row >= top else 0
            self._set_cursor(scr, max(floor, cur.row - n), left)
        elif ch_final in ("G", "`"):  # CHA / HPA
            col_1 = params.get(0, 1)
            if self._modes.is_set(Mode.DECOM):
                target_c = min(right, left + col_1 - 1)
            else:
                target_c = min(scr.cols - 1, col_1 - 1)
            self._set_cursor(scr, cur.row, max(0, target_c))
        elif ch_final == "d":  # VPA
            row_1 = params.get(0, 1)
            if self._modes.is_set(Mode.DECOM):
                target_r = min(bottom, top + row_1 - 1)
            else:
                target_r = min(scr.rows - 1, row_1 - 1)
            self._set_cursor(scr, max(0, target_r), cur.col)
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
            mode = params.raw_or_default(0, 0)
            bce_w, bce_e = self._bce_pair()
            if mode == 0:
                scr._rows[cur.row].clear_range(cur.col, scr.cols, bce_w, bce_e)
                for r in range(cur.row + 1, scr.rows):
                    scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e)
                self._notify_erase(
                    Rect(cur.row, cur.col, 1, scr.cols - cur.col),
                    EraseCause.ED,
                )
                if cur.row + 1 < scr.rows:
                    self._notify_erase(
                        Rect(cur.row + 1, 0, scr.rows - cur.row - 1, scr.cols),
                        EraseCause.ED,
                    )
            elif mode == 1:
                for r in range(cur.row):
                    scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e)
                scr._rows[cur.row].clear_range(0, cur.col + 1, bce_w, bce_e)
                if cur.row > 0:
                    self._notify_erase(
                        Rect(0, 0, cur.row, scr.cols),
                        EraseCause.ED,
                    )
                self._notify_erase(
                    Rect(cur.row, 0, 1, cur.col + 1),
                    EraseCause.ED,
                )
            elif mode in (2, 3):
                for r in range(scr.rows):
                    scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e)
                if mode == 3:
                    scr.history.clear()
                self._notify_erase(
                    Rect(0, 0, scr.rows, scr.cols),
                    EraseCause.ED,
                )
            self._set_cursor(scr, cur.row, cur.col, pending_wrap=False)
        elif ch_final == "K":  # EL
            mode = params.raw_or_default(0, 0)
            bce_w, bce_e = self._bce_pair()
            if mode == 0:
                scr._rows[cur.row].clear_range(cur.col, scr.cols, bce_w, bce_e)
                self._notify_erase(
                    Rect(cur.row, cur.col, 1, scr.cols - cur.col),
                    EraseCause.EL,
                )
            elif mode == 1:
                scr._rows[cur.row].clear_range(0, cur.col + 1, bce_w, bce_e)
                self._notify_erase(
                    Rect(cur.row, 0, 1, cur.col + 1),
                    EraseCause.EL,
                )
            elif mode == 2:
                scr._rows[cur.row].clear_range(0, scr.cols, bce_w, bce_e)
                self._notify_erase(
                    Rect(cur.row, 0, 1, scr.cols),
                    EraseCause.EL,
                )
            self._set_cursor(scr, cur.row, cur.col, pending_wrap=False)
        elif ch_final == "X":  # ECH
            n = min(scr.cols - cur.col, params.get(0, 1))
            bce_w, bce_e = self._bce_pair()
            scr._rows[cur.row].clear_range(cur.col, cur.col + n, bce_w, bce_e)
            self._notify_erase(Rect(cur.row, cur.col, 1, n), EraseCause.ECH)
        elif ch_final == "@":  # ICH
            n = params.get(0, 1)
            bce_w, _ = self._bce_pair()
            scr._rows[cur.row].insert_blanks(
                cur.col,
                n,
                left=left,
                right=right,
                bce_word=bce_w,
            )
        elif ch_final == "P":  # DCH
            n = params.get(0, 1)
            bce_w, _ = self._bce_pair()
            scr._rows[cur.row].delete_cells(
                cur.col,
                n,
                left=left,
                right=right,
                bce_word=bce_w,
            )
        elif ch_final == "L":  # IL
            if top <= cur.row <= bottom and left <= cur.col <= right:
                n = params.get(0, 1)
                bce_w, bce_e = self._bce_pair()
                scr.scroll_down(
                    n,
                    top=cur.row,
                    bottom=bottom,
                    left=left,
                    right=right,
                    bce_word=bce_w,
                    bce_ext=bce_e,
                )
                self._set_cursor(scr, cur.row, left)
        elif ch_final == "M":  # DL
            if top <= cur.row <= bottom and left <= cur.col <= right:
                n = params.get(0, 1)
                bce_w, bce_e = self._bce_pair()
                scr.scroll_up(
                    n,
                    top=cur.row,
                    bottom=bottom,
                    left=left,
                    right=right,
                    save_history=False,
                    bce_word=bce_w,
                    bce_ext=bce_e,
                )
                self._set_cursor(scr, cur.row, left)
        elif ch_final == "S":  # SU
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
        elif ch_final == "T":  # SD
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
        elif ch_final == "b":  # REP
            if self._last_printed:
                count = min(_MAX_REP_COUNT, params.get(0, 1))
                self.on_print(self._last_printed * count)
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
                scr.tabstops.discard(cur.col)
            elif mode == 3:
                scr.tabstops.clear()
        elif ch_final == "c":  # DA1
            if params.raw_or_default(0, 0) == 0:
                self._emit_reply(b"\x1b[?62;22c")
        elif ch_final == "n":  # DSR
            ps = params.raw_or_default(0, 0)
            if ps == 5:
                self._emit_reply(b"\x1b[0n")
            elif ps == 6:
                r_out = (
                    (cur.row - top + 1)
                    if self._modes.is_set(Mode.DECOM)
                    else (cur.row + 1)
                )
                c_out = (
                    (cur.col - left + 1)
                    if self._modes.is_set(Mode.DECOM)
                    else (cur.col + 1)
                )
                self._emit_reply(f"\x1b[{r_out};{c_out}R".encode("ascii"))
        elif (
            ch_final == "t"
            and params.raw_or_default(0, 0) == 21
            and self._effects.allow_title_report
        ):
            safe_title = "".join(
                ch for ch in self._title if " " <= ch <= "~" and ch != "\\"
            )
            self._emit_reply(f"\x1b]l{safe_title}\x1b\\".encode("ascii"))

    def on_osc(self, code: int, data: bytes) -> None:
        """Execute an ``OSC`` Operating System Command."""
        scr = self._screens.active
        text = data.decode("utf-8", errors="replace")
        if code in (0, 2):
            self._title = text
            if self._effects.on_title is not None:
                self._effects.on_title(text)
        elif code == 8:
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
        elif code == 133:
            parts = text.split(";")
            kind = parts[0][:1] if parts and parts[0] else ""
            if kind in ("A", "B", "C", "D"):
                exit_code: int | None = None
                if kind == "D" and len(parts) >= 2 and parts[1].lstrip("-").isdigit():
                    exit_code = int(parts[1])
                zone = PromptZone(
                    kind=kind,
                    row=scr.cursor.row,
                    col=scr.cursor.col,
                    exit_code=exit_code,
                )
                self._prompt_zones.append(zone)
                self._step_event |= StopOn.PROMPT_MARK
                if self._effects.on_osc_133 is not None:
                    self._effects.on_osc_133(kind, zone)
        elif code == 7501:
            parts = text.split(";", 1)
            val = parts[0]
            prog: int | None = None
            if len(parts) == 2 and parts[1].isdigit():
                prog = min(100, int(parts[1]))
            status = ProgramStatus(value=val, progress=prog)
            self._program_status = status
            if self._effects.on_osc_7501 is not None:
                self._effects.on_osc_7501(status)

    def on_dcs(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
        data: bytes,
    ) -> None:
        """Handle ``DCS`` sequences such as ``SIXEL`` (``DCS ... q``)."""
        if (
            prefix == 0
            and not intermediates
            and final == ord("q")
            and self._effects.on_sixel is not None
        ):
            cur = self._screens.active.cursor
            self._effects.on_sixel(
                SixelData(
                    row=cur.row,
                    col=cur.col,
                    params=params.values,
                    payload=data,
                ),
            )

    def on_apc(self, data: bytes) -> None:
        """Handle ``APC`` sequences such as Kitty graphics (``APC G ...``)."""
        if data.startswith(b"G") and self._effects.on_kitty_graphics is not None:
            body = data[1:]
            sep = body.find(b";")
            ctrl_bytes = body if sep == -1 else body[:sep]
            payload = b"" if sep == -1 else body[sep + 1 :]
            control: dict[str, str] = {}
            for pair in ctrl_bytes.decode("ascii", errors="ignore").split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    control[k] = v
            cur = self._screens.active.cursor
            self._effects.on_kitty_graphics(
                KittyCommand(
                    row=cur.row,
                    col=cur.col,
                    control=control,
                    payload=payload,
                ),
            )


__all__ = [
    "Effects",
    "Formatter",
    "KittyCommand",
    "OutputFormat",
    "ProgramStatus",
    "SixelData",
    "Snapshot",
    "StepResult",
    "StopOn",
    "Terminal",
]
