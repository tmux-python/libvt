"""Sans-I/O virtual terminal state machine, immutable snapshots, and formatters."""

from __future__ import annotations

import base64
import binascii
import collections
import collections.abc
import enum
import html
import os
import re
import types
import typing as t
import urllib.parse
from dataclasses import dataclass

from libvt._grid import (
    _EMPTY_FROZEN_HISTORY,
    _PEN_BASE_MASK,
    _PEN_BG_MASK,
    FrozenRow,
    _CellExt,
    _FrozenHistory,
    _Row,
    bump_row_gen,
    current_row_gen,
    make_cell_ext,
    pack_pen_base,
    raw_width_bits,
    unpack_style,
)
from libvt._ucd import (
    CLUSTER_NONE,
    GCB_PREPEND,
    SAFE_NARROW_BMP_RE,
    WidthRole,
    char_width,
    init_cluster_state,
    sanitize_terminal_text,
    step_cluster,
    ucd_lookup,
)
from libvt.config import (
    DEFAULT_LIMITS,
    DEFAULT_OSC7_HOSTS,
    DEFAULT_POLICY,
    DEFAULT_PROFILE,
    TMUX_CAPTURE_POLICY,
    TMUX_PROFILE,
    Charset,
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
    Passthrough,
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
    WindowRenamed,
    WorkingDirectoryChanged,
)
from libvt.exc import ProtocolError
from libvt.graphics import ImageMeta, ImagePlacement
from libvt.input import InputEvent, KittyKeyboardFlags, encode_input
from libvt.modes import (
    ALT_SCREEN_MODES,
    DEC_PRIVATE_MASK,
    TRANSIENT_SNAPSHOT_MODES,
    Mode,
    ModeState,
)
from libvt.parser import CsiParams, Parser, ParserEncoding, ParserState, parse_sgr
from libvt.screen import (
    Cell,
    Cursor,
    CursorShape,
    RegionView,
    RowView,
    Screen,
    ScreenSet,
    ScrollRegion,
    _HistoryView,
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
    Style,
    UnderlineStyle,
    _resolve_color_rgb,
)

if t.TYPE_CHECKING:
    from libvt.tmux import CaptureInput, TmuxPaneMetadata

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
_PRINT_ASCII_RE = re.compile(rb"[\x20-\x7e]+")
_STR_BODY_UTF8_RE = re.compile(rb"[^\x07\x18\x1a\x1b\x7f]+")
_STR_BODY_LATIN1_RE = re.compile(rb"[^\x07\x18\x1a\x1b\x7f\x80-\x9f]+")

_NEXT_TERMINAL_ID: int = 0


def _alloc_terminal_id() -> int:
    global _NEXT_TERMINAL_ID
    _NEXT_TERMINAL_ID += 1
    return _NEXT_TERMINAL_ID


type SyncView = t.Literal["working", "committed"]


class ResyncReason(enum.StrEnum):
    """Reason :meth:`Terminal.changes_since` emitted a full resynchronization."""

    NONE = "none"
    INITIAL = "initial"
    TERMINAL_MISMATCH = "terminal_mismatch"
    EPOCH_CHANGED = "epoch_changed"
    HISTORY_EVICTED = "history_evicted"


@dataclass(frozen=True, slots=True)
class SyncToken:
    """Opaque monotonic watermark passed to :meth:`Terminal.changes_since`.

    Attributes
    ----------
    terminal_id : int
        Identity cookie of the originating :class:`Terminal`.
    view : SyncView
        Tracked view (``"committed"`` or ``"working"``).
    epoch : int
        Structural epoch incremented on ``resize()``, ``RIS``, and ``?1049``
        alternate-screen transitions.
    revision : int
        Monotonic row-content mutation watermark.
    scrolled_total : int
        Cumulative primary-screen lines scrolled into history.
    evicted_total : int
        Cumulative primary-screen history lines evicted by ``maxlen``.
    history_clear_at : int
        ``scrolled_total`` value at the most recent ``ED 3`` history clear.
    history_clear_seq : int
        Monotonic counter of ``ED 3`` history clears.
    cursor : Cursor
        Cursor state at watermark capture.
    modes : frozenset[Mode]
        Enabled terminal modes at watermark capture.
    title : str
        Window title at watermark capture.
    cwd : str
        Working directory at watermark capture.
    cwd_uri : str
        Raw ``OSC 7`` / ``OSC 9;9`` working directory URI at watermark capture.
    aux_revision : int
        Monotonic counter of prompt-zone, program-status, and window-metadata
        updates.
    graphics_revision : int
        Monotonic graphics placement revision on the active screen.
    """

    terminal_id: int
    view: SyncView
    epoch: int
    revision: int
    scrolled_total: int
    evicted_total: int
    history_clear_at: int
    history_clear_seq: int
    cursor: Cursor
    modes: frozenset[Mode]
    title: str
    cwd: str
    cwd_uri: str = ""
    aux_revision: int = 0
    graphics_revision: int = 0


@dataclass(frozen=True, slots=True)
class ScreenDelta:
    """Multi-consumer incremental screen update from :meth:`Terminal.changes_since`.

    Attributes
    ----------
    token : SyncToken
        New watermark representing *snapshot*.
    from_token : SyncToken | None
        Previous watermark supplied by the caller.
    resync_reason : ResyncReason
        ``ResyncReason.NONE`` for an incremental delta, or the reason a full
        resynchronization was required.
    viewport_scroll_up : int
        Count of rows the primary viewport scrolled up into history since
        *from_token* (``0`` on full resync or non-history scroll).
    dirty_rows : tuple[int, ...]
        Sorted zero-based viewport row indices modified after shifting the
        previous viewport upward by :attr:`viewport_scroll_up`.
    new_history : collections.abc.Sequence[RowView]
        Newly sealed scrollback rows appended since *from_token*, ordered
        oldest to newest.
    history_evicted : int
        Count of oldest scrollback rows evicted by scrollback capacity since
        *from_token* when :attr:`history_cleared` is ``False``.
    history_cleared : bool
        ``True`` when ``ED 3`` cleared scrollback since *from_token*.
    cursor_changed : bool
        ``True`` when cursor position, visibility, shape, or ``pending_wrap``
        changed.
    modes_changed : bool
        ``True`` when enabled ANSI/DEC modes changed.
    metadata_changed : bool
        ``True`` when ``title`` or ``cwd`` changed.
    graphics_changed : bool
        ``True`` when ``SIXEL`` or Kitty placements mutated.
    snapshot : Snapshot
        Immutable target :class:`Snapshot` corresponding to :attr:`token`.
    """

    token: SyncToken
    from_token: SyncToken | None
    resync_reason: ResyncReason
    viewport_scroll_up: int
    dirty_rows: tuple[int, ...]
    new_history: collections.abc.Sequence[RowView]
    history_evicted: int
    history_cleared: bool
    cursor_changed: bool
    modes_changed: bool
    metadata_changed: bool
    graphics_changed: bool
    snapshot: Snapshot

    @property
    def full_resync(self) -> bool:
        """Return ``True`` when the consumer must replace cached state."""
        return self.resync_reason is not ResyncReason.NONE

    @property
    def has_changes(self) -> bool:
        """Return ``True`` when any grid, history, cursor, mode, or metadata changed."""
        return (
            self.full_resync
            or self.viewport_scroll_up > 0
            or bool(self.dirty_rows)
            or bool(self.new_history)
            or self.history_evicted > 0
            or self.history_cleared
            or self.cursor_changed
            or self.modes_changed
            or self.metadata_changed
            or self.graphics_changed
        )

    def apply_lines(self, history: list[str], viewport: list[str]) -> None:
        """Apply this delta in place to mutable *history* and *viewport* line lists.

        Parameters
        ----------
        history : list[str]
            Cached scrollback text lines maintained by the consumer.
        viewport : list[str]
            Cached visible-viewport text lines maintained by the consumer.
        """
        if self.full_resync or len(viewport) != self.snapshot.rows:
            history[:] = [r.text for r in self.snapshot.history]
            viewport[:] = list(self.snapshot.lines)
            return
        if self.history_cleared:
            history.clear()
        elif self.history_evicted > 0:
            del history[: self.history_evicted]
        if self.new_history:
            history.extend(r.text for r in self.new_history)
        if self.viewport_scroll_up > 0:
            shift = min(len(viewport), self.viewport_scroll_up)
            del viewport[:shift]
            viewport.extend([""] * shift)
        for r_idx in self.dirty_rows:
            viewport[r_idx] = self.snapshot[r_idx].text

    def apply_rows(self, history: list[RowView], viewport: list[RowView]) -> None:
        """Apply this delta in place to mutable *history* and *viewport* row views.

        Parameters
        ----------
        history : list[RowView]
            Cached scrollback :class:`~libvt.screen.RowView` entries.
        viewport : list[RowView]
            Cached visible :class:`~libvt.screen.RowView` entries.
        """
        if self.full_resync or len(viewport) != self.snapshot.rows:
            history[:] = list(self.snapshot.history)
            viewport[:] = [self.snapshot[r] for r in range(self.snapshot.rows)]
            return
        if self.history_cleared:
            history.clear()
        elif self.history_evicted > 0:
            del history[: self.history_evicted]
        if self.new_history:
            history.extend(self.new_history)
        if self.viewport_scroll_up > 0:
            shift = min(len(viewport), self.viewport_scroll_up)
            del viewport[:shift]
            base = len(viewport)
            viewport.extend([self.snapshot[base + i] for i in range(shift)])
        for r_idx in self.dirty_rows:
            viewport[r_idx] = self.snapshot[r_idx]


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
    g_sets : tuple[Charset, Charset, Charset, Charset]
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
    g_sets: tuple[Charset, Charset, Charset, Charset] = ("B", "B", "B", "B")
    gl: int = 0
    protected: bool = False


class Snapshot:
    """Immutable point-in-time capture of terminal screen, history, and metadata.

    Attributes
    ----------
    cursor : Cursor
        Zero-based cursor position, shape, visibility, and ``pending_wrap``.
    title : str
        Window title set via ``OSC 0`` / ``OSC 2``.
    cwd : str
        Local working directory accepted under ``Policy.osc7_hosts``.
    cwd_uri : str
        Raw ``OSC 7`` / ``OSC 9;9`` working directory URI.
    modes : frozenset[Mode]
        Enabled ANSI and DEC private modes.
    active_is_alt : bool
        ``True`` when the alternate screen buffer is active.
    prompt_zones : tuple[PromptZone, ...]
        Recorded primary-screen ``OSC 133`` semantic prompt zones (empty when
        :attr:`active_is_alt` is ``True``).
    program_status : ProgramStatus | None
        Highest-urgency active ``OSC 7501`` / ``OSC 9;4`` status record.
    program_statuses : collections.abc.Mapping[str, ProgramStatus]
        Read-only mapping of active ``OSC 7501`` task records keyed by ``id``.
    generation : int
        Terminal mutation generation when this snapshot was captured.
    images : collections.abc.Mapping[int, ImageMeta]
        Read-only mapping of stored ``SIXEL`` / Kitty :class:`ImageMeta` items.
    placements : tuple[ImagePlacement, ...]
        Immutable active + scrollback :class:`ImagePlacement` records.
    """

    __slots__ = (
        "_cols",
        "_evicted_total",
        "_frozen",
        "_history",
        "_rows",
        "_scrolled_total",
        "active_is_alt",
        "cursor",
        "cwd",
        "cwd_uri",
        "generation",
        "images",
        "modes",
        "placements",
        "program_status",
        "program_statuses",
        "prompt_zones",
        "title",
    )

    def __init__(
        self,
        *,
        rows: tuple[_Row, ...],
        history: _FrozenHistory | collections.abc.Sequence[FrozenRow] = (
            _EMPTY_FROZEN_HISTORY
        ),
        cols: int,
        cursor: Cursor,
        title: str,
        cwd: str,
        cwd_uri: str = "",
        modes: frozenset[Mode],
        active_is_alt: bool,
        prompt_zones: tuple[PromptZone, ...],
        program_status: ProgramStatus | None,
        program_statuses: collections.abc.Mapping[str, ProgramStatus],
        generation: int,
        images: collections.abc.Mapping[int, ImageMeta] | None = None,
        placements: tuple[ImagePlacement, ...] = (),
        scrolled_total: int | None = None,
        evicted_total: int | None = None,
    ) -> None:
        object.__setattr__(self, "_frozen", False)
        self._rows = rows
        hist_obj = _FrozenHistory.from_sequence(history)
        self._history: _FrozenHistory = hist_obj
        self._cols = cols
        eff_scrolled = (
            (0 if active_is_alt else len(hist_obj))
            if scrolled_total is None
            else scrolled_total
        )
        self._scrolled_total = eff_scrolled
        self._evicted_total = (
            (0 if active_is_alt else max(0, eff_scrolled - len(hist_obj)))
            if evicted_total is None
            else evicted_total
        )
        self.cursor = cursor
        self.title = title
        self.cwd = cwd
        self.cwd_uri = cwd_uri
        self.modes = modes
        self.active_is_alt = active_is_alt
        self.prompt_zones = prompt_zones
        self.program_status = program_status
        self.program_statuses: collections.abc.Mapping[str, ProgramStatus] = (
            types.MappingProxyType(dict(program_statuses))
        )
        self.generation = generation
        self.images: collections.abc.Mapping[int, ImageMeta] = types.MappingProxyType(
            dict(images) if images is not None else {}
        )
        self.placements = placements
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: object) -> None:
        """Reject attribute mutation once snapshot initialization completes."""
        if getattr(self, "_frozen", False):
            msg = f"Snapshot is immutable; cannot assign {name!r}"
            raise AttributeError(msg)
        object.__setattr__(self, name, value)

    @classmethod
    def from_tmux_capture(
        cls,
        captured: CaptureInput,
        *,
        metadata: TmuxPaneMetadata | str | None = None,
        rows: int = 24,
        cols: int = 80,
        scrollback: int | None = None,
        flagged: bool = False,
        joined_capture: CaptureInput | None = None,
        saved_primary_capture: CaptureInput | None = None,
        saved_primary_flagged: bool | None = None,
        profile: Profile = TMUX_PROFILE,
        policy: Policy = TMUX_CAPTURE_POLICY,
        limits: Limits = DEFAULT_LIMITS,
    ) -> Snapshot:
        """Decode ``tmux capture-pane`` output directly into a :class:`Snapshot`."""
        from libvt.tmux import decode_tmux_capture

        return decode_tmux_capture(
            captured,
            metadata=metadata,
            rows=rows,
            cols=cols,
            scrollback=scrollback,
            flagged=flagged,
            joined_capture=joined_capture,
            saved_primary_capture=saved_primary_capture,
            saved_primary_flagged=saved_primary_flagged,
            profile=profile,
            policy=policy,
            limits=limits,
        ).snapshot

    @property
    def rows(self) -> int:
        """Return snapshot height in rows."""
        return len(self._rows)

    @property
    def cols(self) -> int:
        """Return snapshot width in columns."""
        return self._cols

    @property
    def scrolled_total(self) -> int:
        """Return monotonic count of rows scrolled off the active screen's top."""
        return self._scrolled_total

    @property
    def evicted_total(self) -> int:
        """Return count of scrollback rows evicted from the active screen."""
        return self._evicted_total

    def viewport_row(self, abs_row: int) -> int | None:
        """Return zero-based viewport row for *abs_row*, or ``None``.

        Parameters
        ----------
        abs_row : int
            Monotonic absolute row coordinate (e.g.
            :attr:`~libvt.events.PromptZone.abs_row` or
            :attr:`~libvt.graphics.ImagePlacement.abs_row`).

        Returns
        -------
        int | None
            ``0 <= row < rows`` when *abs_row* is visible in this snapshot's
            active viewport, else ``None``.
        """
        if isinstance(abs_row, bool) or not isinstance(abs_row, int):
            msg = f"abs_row must be int, got {type(abs_row).__name__}"
            raise TypeError(msg)
        rel = abs_row - self._scrolled_total
        if 0 <= rel < len(self._rows):
            return rel
        return None

    def at_abs(self, abs_row: int) -> RowView:
        """Return :class:`~libvt.screen.RowView` at *abs_row* in history or viewport.

        Parameters
        ----------
        abs_row : int
            Monotonic absolute row coordinate.

        Returns
        -------
        RowView
            Immutable view of the row from scrollback or the active viewport.

        Raises
        ------
        IndexError
            When *abs_row* has been evicted from history or is out of range.
        """
        if isinstance(abs_row, bool) or not isinstance(abs_row, int):
            msg = f"abs_row must be int, got {type(abs_row).__name__}"
            raise TypeError(msg)
        if self.active_is_alt:
            if 0 <= abs_row < len(self._rows):
                return RowView(self._rows[abs_row])
            msg = f"abs_row {abs_row} out of range for alternate screen"
            raise IndexError(msg)
        first_abs = self._scrolled_total - len(self._history)
        if abs_row < first_abs:
            msg = f"abs_row {abs_row} was evicted (earliest retained is {first_abs})"
            raise IndexError(msg)
        if abs_row < self._scrolled_total:
            return RowView(self._history.at(abs_row - first_abs))
        rel = abs_row - self._scrolled_total
        if rel < len(self._rows):
            return RowView(self._rows[rel])
        msg = f"abs_row {abs_row} out of range"
        raise IndexError(msg)

    @property
    def history(self) -> collections.abc.Sequence[RowView]:
        """Return ``O(1)`` immutable :class:`RowView` sequence over scrollback rows."""
        return _HistoryView(self._history)

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
        key: slice | tuple[int, slice] | tuple[slice, int] | tuple[slice, slice],
    ) -> RegionView: ...

    @t.overload
    def __getitem__(
        self,
        key: tuple[slice | int, slice | int],
    ) -> Cell | RegionView: ...

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
        profile: Profile = DEFAULT_PROFILE,
    ) -> str:
        """Render this snapshot using :class:`Formatter`."""
        return Formatter(
            output_format,
            policy=policy,
            profile=profile,
        ).format_snapshot(self)

    def to_dict(self) -> dict[str, t.Any]:
        """Serialize snapshot state into a deterministic JSON-compatible dict."""
        return {
            "rows": self.rows,
            "cols": self.cols,
            "scrolled_total": self.scrolled_total,
            "evicted_total": self.evicted_total,
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
            "cwd_uri": self.cwd_uri,
            "active_is_alt": self.active_is_alt,
            "modes": sorted(m.name for m in self.modes if m.name is not None),
            "program_status": (
                {
                    "state": self.program_status.state.value,
                    "id": self.program_status.id,
                    "progress": self.program_status.progress,
                    "kind": (
                        self.program_status.kind.value
                        if self.program_status.kind is not None
                        else None
                    ),
                    "app": self.program_status.app,
                    "effective_app": self.program_status.effective_app,
                    "title": self.program_status.title,
                    "msg": self.program_status.msg,
                }
                if self.program_status is not None
                else None
            ),
            "placements": [
                {
                    "protocol": p.protocol.value,
                    "image_id": p.image_id,
                    "placement_id": p.placement_id,
                    "abs_row": p.abs_row,
                    "viewport_row": self.viewport_row(p.abs_row),
                    "col": p.col,
                    "rows": p.rows,
                    "cols": p.cols,
                    "z_index": p.z_index,
                    "virtual": p.virtual,
                }
                for p in self.placements
            ],
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

    __slots__ = ("output_format", "policy", "profile")

    def __init__(
        self,
        output_format: OutputFormat = OutputFormat.PLAIN,
        *,
        policy: Policy = DEFAULT_POLICY,
        profile: Profile = DEFAULT_PROFILE,
    ) -> None:
        self.output_format = output_format
        self.policy = policy
        self.profile = profile

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
            limit = row.used if row.wrapped else row.text_used
            for c in range(limit):
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
            if not row.wrapped and row.used > limit:
                cur_col_pos = limit
                span_c = limit
                row_used = min(snapshot.cols, row.used)
                while span_c < row_used:
                    word = row.attr_at(span_c)
                    ext = row.ext_at(span_c)
                    style = unpack_style(word, ext)
                    link = ext.hyperlink if ext is not None else None
                    end_c = span_c + 1
                    while end_c < row_used:
                        nw = row.attr_at(end_c)
                        ne = row.ext_at(end_c)
                        if (
                            unpack_style(nw, ne) != style
                            or (ne.hyperlink if ne is not None else None) != link
                        ):
                            break
                        end_c += 1
                    if style != DEFAULT_STYLE or link is not None:
                        if cur_col_pos != span_c:
                            out.append(f"\x1b[{r_idx + 1};{span_c + 1}H")
                            cur_col_pos = span_c
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
                        if end_c == snapshot.cols:
                            out.append("\x1b[K")
                        else:
                            out.append(f"\x1b[{end_c - span_c}X")
                    span_c = end_c
        if cur_link is not None:
            out.append("\x1b]8;;\x1b\\")
        if cur_style != DEFAULT_STYLE:
            out.append("\x1b[0m")
        out.append(f"\x1b[{snapshot.cursor.row + 1};{snapshot.cursor.col + 1}H")
        return "".join(out)

    def _render_html(self, snapshot: Snapshot) -> str:
        lines: list[str] = ['<pre class="libvt-screen">']
        allow_unsafe = self.policy.allow_unsafe_uris
        def_fg = self.profile.fg_rgb
        def_bg = self.profile.bg_rgb
        ul_styles = {
            UnderlineStyle.DOUBLE: "double",
            UnderlineStyle.CURLY: "wavy",
            UnderlineStyle.DOTTED: "dotted",
            UnderlineStyle.DASHED: "dashed",
        }
        for row in snapshot._rows:
            view = RowView(row)
            chunks: list[str] = []
            for run in view.runs:
                escaped = html.escape(run.text)
                style = run.style
                css: list[str] = []
                if style.bold:
                    css.append("font-weight:bold")
                if style.dim:
                    css.append("opacity:0.65")
                if style.italic:
                    css.append("font-style:italic")
                if style.hidden:
                    css.append("visibility:hidden")
                if style.reverse:
                    fg_r, fg_g, fg_b = _resolve_color_rgb(style.bg, def_bg)
                    bg_r, bg_g, bg_b = _resolve_color_rgb(style.fg, def_fg)
                    css.append(f"color:rgb({fg_r},{fg_g},{fg_b})")
                    css.append(f"background-color:rgb({bg_r},{bg_g},{bg_b})")
                else:
                    if style.fg is not None:
                        fg_r, fg_g, fg_b = _resolve_color_rgb(style.fg, def_fg)
                        css.append(f"color:rgb({fg_r},{fg_g},{fg_b})")
                    if style.bg is not None:
                        bg_r, bg_g, bg_b = _resolve_color_rgb(style.bg, def_bg)
                        css.append(f"background-color:rgb({bg_r},{bg_g},{bg_b})")
                dec_lines: list[str] = []
                if style.underline != UnderlineStyle.NONE:
                    dec_lines.append("underline")
                if style.overline:
                    dec_lines.append("overline")
                if style.strikethrough:
                    dec_lines.append("line-through")
                if dec_lines:
                    css.append(f"text-decoration-line:{' '.join(dec_lines)}")
                ul_css = ul_styles.get(style.underline)
                if ul_css is not None:
                    css.append(f"text-decoration-style:{ul_css}")
                if style.underline_color is not None:
                    ur, ug, ub = _resolve_color_rgb(style.underline_color, def_fg)
                    css.append(f"text-decoration-color:rgb({ur},{ug},{ub})")
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

    def on_overflow(self, kind: str, limit: int) -> None:
        self._term._unhandled(kind, f"{kind.upper()} overflow (>{limit} bytes)")

    def on_window_rename(self, data: bytes) -> None:
        self._term._handle_window_rename(data)


def _event_byte_cost(event: Event) -> int:
    if isinstance(event, Passthrough):
        return 64 + len(event.data)
    if isinstance(event, SixelGraphic):
        return 64 + len(event.data.payload)
    if isinstance(event, KittyGraphic):
        return 64 + len(event.command.payload)
    if isinstance(event, ClipboardWrite):
        return 64 + len(event.text)
    return 64


def _parse_osc_rgb(spec: bytes) -> tuple[int, int, int] | None:
    text = spec.decode("ascii", errors="ignore").strip()
    if text.startswith("#") and len(text) == 7:
        try:
            return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
        except ValueError:
            return None
    if text.lower().startswith("rgb:"):
        parts = text[4:].split("/")
        if len(parts) != 3:
            return None
        vals: list[int] = []
        for comp in parts:
            if not (1 <= len(comp) <= 4):
                return None
            try:
                raw = int(comp, 16)
            except ValueError:
                return None
            scale = (1 << (4 * len(comp))) - 1
            vals.append((raw * 255) // scale)
        return (vals[0], vals[1], vals[2])
    return None


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
        encoding: ParserEncoding = "utf-8",
        newline_mode: bool = False,
    ) -> None:
        self._profile = profile
        self._policy = policy
        self._limits = limits
        self._encoding: ParserEncoding = encoding
        self._newline_mode: bool = newline_mode
        self._on_event = on_event
        self._on_pty_write = on_pty_write
        self._record_events = record_events
        self._recorder = recorder
        self._events: collections.deque[Event] = collections.deque()
        self._event_bytes: int = 0
        self._screens = ScreenSet(
            rows=rows,
            cols=cols,
            scrollback=scrollback,
            keyboard_stack_depth=limits.max_keyboard_stack,
            cell_height_px=profile.cell_height_px,
            max_images=limits.max_images,
            max_placements=limits.max_placements,
            max_image_dimension=limits.max_image_dimension,
        )
        self._modes = ModeState()
        if newline_mode:
            self._modes.set_mode(Mode.LNM, enabled=True)
        self._parser = Parser(
            encoding=encoding,
            tmux_window_rename=profile.tmux_window_rename,
            abort_dispatch_osc=policy.abort_dispatch_osc,
            limits=limits,
        )
        self._passthrough_parser: Parser | None = None
        self._sink = _TerminalSink(self)
        self._sgr_memo: dict[tuple[Style, CsiParams], Style] = {}
        self._style: Style = DEFAULT_STYLE
        self._protected: bool = False
        self._pen_word: int = 0
        self._hyperlink: Hyperlink | None = None
        self._cell_ext: _CellExt | None = None
        def_g: tuple[Charset, Charset, Charset, Charset] = (
            "B",
            profile.default_g1_charset,
            "B",
            "B",
        )
        self._g_sets: list[Charset] = list(def_g)
        self._gl: int = 0
        self._single_shift: int | None = None
        self._saved_primary = _SavedCursor(g_sets=def_g)
        self._saved_alt = _SavedCursor(g_sets=def_g)
        self._last_printed: str = ""
        self._last_printed_pos: tuple[int, int] | None = None
        self._cluster_state: int = CLUSTER_NONE
        self._cluster_len: int = 0
        self._pending_prepend: str = ""
        self._title: str = ""
        self._window_name: str = ""
        self._fg_rgb: tuple[int, int, int] = profile.fg_rgb
        self._bg_rgb: tuple[int, int, int] = profile.bg_rgb
        self._title_stack: list[str] = []
        self._cwd: str = ""
        self._cwd_uri: str = ""
        self._prompt_zones: collections.deque[PromptZone] = collections.deque(
            maxlen=limits.max_prompt_zones,
        )
        self._status_store = ProgramStatusStore(
            max_records=limits.max_status_records,
        )
        self._replies = bytearray()
        self._terminal_id: int = _alloc_terminal_id()
        self._generation: int = 0
        self._epoch: int = 0
        self._aux_revision: int = 0
        self._sync_generation: int = 0
        self._sync_hold_pending: bool = False
        self._cached_snapshot: Snapshot | None = None
        self._held_snapshot: Snapshot | None = None
        self._held_sync_token: SyncToken | None = None
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

    @classmethod
    def from_tmux_capture(
        cls,
        captured: CaptureInput,
        *,
        metadata: TmuxPaneMetadata | str | None = None,
        rows: int = 24,
        cols: int = 80,
        scrollback: int | None = None,
        flagged: bool = False,
        joined_capture: CaptureInput | None = None,
        saved_primary_capture: CaptureInput | None = None,
        saved_primary_flagged: bool | None = None,
        profile: Profile = TMUX_PROFILE,
        policy: Policy = TMUX_CAPTURE_POLICY,
        limits: Limits = DEFAULT_LIMITS,
    ) -> Terminal:
        """Reconstruct a live :class:`Terminal` from ``tmux capture-pane`` output."""
        from libvt.tmux import decode_tmux_capture

        return decode_tmux_capture(
            captured,
            metadata=metadata,
            rows=rows,
            cols=cols,
            scrollback=scrollback,
            flagged=flagged,
            joined_capture=joined_capture,
            saved_primary_capture=saved_primary_capture,
            saved_primary_flagged=saved_primary_flagged,
            profile=profile,
            policy=policy,
            limits=limits,
        ).terminal

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
    def history(self) -> collections.abc.Sequence[RowView]:
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
    def window_name(self) -> str:
        r"""Return ``tmux`` window name set by ``ESC k <name> ESC \``."""
        return self._window_name

    @property
    def cwd(self) -> str:
        """Return local working directory path accepted under ``Policy.osc7_hosts``."""
        return self._cwd

    @property
    def cwd_uri(self) -> str:
        """Return raw ``OSC 7`` / ``OSC 9;9`` working directory URI."""
        return self._cwd_uri

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
        """Return recorded primary-screen ``OSC 133`` semantic prompt zones."""
        return tuple(self._prompt_zones)

    @property
    def program_status(self) -> ProgramStatus | None:
        """Return highest-urgency active ``OSC 7501`` / ``OSC 9;4`` status."""
        return self._status_store.most_urgent

    @property
    def program_statuses(self) -> dict[str, ProgramStatus]:
        """Return all active ``OSC 7501`` task records keyed by ``id``."""
        return self._status_store.records

    @property
    def images(self) -> dict[int, ImageMeta]:
        """Return active screen's stored :class:`~libvt.graphics.ImageMeta` map."""
        return self._screens.active.images

    @property
    def placements(self) -> tuple[ImagePlacement, ...]:
        """Return active screen's :class:`~libvt.graphics.ImagePlacement` records."""
        return self._screens.active.placements

    def _tabstops_from_list(self, scr: Screen, stops: tuple[int, ...]) -> None:
        scr._tabstops = {c for c in stops if 0 <= c < scr.cols}
        scr._tabstops_sorted = sorted(scr._tabstops)

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

    def _invalidate_cache(self) -> None:
        if self._cached_snapshot is not None:
            self._cached_snapshot = None
            self._generation += 1

    def _build_snapshot(
        self,
        *,
        exclude_modes: frozenset[Mode] = frozenset(),
    ) -> Snapshot:
        scr = self._screens.active
        modes = self._modes.enabled_modes()
        if exclude_modes:
            modes = modes - exclude_modes
        active_alt = self._screens.active_is_alt
        return Snapshot(
            rows=tuple(r.clone() for r in scr._rows),
            history=self._screens.primary._history.freeze(),
            cols=scr.cols,
            cursor=scr.cursor,
            title=self._title,
            cwd=self._cwd,
            cwd_uri=self._cwd_uri,
            modes=modes,
            active_is_alt=active_alt,
            prompt_zones=() if active_alt else tuple(self._prompt_zones),
            program_status=self._status_store.most_urgent,
            program_statuses=self._status_store.records,
            generation=self._generation,
            images=scr.images,
            placements=scr.placements,
            scrolled_total=scr._scrolled_total,
            evicted_total=scr._evicted_total,
        )

    def _live_sync_token(
        self,
        *,
        view: SyncView,
        exclude_modes: frozenset[Mode] = frozenset(),
    ) -> SyncToken:
        scr = self._screens.active
        prim = self._screens.primary
        modes = self._modes.enabled_modes()
        if exclude_modes:
            modes = modes - exclude_modes
        gfx_rev = scr._gfx.revision if scr._gfx is not None else 0
        return SyncToken(
            terminal_id=self._terminal_id,
            view=view,
            epoch=self._epoch,
            revision=current_row_gen(),
            scrolled_total=prim._scrolled_total,
            evicted_total=prim._evicted_total,
            history_clear_at=prim._history_clear_at,
            history_clear_seq=prim._history_clear_seq,
            cursor=scr.cursor,
            modes=modes,
            title=self._title,
            cwd=self._cwd,
            cwd_uri=self._cwd_uri,
            aux_revision=self._aux_revision,
            graphics_revision=gfx_rev,
        )

    def _ensure_sync_snapshot(self) -> None:
        if self._sync_hold_pending and self._held_snapshot is None:
            self._held_snapshot = self._build_snapshot(
                exclude_modes=TRANSIENT_SNAPSHOT_MODES,
            )
            self._held_sync_token = self._live_sync_token(
                view="committed",
                exclude_modes=TRANSIENT_SNAPSHOT_MODES,
            )
            self._sync_hold_pending = False

    def sync_token(self, *, view: SyncView = "committed") -> SyncToken:
        """Return an immutable watermark token for :meth:`changes_since`.

        Parameters
        ----------
        view : SyncView
            ``"committed"`` (default, respecting ``?2026`` frame holds) or
            ``"working"`` (live in-progress buffer).

        Returns
        -------
        SyncToken
            Opaque watermark capturing current state without mutating the
            terminal.
        """
        if view == "committed":
            if self._sync_hold_pending:
                self._ensure_sync_snapshot()
            if self._held_sync_token is not None:
                return self._held_sync_token
        return self._live_sync_token(view=view)

    def changes_since(
        self,
        token: SyncToken | None = None,
        *,
        view: SyncView = "committed",
    ) -> ScreenDelta:
        """Compute incremental changes relative to *token* without mutating state.

        Parameters
        ----------
        token : SyncToken | None
            Previous watermark from :meth:`sync_token` or
            :attr:`ScreenDelta.token`; ``None`` requests an initial full sync.
        view : SyncView
            ``"committed"`` (default) or ``"working"``.

        Returns
        -------
        ScreenDelta
            Incremental row/scrollback delta or full resynchronization record.
        """
        cur_snap = self.committed if view == "committed" else self.snapshot()
        cur_tok = self.sync_token(view=view)

        def _full(reason: ResyncReason) -> ScreenDelta:
            return ScreenDelta(
                token=cur_tok,
                from_token=token,
                resync_reason=reason,
                viewport_scroll_up=0,
                dirty_rows=tuple(range(cur_snap.rows)),
                new_history=cur_snap.history,
                history_evicted=0,
                history_cleared=False,
                cursor_changed=True,
                modes_changed=True,
                metadata_changed=True,
                graphics_changed=bool(cur_snap.placements),
                snapshot=cur_snap,
            )

        if token is None:
            return _full(ResyncReason.INITIAL)
        if token.terminal_id != self._terminal_id or token.view != view:
            return _full(ResyncReason.TERMINAL_MISMATCH)
        if token.epoch != cur_tok.epoch:
            return _full(ResyncReason.EPOCH_CHANGED)

        if token == cur_tok:
            return ScreenDelta(
                token=cur_tok,
                from_token=token,
                resync_reason=ResyncReason.NONE,
                viewport_scroll_up=0,
                dirty_rows=(),
                new_history=(),
                history_evicted=0,
                history_cleared=False,
                cursor_changed=False,
                modes_changed=False,
                metadata_changed=False,
                graphics_changed=False,
                snapshot=cur_snap,
            )

        hist_cleared = cur_tok.history_clear_seq != token.history_clear_seq
        hist_len = len(cur_snap._history)
        zero_hist_cap = self._screens.primary._history.maxlen == 0 and hist_len == 0
        new_hist: collections.abc.Sequence[RowView]
        if hist_cleared:
            if zero_hist_cap:
                new_hist = ()
                evicted_cnt = 0
            else:
                post_clear = max(0, cur_tok.scrolled_total - cur_tok.history_clear_at)
                if post_clear > hist_len:
                    return _full(ResyncReason.HISTORY_EVICTED)
                new_hist = (
                    cur_snap.history[hist_len - post_clear :] if post_clear > 0 else ()
                )
                evicted_cnt = 0
        else:
            added = max(0, cur_tok.scrolled_total - token.scrolled_total)
            if zero_hist_cap:
                new_hist = ()
                evicted_cnt = 0
            else:
                if added > hist_len:
                    return _full(ResyncReason.HISTORY_EVICTED)
                new_hist = cur_snap.history[hist_len - added :] if added > 0 else ()
                evicted_cnt = max(0, cur_tok.evicted_total - token.evicted_total)

        v_scroll = (
            0
            if cur_snap.active_is_alt
            else max(0, cur_tok.scrolled_total - token.scrolled_total)
        )
        dirty = tuple(
            idx
            for idx, r in enumerate(cur_snap._rows)
            if r.content_gen > token.revision
        )
        return ScreenDelta(
            token=cur_tok,
            from_token=token,
            resync_reason=ResyncReason.NONE,
            viewport_scroll_up=v_scroll,
            dirty_rows=dirty,
            new_history=new_hist,
            history_evicted=evicted_cnt,
            history_cleared=hist_cleared,
            cursor_changed=cur_tok.cursor != token.cursor,
            modes_changed=cur_tok.modes != token.modes,
            metadata_changed=(
                cur_tok.title != token.title
                or cur_tok.cwd != token.cwd
                or cur_tok.cwd_uri != token.cwd_uri
                or cur_tok.aux_revision != token.aux_revision
            ),
            graphics_changed=cur_tok.graphics_revision != token.graphics_revision,
            snapshot=cur_snap,
        )

    def snapshot(self) -> Snapshot:
        """Capture an immutable :class:`Snapshot` of live terminal state."""
        if (
            self._cached_snapshot is not None
            and self._cached_snapshot.generation == self._generation
        ):
            return self._cached_snapshot
        snap = self._build_snapshot()
        self._cached_snapshot = snap
        return snap

    @t.overload
    def __getitem__(self, key: int) -> RowView: ...

    @t.overload
    def __getitem__(self, key: tuple[int, int]) -> Cell: ...

    @t.overload
    def __getitem__(
        self,
        key: slice | tuple[int, slice] | tuple[slice, int] | tuple[slice, slice],
    ) -> RegionView: ...

    @t.overload
    def __getitem__(
        self,
        key: tuple[slice | int, slice | int],
    ) -> Cell | RegionView: ...

    def __getitem__(
        self,
        key: int | slice | tuple[slice | int, slice | int],
    ) -> RowView | Cell | RegionView:
        """Index a row, single ``(row, col)`` cell, or 2D region view."""
        return self._screens.active[key]

    def _emit_event(self, event: Event) -> None:
        if self._record_events:
            max_ev = self._limits.max_events
            max_bytes = self._limits.max_event_bytes
            cost = _event_byte_cost(event)
            if max_ev > 0 and max_bytes > 0:
                while self._events and (
                    len(self._events) >= max_ev or self._event_bytes + cost > max_bytes
                ):
                    dropped = self._events.popleft()
                    self._event_bytes -= _event_byte_cost(dropped)
                if cost <= max_bytes:
                    self._events.append(event)
                    self._event_bytes += cost
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
        self._event_bytes = 0
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
        self._held_sync_token = None
        self._cached_snapshot = None
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
        self._before_mutate()
        if self._status_store.on_process_exit():
            self._aux_revision += 1
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
        self._cluster_state = CLUSTER_NONE
        self._cluster_len = 0
        self._pending_prepend = ""

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

    def resize(
        self,
        *,
        rows: int,
        cols: int,
        reflow: bool | None = None,
    ) -> None:
        """Resize terminal screens to ``(rows, cols)``."""
        eff_reflow = self._profile.reflow_on_resize if reflow is None else reflow
        self._before_mutate()
        self._clear_cluster_anchor()
        self._screens.resize(rows=rows, cols=cols, reflow=eff_reflow)
        if not self._modes.is_set(Mode.DECAWM):
            self._screens.primary._pending_wrap = False
            self._screens.alternate._pending_wrap = False
        if self._screens.active_is_alt:
            prim = self._screens.primary
            self._saved_primary = _SavedCursor(
                row=prim._cur_row,
                col=prim._cur_col,
                pending_wrap=prim._pending_wrap,
                style=self._saved_primary.style,
                decom=self._saved_primary.decom,
                g_sets=self._saved_primary.g_sets,
                gl=self._saved_primary.gl,
                protected=self._saved_primary.protected,
            )
        self._generation += 1
        self._epoch += 1
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
        def_g: tuple[Charset, Charset, Charset, Charset] = (
            "B",
            self._profile.default_g1_charset,
            "B",
            "B",
        )
        self._g_sets = list(def_g)
        self._gl = 0
        self._single_shift = None
        was_sync = self.synchronized
        was_alt = self._screens.active_is_alt
        self._modes = ModeState()
        if self._newline_mode:
            self._modes.set_mode(Mode.LNM, enabled=True)
        self._sync_hold_pending = False
        self._held_snapshot = None
        self._held_sync_token = None
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
            self._epoch += 1
            self._parser.reset()
            self._passthrough_parser = None
            self._screens.active_is_alt = False
            self._screens.primary.reset()
            self._screens.alternate.reset()
            self._saved_primary = _SavedCursor(g_sets=def_g)
            self._saved_alt = _SavedCursor(g_sets=def_g)
            self._title = ""
            self._window_name = ""
            self._fg_rgb = self._profile.fg_rgb
            self._bg_rgb = self._profile.bg_rgb
            self._title_stack.clear()
            self._cwd = ""
            self._cwd_uri = ""
            self._prompt_zones.clear()
            self._status_store.reset()
            self._aux_revision += 1
            self._notify_erase(
                Rect(0, 0, self.rows, self.cols),
                EraseCause.RESET,
            )
        self._cached_snapshot = None
        self._generation += 1
        if was_sync:
            self._emit_event(
                RenderHoldChanged(
                    active=False,
                    sync_generation=self._sync_generation,
                ),
            )

    def _before_mutate(self) -> None:
        if self._sync_hold_pending:
            self._ensure_sync_snapshot()
        self._invalidate_cache()

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
                ascii_m = _PRINT_ASCII_RE.match(mv, idx)
                if ascii_m is not None:
                    end = ascii_m.end()
                    span = mv[idx:end]
                    if self._recorder is not None:
                        self._recorder.record(span)
                    self._parser.feed_to(span, self._sink)
                    idx = end
                    self._generation += 1
                    continue

            # Bulk-advance interior payload bytes inside OSC / DCS / APC strings.
            if state in (
                ParserState.OSC_STRING,
                ParserState.DCS_PASSTHROUGH,
                ParserState.SOS_PM_APC_STRING,
            ):
                body_re = (
                    _STR_BODY_LATIN1_RE
                    if self._encoding == "latin-1"
                    else _STR_BODY_UTF8_RE
                )
                body_m = body_re.match(mv, idx)
                if body_m is not None:
                    end = body_m.end()
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
        self._before_mutate()
        scr._cur_row = max(0, min(scr.rows - 1, row))
        scr._cur_col = max(0, min(scr.cols - 1, col))
        scr._pending_wrap = pending_wrap
        if clear_cluster:
            self._clear_cluster_anchor()

    def _shift_prompt_zones_below(
        self,
        prev_scrolled: int,
        bottom: int,
        count: int,
    ) -> None:
        if not self._prompt_zones or count <= 0:
            return
        cutoff = prev_scrolled + bottom
        changed = False
        updated: list[PromptZone] = []
        for z in self._prompt_zones:
            if z.abs_row > cutoff:
                updated.append(
                    PromptZone(
                        kind=z.kind,
                        abs_row=z.abs_row + count,
                        col=z.col,
                        exit_code=z.exit_code,
                    ),
                )
                changed = True
            else:
                updated.append(z)
        if changed:
            self._prompt_zones.clear()
            self._prompt_zones.extend(updated)
            self._aux_revision += 1

    def _linefeed(self, scr: Screen, *, carriage_return: bool = False) -> None:
        self._before_mutate()
        top, bottom, left, right = self._effective_margins(scr)
        col = left if carriage_return else scr._cur_col
        keep_wrap = (
            (not carriage_return)
            and self._profile.pending_wrap_phantom_col
            and scr._pending_wrap
        )
        if scr._cur_row == bottom and left <= scr._cur_col <= right:
            prev_scrolled = scr._scrolled_total
            bce_word, bce_ext = self._bce_pair()
            save_hist = not self._screens.active_is_alt
            scr.scroll_up(
                1,
                top=top,
                bottom=bottom,
                left=left,
                right=right,
                save_history=save_hist,
                bce_word=bce_word,
                bce_ext=bce_ext,
            )
            hist_delta = scr._scrolled_total - prev_scrolled
            if (
                hist_delta > 0
                and top == 0
                and bottom < scr.rows - 1
                and left == 0
                and right == scr.cols - 1
            ):
                self._shift_prompt_zones_below(prev_scrolled, bottom, hist_delta)
            self._notify_erase(
                Rect(bottom, left, 1, right - left + 1),
                EraseCause.SCROLL,
            )
            self._set_cursor(scr, bottom, col, pending_wrap=keep_wrap)
        elif scr._cur_row < scr.rows - 1:
            self._set_cursor(scr, scr._cur_row + 1, col, pending_wrap=keep_wrap)
        else:
            self._set_cursor(scr, scr._cur_row, col, pending_wrap=keep_wrap)

    def _advance_rows(
        self,
        scr: Screen,
        steps: int,
        *,
        final_col: int,
    ) -> int:
        self._before_mutate()
        orig_row = scr._cur_row
        orig_col = scr._cur_col
        if steps <= 0:
            self._set_cursor(scr, orig_row, final_col, pending_wrap=False)
            return orig_row
        top, bottom, left, right = self._effective_margins(scr)
        if orig_col < left or orig_col > right or orig_row > bottom:
            new_r = min(scr.rows - 1, orig_row + steps)
            self._set_cursor(scr, new_r, final_col, pending_wrap=False)
            return orig_row
        to_bottom = min(steps, bottom - orig_row)
        scroll_cnt = steps - to_bottom
        if scroll_cnt <= 0:
            self._set_cursor(scr, orig_row + to_bottom, final_col, pending_wrap=False)
            return orig_row
        prev_scrolled = scr._scrolled_total
        bce_word, bce_ext = self._bce_pair()
        save_hist = not self._screens.active_is_alt
        scr.scroll_up(
            scroll_cnt,
            top=top,
            bottom=bottom,
            left=left,
            right=right,
            save_history=save_hist,
            bce_word=bce_word,
            bce_ext=bce_ext,
        )
        hist_delta = scr._scrolled_total - prev_scrolled
        if (
            hist_delta > 0
            and top == 0
            and bottom < scr.rows - 1
            and left == 0
            and right == scr.cols - 1
        ):
            self._shift_prompt_zones_below(prev_scrolled, bottom, hist_delta)
        erase_h = min(scroll_cnt, bottom - top + 1)
        self._notify_erase(
            Rect(bottom - erase_h + 1, left, erase_h, right - left + 1),
            EraseCause.SCROLL,
        )
        self._set_cursor(scr, bottom, final_col, pending_wrap=False)
        if orig_row < top:
            return orig_row
        return orig_row - scroll_cnt

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

    def _write_slice_run(
        self,
        scr: Screen,
        text: str,
        start: int,
        end: int,
        *,
        autowrap: bool,
        bottom: int,
        left: int,
        right: int,
        mode_2027: bool,
    ) -> None:
        pos = start
        sixel_pol = self._profile.sixel_erase_policy
        gfx = scr._gfx
        has_sixels = gfx is not None and gfx.has_sixels
        while pos < end:
            c_col = scr._cur_col
            max_col = right if c_col <= right else scr.cols - 1
            min_col = left if c_col >= left else 0
            if scr._pending_wrap and autowrap:
                prev_row = scr._cur_row
                can_advance = prev_row < scr.rows - 1 or (
                    prev_row == bottom and left <= c_col <= right
                )
                set_wrap = can_advance and min_col == 0 and c_col == scr.cols - 1
                if set_wrap and prev_row == 0 and bottom == 0:
                    scr._rows[0].wrapped = True
                    bump_row_gen(scr._rows[0])
                self._linefeed(scr)
                if set_wrap and not (prev_row == 0 and bottom == 0):
                    wrap_idx = prev_row - 1 if prev_row == bottom else prev_row
                    if 0 <= wrap_idx < scr.rows:
                        scr._rows[wrap_idx].wrapped = True
                        bump_row_gen(scr._rows[wrap_idx])
                self._set_cursor(scr, scr._cur_row, min_col, pending_wrap=False)
                c_col = scr._cur_col
            avail = max_col - c_col + 1
            rem = end - pos
            if not autowrap and rem > avail:
                seg = (
                    text[pos : pos + avail - 1] + text[end - 1]
                    if avail > 1
                    else text[end - 1]
                )
                consumed = rem
            else:
                seg = text[pos : min(end, pos + avail)]
                consumed = len(seg)
            seg_len = len(seg)
            scr.heal_before_edit(scr._cur_row, c_col, c_col + seg_len)
            scr._rows[scr._cur_row].write_ascii_slice(
                c_col,
                seg,
                self._pen_word,
                self._cell_ext,
            )
            if has_sixels and gfx is not None:
                gfx.erase_sixel_rect(
                    row=scr._cur_row,
                    col=c_col,
                    rows=1,
                    cols=seg_len,
                    scrolled_total=scr._scrolled_total,
                    policy=sixel_pol,
                )
            pos += consumed
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
            self._cluster_state = init_cluster_state(
                ord(seg[-1]),
                mode_2027=mode_2027,
            )
            self._cluster_len = 1

    def _handle_print(self, text: str) -> None:
        self._before_mutate()
        if self._single_shift is not None or self._g_sets[self._gl] != "B":
            text = self._map_charset(text)

        scr = self._screens.active
        autowrap = self._modes.is_set(Mode.DECAWM)
        insert_mode = self._modes.is_set(Mode.IRM)
        _, bottom, left, right = self._effective_margins(scr)

        mode_2027 = self._modes.is_set(Mode.GRAPHEME_CLUSTERING)
        total = len(text)
        if (
            not insert_mode
            and not self._pending_prepend
            and (not mode_2027 or ((self._cluster_state >> 1) & 0x0F) != GCB_PREPEND)
            and text.isascii()
        ):
            self._write_slice_run(
                scr,
                text,
                0,
                total,
                autowrap=autowrap,
                bottom=bottom,
                left=left,
                right=right,
                mode_2027=mode_2027,
            )
            return

        vs15_narrows = self._profile.vs15_narrows_wide_emoji
        vs16_wide = self._profile.vs16_always_wide
        max_cluster = self._limits.max_grapheme_codepoints
        sixel_pol = self._profile.sixel_erase_policy

        t_idx = 0
        while t_idx < total:
            if (
                not insert_mode
                and not self._pending_prepend
                and (
                    not mode_2027 or ((self._cluster_state >> 1) & 0x0F) != GCB_PREPEND
                )
            ):
                safe_m = SAFE_NARROW_BMP_RE.match(text, t_idx)
                if safe_m is not None and safe_m.end() > t_idx:
                    self._write_slice_run(
                        scr,
                        text,
                        t_idx,
                        safe_m.end(),
                        autowrap=autowrap,
                        bottom=bottom,
                        left=left,
                        right=right,
                        mode_2027=mode_2027,
                    )
                    t_idx = safe_m.end()
                    if t_idx >= total:
                        return

            ch = text[t_idx]
            t_idx += 1
            cp = ord(ch)
            if self._pending_prepend:
                extends, next_state, _ = step_cluster(
                    self._cluster_state,
                    cp,
                    mode_2027=mode_2027,
                    vs15_narrows=vs15_narrows,
                    vs16_always_wide=vs16_wide,
                )
                if extends:
                    w_prep = char_width(ch)
                    if w_prep == 0:
                        if len(self._pending_prepend) < max_cluster - 1:
                            self._pending_prepend += ch
                        self._cluster_state = next_state
                        continue
                    ch_full = self._pending_prepend + ch
                    self._pending_prepend = ""
                else:
                    self._pending_prepend = ""
                    ch_full = ch
            else:
                ch_full = ch

            if len(ch_full) == 1 and self._last_printed_pos is not None:
                extends, next_state, w_override = step_cluster(
                    self._cluster_state,
                    cp,
                    mode_2027=mode_2027,
                    vs15_narrows=vs15_narrows,
                    vs16_always_wide=vs16_wide,
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
                        can_wrap_row = pr < scr.rows - 1 or (
                            pr == bottom and left <= pc <= right
                        )
                        if (
                            w_override == 2
                            and cur_role == int(WidthRole.NARROW)
                            and pc == eff_right
                            and autowrap
                            and can_wrap_row
                            and eff_right - eff_left >= 1
                        ):
                            prev_cluster_len = self._cluster_len
                            combined = row_obj.chars[pc] + ch
                            base_pen = row_obj.attr_at(pc) & _PEN_BASE_MASK
                            base_ext = row_obj.ext_at(pc)
                            bce_w, bce_e = self._bce_pair()
                            full_row_wrap = eff_left == 0 and eff_right == scr.cols - 1
                            if full_row_wrap and pr == 0 and bottom == 0:
                                row_obj.write_spacer_head(pc, bce_w, bce_e)
                                row_obj.wrapped = True
                            else:
                                row_obj.write_narrow(pc, " ", bce_w, bce_e)
                            self._linefeed(scr)
                            if full_row_wrap and not (pr == 0 and bottom == 0):
                                wrap_idx = pr - 1 if pr == bottom else pr
                                if 0 <= wrap_idx < scr.rows:
                                    scr._rows[wrap_idx].write_spacer_head(
                                        pc, bce_w, bce_e
                                    )
                                    scr._rows[wrap_idx].wrapped = True
                            nr = scr._cur_row
                            if not full_row_wrap:
                                scr.heal_before_edit(nr, eff_left, eff_left + 2)
                            if insert_mode:
                                scr._rows[nr].insert_blanks(
                                    eff_left,
                                    2,
                                    left=eff_left,
                                    right=eff_right,
                                    bce_word=bce_w,
                                    bce_ext=bce_e,
                                )
                            scr._rows[nr].write_wide(
                                eff_left,
                                combined,
                                base_pen,
                                base_ext,
                            )
                            if scr._gfx is not None and scr._gfx.has_sixels:
                                scr._gfx.erase_sixel_rect(
                                    row=nr,
                                    col=eff_left,
                                    rows=1,
                                    cols=(
                                        (eff_right - eff_left + 1) if insert_mode else 2
                                    ),
                                    scrolled_total=scr._scrolled_total,
                                    policy=sixel_pol,
                                )
                            self._cluster_state = next_state
                            self._cluster_len = prev_cluster_len + 1
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

                        if (
                            w_override == 2
                            and cur_role == int(WidthRole.NARROW)
                            and pc >= eff_right
                        ):
                            # Cannot wrap (e.g. ?7l or bottom margin): keep cell narrow
                            # without appending a selector that inflates display_width.
                            continue

                        if (
                            w_override == 2
                            and cur_role == int(WidthRole.NARROW)
                            and pc + 1 < scr.cols
                        ):
                            scr.heal_before_edit(pr, pc, pc + 2)
                            if insert_mode and pc + 1 <= eff_right:
                                bce_w, bce_e = self._bce_pair()
                                row_obj.insert_blanks(
                                    pc + 1,
                                    1,
                                    left=eff_left,
                                    right=eff_right,
                                    bce_word=bce_w,
                                    bce_ext=bce_e,
                                )
                            if scr._gfx is not None and scr._gfx.has_sixels:
                                scr._gfx.erase_sixel_rect(
                                    row=pr,
                                    col=pc,
                                    rows=1,
                                    cols=(eff_right - pc + 1) if insert_mode else 2,
                                    scrolled_total=scr._scrolled_total,
                                    policy=sixel_pol,
                                )
                        vacated_bce = (
                            (self._pen_word & _PEN_BG_MASK) if w_override == 1 else 0
                        )
                        row_obj.append_grapheme(
                            pc,
                            ch,
                            width_override=w_override,
                            bce_word=vacated_bce,
                        )
                        if pc == 0 and w_override == 1:
                            scr.heal_wrapped_spacer_head(pr)
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
            elif len(ch_full) == 1:
                _, next_state, _ = step_cluster(
                    CLUSTER_NONE,
                    cp,
                    mode_2027=mode_2027,
                    vs15_narrows=vs15_narrows,
                    vs16_always_wide=vs16_wide,
                )

            width = char_width(ch)
            if width == 0:
                if mode_2027 and ((ucd_lookup(cp) >> 2) & 0x0F) == GCB_PREPEND:
                    self._last_printed_pos = None
                    self._pending_prepend = ch
                    self._cluster_state = next_state
                else:
                    self._clear_cluster_anchor()
                continue

            c_row = scr._cur_row
            c_col = scr._cur_col
            max_col = right if c_col <= right else scr.cols - 1
            min_col = left if c_col >= left else 0
            just_wrapped_wide = False

            if scr._pending_wrap and autowrap:
                can_advance = c_row < scr.rows - 1 or (
                    c_row == bottom and left <= c_col <= right
                )
                set_wrap = can_advance and min_col == 0 and c_col == scr.cols - 1
                if set_wrap and c_row == 0 and bottom == 0:
                    scr._rows[0].wrapped = True
                    bump_row_gen(scr._rows[0])
                self._linefeed(scr)
                if set_wrap and not (c_row == 0 and bottom == 0):
                    wrap_idx = c_row - 1 if c_row == bottom else c_row
                    if 0 <= wrap_idx < scr.rows:
                        scr._rows[wrap_idx].wrapped = True
                        bump_row_gen(scr._rows[wrap_idx])
                self._set_cursor(scr, scr._cur_row, min_col, pending_wrap=False)
                c_row = scr._cur_row
                c_col = scr._cur_col

            if width == 2 and c_col == max_col:
                can_advance = c_row < scr.rows - 1 or (
                    c_row == bottom and left <= c_col <= right
                )
                if autowrap and can_advance and max_col - min_col >= 1:
                    bce_w, bce_e = self._bce_pair()
                    full_row_wrap = min_col == 0 and max_col == scr.cols - 1
                    if full_row_wrap and c_row == 0 and bottom == 0:
                        scr._rows[0].write_spacer_head(c_col, bce_w, bce_e)
                        scr._rows[0].wrapped = True
                        just_wrapped_wide = True
                    else:
                        scr._rows[c_row].write_narrow(c_col, " ", bce_w, bce_e)
                    self._linefeed(scr)
                    if full_row_wrap and not (c_row == 0 and bottom == 0):
                        wrap_idx = c_row - 1 if c_row == bottom else c_row
                        if 0 <= wrap_idx < scr.rows:
                            scr._rows[wrap_idx].write_spacer_head(c_col, bce_w, bce_e)
                            scr._rows[wrap_idx].wrapped = True
                        just_wrapped_wide = True
                    self._set_cursor(scr, scr._cur_row, min_col, pending_wrap=False)
                    c_row = scr._cur_row
                    c_col = scr._cur_col
                else:
                    ch_full = "\ufffd"
                    width = 1

            if not just_wrapped_wide:
                scr.heal_before_edit(c_row, c_col, c_col + width)

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
                    ch_full,
                    self._pen_word,
                    self._cell_ext,
                )
                written_cols = 2
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
                    ch_full,
                    self._pen_word,
                    self._cell_ext,
                )
                written_cols = 1
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

            if scr._gfx is not None and scr._gfx.has_sixels:
                scr._gfx.erase_sixel_rect(
                    row=c_row,
                    col=placed_col,
                    rows=1,
                    cols=(max_col - placed_col + 1) if insert_mode else written_cols,
                    scrolled_total=scr._scrolled_total,
                    policy=sixel_pol,
                )

            self._last_printed = ch_full
            self._last_printed_pos = (c_row, placed_col)
            self._cluster_state = next_state
            self._cluster_len = len(ch_full)

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
            self._before_mutate()
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
        self._last_printed = ""
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
        self._before_mutate()
        scr = self._screens.active
        saved = self._saved_alt if self._screens.active_is_alt else self._saved_primary
        self._style = saved.style
        self._protected = saved.protected
        self._sync_pen_cache()
        self._g_sets = list(saved.g_sets)
        self._gl = saved.gl
        self._single_shift = None
        self._modes.set_mode(Mode.DECOM, enabled=saved.decom)
        restored_wrap = (
            False if self._profile.pending_wrap_phantom_col else saved.pending_wrap
        )
        self._set_cursor(
            scr,
            saved.row,
            saved.col,
            pending_wrap=restored_wrap,
        )

    def _handle_esc(self, intermediates: bytes, final: int) -> None:
        self._last_printed = ""
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
                keep_wrap = self._profile.pending_wrap_phantom_col and scr._pending_wrap
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
                        scr,
                        scr._cur_row - 1,
                        scr._cur_col,
                        pending_wrap=keep_wrap,
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
            fill = "E" * scr.cols
            for r in scr._rows:
                r.reset_blank(scr.cols)
                r.write_ascii_slice(0, fill, 0, None)
            scr.scroll_region = ScrollRegion(
                top=0,
                bottom=scr.rows - 1,
                left=0,
                right=scr.cols - 1,
            )
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

        if mode == Mode.SYNCHRONIZED_OUTPUT:
            if enabled == was_set:
                return
            self._invalidate_cache()
            self._modes.set_mode(mode, enabled=enabled)
            if enabled:
                self._sync_generation += 1
                self._sync_hold_pending = True
                self._held_snapshot = None
                self._held_sync_token = None
                self._step_event |= StopOn.SYNC_START
            else:
                self._sync_hold_pending = False
                self._held_snapshot = None
                self._held_sync_token = None
                self._step_event |= StopOn.SYNC_END
            self._emit_event(
                RenderHoldChanged(
                    active=enabled,
                    sync_generation=self._sync_generation,
                ),
            )
            return

        if mode in ALT_SCREEN_MODES:
            self._before_mutate()
            self._clear_cluster_anchor()
            if enabled and not self._screens.active_is_alt:
                if mode == Mode.ALT_SCREEN:
                    self._save_cursor()
                self._screens.alternate.reset()
                self._screens.active_is_alt = True
                self._epoch += 1
                self._modes.sync_alt_screen(active_is_alt=True, entered_via=mode)
            elif not enabled and self._screens.active_is_alt:
                if mode in (Mode.ALT_SCREEN_1047, Mode.ALT_SCREEN):
                    self._screens.alternate.reset()
                self._screens.active_is_alt = False
                self._epoch += 1
                self._modes.sync_alt_screen(active_is_alt=False)
                if mode == Mode.ALT_SCREEN:
                    self._restore_cursor()
            elif not enabled:
                self._modes.sync_alt_screen(active_is_alt=False)
            return

        self._before_mutate()
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

    def _erase_display(
        self,
        scr: Screen,
        mode: int,
        *,
        selective: bool,
    ) -> None:
        if mode == 3 and not selective:
            self._before_mutate()
            if (
                not self._screens.active_is_alt
                or self._profile.ed3_clears_scrollback_on_alt
            ):
                self._screens.primary.clear_history()
            else:
                scr.clear_history()
            return

        if mode not in (0, 1, 2) and not (mode == 22 and not selective):
            return

        self._before_mutate()
        bce_w, bce_e = self._bce_pair()
        cause = EraseCause.DECSED if selective else EraseCause.ED
        c_row = scr._cur_row
        c_col = scr._cur_col
        phantom = self._profile.pending_wrap_phantom_col and scr._pending_wrap
        sixel_pol = self._profile.sixel_erase_policy
        scroll_clear = (
            not selective
            and self._profile.ed2_scrolls_primary_to_history
            and not self._screens.active_is_alt
            and (mode == 2 or (mode == 0 and c_row == 0 and c_col == 0 and not phantom))
        )
        if scroll_clear:
            scr.push_scrollback_and_clear(
                bce_w,
                bce_e,
                include_cursor_row=False,
            )
            self._notify_erase(Rect(0, 0, scr.rows, scr.cols), cause)
            self._set_cursor(scr, c_row, c_col, pending_wrap=phantom)
            return

        if mode == 0:
            if not phantom:
                scr.heal_before_edit(c_row, c_col, scr.cols)
                scr._rows[c_row].clear_range(
                    c_col,
                    scr.cols,
                    bce_w,
                    bce_e,
                    selective=selective,
                )
                scr._rows[c_row].wrapped = False
                if not selective and scr._gfx is not None and scr._gfx.has_sixels:
                    scr._gfx.erase_sixel_rect(
                        row=c_row,
                        col=c_col,
                        rows=1,
                        cols=scr.cols - c_col,
                        scrolled_total=scr._scrolled_total,
                        policy=sixel_pol,
                    )
                self._notify_erase(Rect(c_row, c_col, 1, scr.cols - c_col), cause)
            for r in range(c_row + 1, scr.rows):
                scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e, selective=selective)
            if c_row + 1 < scr.rows:
                if not selective and scr._gfx is not None and scr._gfx.has_sixels:
                    scr._gfx.erase_sixel_rect(
                        row=c_row + 1,
                        col=0,
                        rows=scr.rows - c_row - 1,
                        cols=scr.cols,
                        scrolled_total=scr._scrolled_total,
                        policy=sixel_pol,
                    )
                self._notify_erase(
                    Rect(c_row + 1, 0, scr.rows - c_row - 1, scr.cols),
                    cause,
                )
        elif mode == 1:
            end_c = scr.cols if phantom else c_col + 1
            for r in range(c_row):
                scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e, selective=selective)
            scr.heal_before_edit(c_row, 0, end_c)
            scr._rows[c_row].clear_range(
                0,
                end_c,
                bce_w,
                bce_e,
                selective=selective,
            )
            if not selective and scr._gfx is not None and scr._gfx.has_sixels:
                if c_row > 0:
                    scr._gfx.erase_sixel_rect(
                        row=0,
                        col=0,
                        rows=c_row,
                        cols=scr.cols,
                        scrolled_total=scr._scrolled_total,
                        policy=sixel_pol,
                    )
                scr._gfx.erase_sixel_rect(
                    row=c_row,
                    col=0,
                    rows=1,
                    cols=end_c,
                    scrolled_total=scr._scrolled_total,
                    policy=sixel_pol,
                )
            if c_row > 0:
                self._notify_erase(Rect(0, 0, c_row, scr.cols), cause)
            self._notify_erase(Rect(c_row, 0, 1, end_c), cause)
        elif mode == 2:
            for r in range(scr.rows):
                scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e, selective=selective)
            if not selective and scr._gfx is not None:
                scr._gfx.clear_active_screen(scr._scrolled_total, scr.rows)
            self._notify_erase(Rect(0, 0, scr.rows, scr.cols), cause)
        elif mode == 22 and not selective:
            if not self._screens.active_is_alt:
                scr.push_scrollback_and_clear(bce_w, bce_e)
            else:
                for r in range(scr.rows):
                    scr._rows[r].clear_range(0, scr.cols, bce_w, bce_e)
                if scr._gfx is not None:
                    scr._gfx.clear_active_screen(scr._scrolled_total, scr.rows)
            self._notify_erase(Rect(0, 0, scr.rows, scr.cols), cause)
        self._set_cursor(scr, c_row, c_col, pending_wrap=phantom)

    def _erase_line(
        self,
        scr: Screen,
        mode: int,
        *,
        selective: bool,
    ) -> None:
        if mode not in (0, 1, 2):
            return
        phantom = self._profile.pending_wrap_phantom_col and scr._pending_wrap
        if mode == 0 and phantom:
            return
        self._before_mutate()
        bce_w, bce_e = self._bce_pair()
        cause = EraseCause.DECSEL if selective else EraseCause.EL
        c_row = scr._cur_row
        c_col = scr._cur_col
        sixel_pol = self._profile.sixel_erase_policy
        if mode == 0:
            scr.heal_before_edit(c_row, c_col, scr.cols)
            scr._rows[c_row].clear_range(
                c_col,
                scr.cols,
                bce_w,
                bce_e,
                selective=selective,
            )
            scr._rows[c_row].wrapped = False
            if not selective and scr._gfx is not None and scr._gfx.has_sixels:
                scr._gfx.erase_sixel_rect(
                    row=c_row,
                    col=c_col,
                    rows=1,
                    cols=scr.cols - c_col,
                    scrolled_total=scr._scrolled_total,
                    policy=sixel_pol,
                )
            self._notify_erase(Rect(c_row, c_col, 1, scr.cols - c_col), cause)
        elif mode == 1:
            end_c = scr.cols if phantom else c_col + 1
            scr.heal_before_edit(c_row, 0, end_c)
            scr._rows[c_row].clear_range(
                0,
                end_c,
                bce_w,
                bce_e,
                selective=selective,
            )
            if end_c >= scr.cols:
                scr._rows[c_row].wrapped = False
            if not selective and scr._gfx is not None and scr._gfx.has_sixels:
                scr._gfx.erase_sixel_rect(
                    row=c_row,
                    col=0,
                    rows=1,
                    cols=end_c,
                    scrolled_total=scr._scrolled_total,
                    policy=sixel_pol,
                )
            self._notify_erase(Rect(c_row, 0, 1, end_c), cause)
        elif mode == 2:
            scr.heal_before_edit(c_row, 0, scr.cols)
            scr._rows[c_row].clear_range(
                0,
                scr.cols,
                bce_w,
                bce_e,
                selective=selective,
            )
            if not selective and scr._gfx is not None and scr._gfx.has_sixels:
                scr._gfx.erase_sixel_rect(
                    row=c_row,
                    col=0,
                    rows=1,
                    cols=scr.cols,
                    scrolled_total=scr._scrolled_total,
                    policy=sixel_pol,
                )
            self._notify_erase(Rect(c_row, 0, 1, scr.cols), cause)
        self._set_cursor(scr, c_row, c_col, pending_wrap=phantom)

    def _handle_csi(
        self,
        prefix: int,
        params: CsiParams,
        intermediates: bytes,
        final: int,
    ) -> None:
        prev_printed = self._last_printed
        self._last_printed = ""
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
            if self._profile.decstr_enabled:
                self.reset(soft=True)
            return

        # DECSCUSR: CSI Ps SP q
        if prefix == 0 and intermediates == b" " and final == ord("q"):
            self._before_mutate()
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
                    phantom_extra = (
                        1
                        if (
                            self._profile.pending_wrap_phantom_col and scr._pending_wrap
                        )
                        else 0
                    )
                    r_out = (
                        (scr._cur_row - top + 1)
                        if self._modes.is_set(Mode.DECOM)
                        else (scr._cur_row + 1)
                    )
                    c_out = (
                        (scr._cur_col - left + 1 + phantom_extra)
                        if self._modes.is_set(Mode.DECOM)
                        else (scr._cur_col + 1 + phantom_extra)
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
        elif ch_final == "a":  # HPR
            if not self._profile.hpr_vpr_enabled:
                return
            n = params.get(0, 1)
            ceil = right if self._modes.is_set(Mode.DECOM) else scr.cols - 1
            self._set_cursor(scr, c_row, min(ceil, c_col + n), pending_wrap=False)
        elif ch_final == "d":  # VPA
            row_1 = params.get(0, 1)
            if self._modes.is_set(Mode.DECOM):
                target_r = min(bottom, top + row_1 - 1)
            else:
                target_r = min(scr.rows - 1, row_1 - 1)
            keep_wrap = self._profile.pending_wrap_phantom_col and scr._pending_wrap
            self._set_cursor(scr, max(0, target_r), c_col, pending_wrap=keep_wrap)
        elif ch_final == "e":  # VPR
            if not self._profile.hpr_vpr_enabled:
                return
            n = params.get(0, 1)
            ceil = bottom if self._modes.is_set(Mode.DECOM) else scr.rows - 1
            self._set_cursor(scr, min(ceil, c_row + n), c_col, pending_wrap=False)
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
            if self._profile.pending_wrap_phantom_col and scr._pending_wrap:
                return
            self._before_mutate()
            n = min(scr.cols - c_col, params.get(0, 1))
            bce_w, bce_e = self._bce_pair()
            scr.heal_before_edit(c_row, c_col, c_col + n)
            scr._rows[c_row].clear_range(c_col, c_col + n, bce_w, bce_e)
            if c_col + n >= scr.cols:
                scr._rows[c_row].wrapped = False
            if scr._gfx is not None and scr._gfx.has_sixels:
                scr._gfx.erase_sixel_rect(
                    row=c_row,
                    col=c_col,
                    rows=1,
                    cols=n,
                    scrolled_total=scr._scrolled_total,
                    policy=self._profile.sixel_erase_policy,
                )
            self._notify_erase(Rect(c_row, c_col, 1, n), EraseCause.ECH)
            self._set_cursor(scr, c_row, c_col, pending_wrap=False)
        elif ch_final == "@":  # ICH
            if self._profile.pending_wrap_phantom_col and scr._pending_wrap:
                return
            self._before_mutate()
            n = params.get(0, 1)
            bce_w, bce_e = self._bce_pair()
            scr.heal_before_edit(c_row, c_col, right + 1)
            scr._rows[c_row].insert_blanks(
                c_col,
                n,
                left=left,
                right=right,
                bce_word=bce_w,
                bce_ext=bce_e,
            )
            if scr._gfx is not None and scr._gfx.has_sixels and left <= c_col <= right:
                scr._gfx.erase_sixel_rect(
                    row=c_row,
                    col=c_col,
                    rows=1,
                    cols=right - c_col + 1,
                    scrolled_total=scr._scrolled_total,
                    policy=self._profile.sixel_erase_policy,
                )
            self._set_cursor(scr, c_row, c_col, pending_wrap=False)
        elif ch_final == "P":  # DCH
            if self._profile.pending_wrap_phantom_col and scr._pending_wrap:
                return
            self._before_mutate()
            n = params.get(0, 1)
            bce_w, bce_e = self._bce_pair()
            scr.heal_before_edit(c_row, c_col, right + 1)
            if c_col == 0 and left == 0 and c_row > 0 and scr._rows[c_row - 1].wrapped:
                scr._rows[c_row - 1].wrapped = False
                bump_row_gen(scr._rows[c_row - 1])
            scr._rows[c_row].delete_cells(
                c_col,
                n,
                left=left,
                right=right,
                bce_word=bce_w,
                bce_ext=bce_e,
            )
            if right == scr.cols - 1:
                scr._rows[c_row].wrapped = False
            if scr._gfx is not None and scr._gfx.has_sixels and left <= c_col <= right:
                scr._gfx.erase_sixel_rect(
                    row=c_row,
                    col=c_col,
                    rows=1,
                    cols=right - c_col + 1,
                    scrolled_total=scr._scrolled_total,
                    policy=self._profile.sixel_erase_policy,
                )
            self._set_cursor(scr, c_row, c_col, pending_wrap=False)
        elif ch_final == "L":  # IL
            if top <= c_row <= bottom and left <= c_col <= right:
                self._before_mutate()
                n = params.get(0, 1)
                bce_w, bce_e = self._bce_pair()
                keep_wrap = (
                    not self._profile.il_dl_resets_col
                    and self._profile.pending_wrap_phantom_col
                    and scr._pending_wrap
                )
                scr.scroll_down(
                    n,
                    top=c_row,
                    bottom=bottom,
                    left=left,
                    right=right,
                    bce_word=bce_w,
                    bce_ext=bce_e,
                    is_line_op=True,
                )
                self._notify_erase(
                    Rect(c_row, left, min(n, bottom - c_row + 1), right - left + 1),
                    EraseCause.SCROLL,
                )
                target_col = left if self._profile.il_dl_resets_col else c_col
                self._set_cursor(scr, c_row, target_col, pending_wrap=keep_wrap)
        elif ch_final == "M":  # DL
            if top <= c_row <= bottom and left <= c_col <= right:
                self._before_mutate()
                n = params.get(0, 1)
                bce_w, bce_e = self._bce_pair()
                keep_wrap = (
                    not self._profile.il_dl_resets_col
                    and self._profile.pending_wrap_phantom_col
                    and scr._pending_wrap
                )
                scr.scroll_up(
                    n,
                    top=c_row,
                    bottom=bottom,
                    left=left,
                    right=right,
                    save_history=False,
                    bce_word=bce_w,
                    bce_ext=bce_e,
                    is_line_op=True,
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
                target_col = left if self._profile.il_dl_resets_col else c_col
                self._set_cursor(scr, c_row, target_col, pending_wrap=keep_wrap)
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
            if prev_printed:
                count = min(self._limits.max_rep_count, params.get(0, 1))
                if not self._profile.rep_wraps and self._modes.is_set(Mode.DECAWM):
                    if scr._pending_wrap and self._profile.pending_wrap_phantom_col:
                        self._last_printed = prev_printed
                    else:
                        w = max(1, char_width(prev_printed[0]))
                        max_c = right if c_col <= right else scr.cols - 1
                        avail_cols = max_c - c_col + 1
                        fit = max(0, avail_cols // w)
                        if fit > 0:
                            self._handle_print(prev_printed * min(count, fit))
                        self._last_printed = prev_printed
                else:
                    self._handle_print(prev_printed * count)
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
                phantom_extra = (
                    1
                    if (self._profile.pending_wrap_phantom_col and scr._pending_wrap)
                    else 0
                )
                r_out = (
                    (c_row - top + 1) if self._modes.is_set(Mode.DECOM) else (c_row + 1)
                )
                c_out = (
                    (c_col - left + 1 + phantom_extra)
                    if self._modes.is_set(Mode.DECOM)
                    else (c_col + 1 + phantom_extra)
                )
                self._emit_reply(f"\x1b[{r_out};{c_out}R".encode("ascii"))
        elif ch_final == "t":  # XTWINOPS
            op = params.raw_or_default(0, 0)
            if op == 18:  # report text area size in chars
                self._emit_reply(f"\x1b[8;{scr.rows};{scr.cols}t".encode("ascii"))
            elif op == 14:  # report text area size in pixels
                px_h = scr.rows * self._profile.cell_height_px
                px_w = scr.cols * self._profile.cell_width_px
                self._emit_reply(f"\x1b[4;{px_h};{px_w}t".encode("ascii"))
            elif op == 16:  # report cell size in pixels
                self._emit_reply(
                    f"\x1b[6;{self._profile.cell_height_px};{self._profile.cell_width_px}t".encode(
                        "ascii"
                    ),
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
                self._before_mutate()
                self._title = self._title_stack.pop()
                self._aux_revision += 1
                self._emit_event(TitleChanged(title=self._title))
        else:
            self._unhandled("csi", f"CSI {ch_final}")

    def _handle_window_rename(self, raw: bytes) -> None:
        self._last_printed = ""
        try:
            name = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return
        self._before_mutate()
        self._window_name = name
        self._aux_revision += 1
        self._emit_event(WindowRenamed(name=name))

    def _update_cwd(self, raw_uri: str, *, from_osc7: bool = True) -> None:
        self._before_mutate()
        cleaned = raw_uri.strip()
        if not cleaned or "\x00" in cleaned:
            self._cwd = ""
            self._cwd_uri = ""
            self._emit_event(WorkingDirectoryChanged(uri="", path=""))
            return
        safe_uri = sanitize_terminal_text(cleaned)
        self._cwd_uri = safe_uri
        if not from_osc7 and not cleaned.startswith("file://"):
            self._cwd = safe_uri
            self._emit_event(WorkingDirectoryChanged(uri=safe_uri, path=safe_uri))
            return
        parsed = urllib.parse.urlsplit(cleaned)
        if parsed.scheme.lower() != "file" or not parsed.path.startswith("/"):
            self._cwd = ""
            self._emit_event(WorkingDirectoryChanged(uri=safe_uri, path=""))
            return
        raw_host = urllib.parse.unquote(parsed.hostname or "").lower()
        allowed_hosts = (
            self._policy.osc7_hosts
            if self._policy.osc7_hosts is not None
            else DEFAULT_OSC7_HOSTS
        )
        raw_path = urllib.parse.unquote_to_bytes(parsed.path)
        if b"\x00" in raw_path:
            self._cwd = ""
            self._emit_event(WorkingDirectoryChanged(uri=safe_uri, path=""))
            return
        decoded_path = os.fsdecode(raw_path)
        if raw_host in allowed_hosts:
            self._cwd = decoded_path
        else:
            self._cwd = ""
        self._emit_event(
            WorkingDirectoryChanged(
                uri=safe_uri,
                path=sanitize_terminal_text(decoded_path),
            ),
        )

    def _handle_osc(self, code: int, data: bytes, terminator: bytes) -> None:
        self._last_printed = ""
        scr = self._screens.active
        out_term = b"\x07" if terminator == b"\x07" else b"\x1b\\"

        if code in (0, 2):
            self._before_mutate()
            text = data.decode("utf-8", errors="replace")
            self._title = text
            self._emit_event(TitleChanged(title=text))
        elif code == 1:
            return
        elif code == 7:
            self._update_cwd(data.decode("utf-8", errors="replace"), from_osc7=True)
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
                self._before_mutate()
                sub = int(parts[1])
                prog = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                changed, status = self._status_store.apply_osc_9_4(sub, prog)
                if changed:
                    self._aux_revision += 1
                    self._emit_event(ProgramStatusChanged(status=status, task_id=""))
            elif len(parts) >= 2 and parts[0] == "9":
                self._update_cwd(";".join(parts[1:]), from_osc7=False)
        elif code in (10, 11):
            if data == b"?":
                if self._policy.allow_color_queries:
                    r, g, b = self._fg_rgb if code == 10 else self._bg_rgb
                    reply = (
                        f"\x1b]{code};rgb:{r:02x}{r:02x}/{g:02x}{g:02x}/{b:02x}{b:02x}"
                    ).encode("ascii")
                    self._emit_reply(reply + out_term)
                return
            parsed_rgb = _parse_osc_rgb(data)
            if parsed_rgb is not None:
                if code == 10:
                    self._fg_rgb = parsed_rgb
                else:
                    self._bg_rgb = parsed_rgb
        elif code == 12:
            return
        elif code == 110:
            self._fg_rgb = self._profile.fg_rgb
        elif code == 111:
            self._bg_rgb = self._profile.bg_rgb
        elif code == 112:
            return
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
                self._before_mutate()
                kind = PromptKind(raw_kind)
                exit_code: int | None = None
                is_continuation = any(p.strip() == "k=s" for p in parts[1:])
                if (
                    kind == PromptKind.COMMAND_FINISHED
                    and len(parts) >= 2
                    and parts[1].lstrip("-").isdigit()
                ):
                    exit_code = int(parts[1])
                if not self._screens.active_is_alt:
                    abs_r = self._screens.primary.scrolled_total + scr._cur_row
                    zone = PromptZone(
                        kind=kind,
                        abs_row=abs_r,
                        col=scr._cur_col,
                        exit_code=exit_code,
                    )
                    self._prompt_zones.append(zone)
                    self._aux_revision += 1
                    self._step_event |= StopOn.PROMPT_MARK
                    self._emit_event(PromptMark(zone=zone))
                if (
                    kind == PromptKind.PROMPT_START
                    and not is_continuation
                    and self._status_store.on_prompt_start()
                ):
                    self._aux_revision += 1
                    self._emit_event(
                        ProgramStatusChanged(status=self._status_store.most_urgent),
                    )
        elif code == 7501:
            if data == b"?":
                cap = self._profile.osc_7501_capabilities
                if self._policy.allow_program_status_query and cap:
                    self._emit_reply(f"\x1b]7501;{cap}".encode("ascii") + out_term)
                return
            self._before_mutate()
            changed, rec = self._status_store.apply_osc_7501(data)
            if changed:
                self._aux_revision += 1
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
        self._last_printed = ""
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
            prefix == 0
            and not intermediates
            and not params.values
            and final == ord("t")
            and data.startswith(b"mux;")
        ):
            unwrapped = bytes(memoryview(data)[4:])
            if self._record_events or self._on_event is not None:
                self._emit_event(Passthrough(data=unwrapped))
            if self._policy.allow_passthrough and self._passthrough_depth < 2:
                if self._passthrough_depth == 0:
                    if self._passthrough_parser is None:
                        self._passthrough_parser = Parser(
                            encoding=self._encoding,
                            tmux_window_rename=self._profile.tmux_window_rename,
                            abort_dispatch_osc=self._policy.abort_dispatch_osc,
                            limits=self._limits,
                        )
                    inner_parser = self._passthrough_parser
                else:
                    inner_parser = Parser(
                        encoding=self._encoding,
                        tmux_window_rename=self._profile.tmux_window_rename,
                        abort_dispatch_osc=self._policy.abort_dispatch_osc,
                        limits=self._limits,
                    )
                self._passthrough_depth += 1
                try:
                    inner_parser.feed_to(unwrapped, self._sink)
                finally:
                    self._passthrough_depth -= 1
            return

        # SIXEL: DCS Ps... q <sixel> ST
        if prefix == 0 and not intermediates and final == ord("q"):
            scr = self._screens.active
            orig_row, orig_col = scr._cur_row, scr._cur_col
            if self._policy.track_graphics:
                self._before_mutate()
                decsdm = self._modes.is_set(Mode.DECSDM)
                anchor_r = 0 if decsdm else orig_row
                anchor_c = 0 if decsdm else orig_col
                gfx = scr.ensure_graphics()
                prepared = gfx.prepare_sixel(
                    data,
                    col=anchor_c,
                    screen_cols=scr.cols,
                    cell_width_px=self._profile.cell_width_px,
                    cell_height_px=self._profile.cell_height_px,
                )
                if prepared is not None:
                    w_px, h_px, s_rows, s_cols = prepared
                    pre_scrolled = scr._scrolled_total
                    top, bottom, left, right = self._effective_margins(scr)
                    in_margins = top <= orig_row <= bottom and left <= orig_col <= right
                    if decsdm:
                        post_r = anchor_r
                        eff_rows = max(1, min(s_rows, scr.rows - anchor_r))
                        min_sixel_abs = 0
                    else:
                        post_r = self._advance_rows(scr, s_rows, final_col=0)
                        eff_rows = min(s_rows, max(1, scr.rows - post_r))
                        min_sixel_abs = (
                            scr._scrolled_total + (top if in_margins else 0)
                            if scr._scrolled_total == pre_scrolled
                            else scr._scrolled_total - len(scr._history)
                        )
                    gfx.commit_sixel(
                        payload_len=len(data),
                        width_px=w_px,
                        height_px=h_px,
                        row=post_r,
                        col=anchor_c,
                        rows=eff_rows,
                        cols=s_cols,
                        scrolled_total=scr._scrolled_total,
                        min_abs=min_sixel_abs,
                    )
            if self._record_events or self._on_event is not None:
                self._emit_event(
                    SixelGraphic(
                        data=SixelData(
                            row=orig_row,
                            col=orig_col,
                            params=params.values,
                            payload=data,
                        ),
                    ),
                )
            return

        inter_s = intermediates.decode("ascii", errors="replace")
        self._unhandled("dcs", f"DCS {inter_s}{chr(final)}")

    def _handle_apc(self, data: bytes) -> None:
        self._last_printed = ""
        if data.startswith(b"G"):
            scr = self._screens.active
            orig_row, orig_col = scr._cur_row, scr._cur_col
            need_parse = (
                self._policy.track_graphics
                or self._record_events
                or self._on_event is not None
            )
            if need_parse:
                body = data[1:]
                sep = body.find(b";")
                ctrl_bytes = body if sep == -1 else body[:sep]
                payload = b"" if sep == -1 else body[sep + 1 :]
                control: dict[str, str] = {}
                for pair in ctrl_bytes.decode("ascii", errors="ignore").split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        control[k] = v
                if self._policy.track_graphics:
                    self._before_mutate()
                    top, bottom, left, right = self._effective_margins(scr)
                    in_margins = top <= orig_row <= bottom and left <= orig_col <= right

                    def _advance_kitty(p_rows: int, p_cols: int) -> int:
                        target_x = orig_col + p_cols
                        wraps = target_x >= scr.cols
                        lf_count = max(0, p_rows - 1) + (1 if wraps else 0)
                        final_c = 0 if wraps else target_x
                        return self._advance_rows(scr, lf_count, final_col=final_c)

                    reply = scr.ensure_graphics().handle_kitty(
                        control,
                        payload,
                        cursor_row=orig_row,
                        cursor_col=orig_col,
                        screen_rows=scr.rows,
                        screen_cols=scr.cols,
                        scrolled_total_getter=lambda: scr._scrolled_total,
                        scrolled_total=scr._scrolled_total,
                        cell_width_px=self._profile.cell_width_px,
                        cell_height_px=self._profile.cell_height_px,
                        min_row=(top if in_margins else 0),
                        advance_cursor=_advance_kitty,
                    )
                    if reply is not None:
                        self._emit_reply(reply)
                if self._record_events or self._on_event is not None:
                    self._emit_event(
                        KittyGraphic(
                            command=KittyCommand(
                                row=orig_row,
                                col=orig_col,
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
    newline_mode: bool = False,
) -> str:
    """Emulate *data* on a fresh ``(rows, cols)`` terminal and return ``text``."""
    term = Terminal(
        rows=rows,
        cols=cols,
        scrollback=0,
        profile=profile,
        newline_mode=newline_mode,
    )
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
    "ResyncReason",
    "ScreenDelta",
    "Snapshot",
    "StepResult",
    "StopOn",
    "SyncToken",
    "SyncView",
    "Terminal",
    "render",
    "strip",
]
