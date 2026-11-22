"""Zero-based screen buffer, lazy cell/row/region views, and dual screen set."""

from __future__ import annotations

import collections
import collections.abc
import enum
import typing as t
from dataclasses import dataclass

from libvt._grid import (
    _CellExt,
    _Row,
    unpack_style,
    unpack_width_role,
)
from libvt._width import WidthRole
from libvt.style import DEFAULT_STYLE, Hyperlink, Style


class CursorShape(enum.StrEnum):
    """Cursor glyph shape configured via ``DECSCUSR`` (``CSI Ps SP q``)."""

    BLOCK = "block"
    UNDERLINE = "underline"
    BAR = "bar"


@dataclass(frozen=True, slots=True)
class Cursor:
    """Zero-based cursor state on a :class:`Screen`.

    Attributes
    ----------
    row : int
        Zero-based row index in ``0..rows - 1``.
    col : int
        Zero-based column index in ``0..cols - 1``.
    visible : bool
        Whether the cursor is visible (``DECTCEM`` ``?25``).
    shape : CursorShape
        Visual cursor shape.
    blinking : bool
        Whether cursor blinking is active.
    pending_wrap : bool
        ``True`` when a glyph filled ``cols - 1`` under ``DECAWM`` and the
        next printable character will wrap to the next line.
    """

    row: int = 0
    col: int = 0
    visible: bool = True
    shape: CursorShape = CursorShape.BLOCK
    blinking: bool = True
    pending_wrap: bool = False


@dataclass(frozen=True, slots=True)
class ScrollRegion:
    """Inclusive zero-based scroll margin bounds (``DECSTBM`` / ``DECSLRM``).

    Attributes
    ----------
    top : int
        Top margin row index (inclusive, ``0``-based).
    bottom : int
        Bottom margin row index (inclusive, ``0``-based).
    left : int
        Left margin column index (inclusive, ``0``-based).
    right : int
        Right margin column index (inclusive, ``0``-based).
    """

    top: int
    bottom: int
    left: int = 0
    right: int = 79


@dataclass(frozen=True, slots=True)
class Rect:
    """Zero-based cell rectangle reported to erase callbacks.

    Attributes
    ----------
    top : int
        Starting zero-based row index.
    left : int
        Starting zero-based column index.
    rows : int
        Height in rows.
    cols : int
        Width in columns.
    """

    top: int
    left: int
    rows: int
    cols: int


class EraseCause(enum.StrEnum):
    """Origin of a cell-erase event reported via :class:`~libvt.terminal.Effects`."""

    ED = "ed"
    EL = "el"
    ECH = "ech"
    SCROLL = "scroll"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class PromptZone:
    """Semantic shell prompt marker recorded via ``OSC 133``.

    Attributes
    ----------
    kind : str
        Marker code (``"A"`` prompt start, ``"B"`` command start,
        ``"C"`` command output start, ``"D"`` command finished).
    row : int
        Zero-based screen row where the marker arrived.
    col : int
        Zero-based screen column where the marker arrived.
    exit_code : int | None
        Optional integer exit status parsed from ``OSC 133 ; D ; code``.
    """

    kind: str
    row: int
    col: int
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class Cell:
    """Immutable view of one grid cell materialized lazily on indexing.

    Attributes
    ----------
    text : str
        Grapheme cluster string (``" "`` for blank, ``""`` for wide spacer).
    style : Style
        Resolved SGR graphic rendition attributes.
    width_role : WidthRole
        Narrow, wide-head, or wide-spacer column role.
    hyperlink : Hyperlink | None
        Active ``OSC 8`` hyperlink, or ``None``.
    """

    text: str = " "
    style: Style = DEFAULT_STYLE
    width_role: WidthRole = WidthRole.NARROW
    hyperlink: Hyperlink | None = None


BLANK_CELL = Cell()


def _materialize_cell(row: _Row, col: int) -> Cell:
    ch = row.chars[col]
    word = row.attrs[col]
    if ch == " " and word == 0:
        return BLANK_CELL
    ext = row.ext.get(col)
    return Cell(
        text=ch,
        style=unpack_style(word, ext),
        width_role=unpack_width_role(word),
        hyperlink=ext.hyperlink if ext is not None else None,
    )


class RowView(collections.abc.Sequence[Cell]):
    """Lazy read-only sequence view over one terminal row."""

    __slots__ = ("_row",)

    def __init__(self, row: _Row) -> None:
        self._row = row

    def __len__(self) -> int:
        """Return number of columns in the row."""
        return len(self._row.chars)

    @t.overload
    def __getitem__(self, index: int) -> Cell: ...

    @t.overload
    def __getitem__(self, index: slice) -> tuple[Cell, ...]: ...

    def __getitem__(self, index: int | slice) -> Cell | tuple[Cell, ...]:
        """Return cell or slice of cells along the row."""
        cols = len(self._row.chars)
        if isinstance(index, slice):
            return tuple(
                _materialize_cell(self._row, c) for c in range(*index.indices(cols))
            )
        col = index + cols if index < 0 else index
        if not 0 <= col < cols:
            msg = f"column index {index} out of range for {cols} columns"
            raise IndexError(msg)
        return _materialize_cell(self._row, col)

    @property
    def text(self) -> str:
        """Return written text on this row without unwritten right padding."""
        return self._row.rstrip_text()

    @property
    def wrapped(self) -> bool:
        """Return ``True`` when this row soft-wrapped onto the next row."""
        return self._row.wrapped

    def __repr__(self) -> str:
        """Return developer representation of the row view."""
        return f"RowView({self.text!r}, wrapped={self.wrapped})"


class RegionView:
    """Lazy 2D rectangular view produced by ``term[r1:r2, c1:c2]`` slicing."""

    __slots__ = ("_col_indices", "_rows")

    def __init__(
        self,
        rows: tuple[_Row, ...],
        col_indices: range,
    ) -> None:
        self._rows = rows
        self._col_indices = col_indices

    @property
    def rows(self) -> int:
        """Return height of the sliced region in rows."""
        return len(self._rows)

    @property
    def cols(self) -> int:
        """Return width of the sliced region in columns."""
        return len(self._col_indices)

    @property
    def lines(self) -> tuple[str, ...]:
        """Return one right-trimmed string per row in the region."""
        out: list[str] = []
        for r in self._rows:
            limit = min(r.used, self._col_indices.stop)
            chars = [
                r.chars[c] if c < limit else "" for c in self._col_indices if c < limit
            ]
            out.append("".join(chars))
        return tuple(out)

    @property
    def text(self) -> str:
        """Return newline-joined region text with trailing blank rows omitted."""
        lines = list(self.lines)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def __getitem__(self, key: tuple[int, int]) -> Cell:
        """Return cell at relative ``(row, col)`` within the region."""
        r_idx, c_idx = key
        r = r_idx + len(self._rows) if r_idx < 0 else r_idx
        c = c_idx + len(self._col_indices) if c_idx < 0 else c_idx
        if not 0 <= r < len(self._rows) or not 0 <= c < len(self._col_indices):
            msg = f"region index {key!r} out of range for ({self.rows}, {self.cols})"
            raise IndexError(msg)
        return _materialize_cell(self._rows[r], self._col_indices[c])

    def __repr__(self) -> str:
        """Return developer representation of the rectangular region view."""
        return f"RegionView(rows={self.rows}, cols={self.cols}, text={self.text!r})"


def slice_grid(
    rows: t.Sequence[_Row],
    total_cols: int,
    key: int | slice | tuple[slice | int, slice | int],
) -> RowView | Cell | RegionView:
    """Resolve 1D or 2D NumPy-style indexing against a sequence of :class:`_Row`."""
    total_rows = len(rows)
    if isinstance(key, int):
        r = key + total_rows if key < 0 else key
        if not 0 <= r < total_rows:
            msg = f"row index {key} out of range for {total_rows} rows"
            raise IndexError(msg)
        return RowView(rows[r])
    if isinstance(key, slice):
        selected = tuple(rows[r] for r in range(*key.indices(total_rows)))
        return RegionView(selected, range(total_cols))
    r_key, c_key = key
    if isinstance(r_key, int) and isinstance(c_key, int):
        r = r_key + total_rows if r_key < 0 else r_key
        c = c_key + total_cols if c_key < 0 else c_key
        if not 0 <= r < total_rows or not 0 <= c < total_cols:
            msg = f"cell index {key!r} out of range for ({total_rows}, {total_cols})"
            raise IndexError(msg)
        return _materialize_cell(rows[r], c)
    if isinstance(r_key, int):
        r = r_key + total_rows if r_key < 0 else r_key
        if not 0 <= r < total_rows:
            msg = f"row index {r_key} out of range for {total_rows} rows"
            raise IndexError(msg)
        selected_rows = (rows[r],)
    else:
        selected_rows = tuple(rows[r] for r in range(*r_key.indices(total_rows)))
    if isinstance(c_key, int):
        c = c_key + total_cols if c_key < 0 else c_key
        if not 0 <= c < total_cols:
            msg = f"column index {c_key} out of range for {total_cols} columns"
            raise IndexError(msg)
        col_range = range(c, c + 1)
    else:
        start, stop, step = c_key.indices(total_cols)
        if step != 1:
            msg = "column slice step must be 1"
            raise ValueError(msg)
        col_range = range(start, stop)
    return RegionView(selected_rows, col_range)


def format_rows_text(rows: t.Sequence[_Row]) -> str:
    """Join written row text, omitting trailing untouched rows."""
    last_non_empty = -1
    for idx in range(len(rows) - 1, -1, -1):
        if rows[idx].used > 0 or rows[idx].wrapped:
            last_non_empty = idx
            break
    if last_non_empty < 0:
        return ""
    return "\n".join(rows[idx].rstrip_text() for idx in range(last_non_empty + 1))


class Screen:
    """Single terminal screen grid with cursor, margins, tabstops, and history."""

    __slots__ = (
        "_cols",
        "_rows",
        "_scrollback_limit",
        "cursor",
        "history",
        "scroll_region",
        "tabstops",
    )

    def __init__(
        self,
        *,
        rows: int = 24,
        cols: int = 80,
        scrollback: int = 2_000,
    ) -> None:
        if rows < 1 or cols < 1:
            msg = f"rows and cols must be >= 1, got rows={rows}, cols={cols}"
            raise ValueError(msg)
        if scrollback < 0:
            msg = f"scrollback must be >= 0, got {scrollback}"
            raise ValueError(msg)
        self._cols = cols
        self._scrollback_limit = scrollback
        self._rows: list[_Row] = [_Row(cols) for _ in range(rows)]
        self.history: collections.deque[_Row] = collections.deque(
            maxlen=scrollback,
        )
        self.cursor = Cursor()
        self.scroll_region = ScrollRegion(
            top=0,
            bottom=rows - 1,
            left=0,
            right=cols - 1,
        )
        self.tabstops: set[int] = set(range(8, cols, 8))

    @property
    def rows(self) -> int:
        """Return screen height in rows."""
        return len(self._rows)

    @property
    def cols(self) -> int:
        """Return screen width in columns."""
        return self._cols

    @property
    def lines(self) -> tuple[str, ...]:
        """Return one right-trimmed string for each visible row."""
        return tuple(r.rstrip_text() for r in self._rows)

    @property
    def text(self) -> str:
        """Return visible screen text with trailing blank rows omitted."""
        return format_rows_text(self._rows)

    @property
    def history_lines(self) -> tuple[str, ...]:
        """Return scrollback lines ordered oldest to newest."""
        return tuple(r.rstrip_text() for r in self.history)

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

    def scroll_up(
        self,
        count: int = 1,
        *,
        top: int | None = None,
        bottom: int | None = None,
        left: int = 0,
        right: int | None = None,
        save_history: bool = False,
        bce_word: int = 0,
        bce_ext: _CellExt | None = None,
    ) -> None:
        """Scroll region ``[top..bottom, left..right]`` upward by *count* rows."""
        t_row = self.scroll_region.top if top is None else top
        b_row = self.scroll_region.bottom if bottom is None else bottom
        r_col = (self._cols - 1) if right is None else right
        if count <= 0 or t_row > b_row:
            return
        span = b_row - t_row + 1
        shift = min(count, span)
        if left == 0 and r_col == self._cols - 1:
            if save_history and t_row == 0 and self._scrollback_limit > 0:
                for idx in range(shift):
                    self.history.append(self._rows[t_row + idx].clone())
            for dst in range(t_row, b_row - shift + 1):
                self._rows[dst] = self._rows[dst + shift]
            for dst in range(b_row - shift + 1, b_row + 1):
                blank = _Row(self._cols)
                if bce_word != 0 or bce_ext is not None:
                    blank.clear_range(0, self._cols, bce_word, bce_ext)
                self._rows[dst] = blank
            return

        # Rectangular vertical scroll when DECLRMM left/right margins are narrowed.
        for dst in range(t_row, b_row - shift + 1):
            src_row = self._rows[dst + shift]
            dst_row = self._rows[dst]
            dst_row._heal_wide_boundary(left)
            dst_row._heal_wide_boundary(r_col)
            src_row._heal_wide_boundary(left)
            src_row._heal_wide_boundary(r_col)
            for c in range(left, r_col + 1):
                dst_row.chars[c] = src_row.chars[c]
                dst_row.attrs[c] = src_row.attrs[c]
                if c in src_row.ext:
                    dst_row.ext[c] = src_row.ext[c]
                else:
                    dst_row.ext.pop(c, None)
            dst_row.used = max(dst_row.used, min(src_row.used, r_col + 1))
        for dst in range(b_row - shift + 1, b_row + 1):
            self._rows[dst].clear_range(left, r_col + 1, bce_word, bce_ext)

    def scroll_down(
        self,
        count: int = 1,
        *,
        top: int | None = None,
        bottom: int | None = None,
        left: int = 0,
        right: int | None = None,
        bce_word: int = 0,
        bce_ext: _CellExt | None = None,
    ) -> None:
        """Scroll region ``[top..bottom, left..right]`` downward by *count* rows."""
        t_row = self.scroll_region.top if top is None else top
        b_row = self.scroll_region.bottom if bottom is None else bottom
        r_col = (self._cols - 1) if right is None else right
        if count <= 0 or t_row > b_row:
            return
        span = b_row - t_row + 1
        shift = min(count, span)
        if left == 0 and r_col == self._cols - 1:
            for dst in range(b_row, t_row + shift - 1, -1):
                self._rows[dst] = self._rows[dst - shift]
            for dst in range(t_row, t_row + shift):
                blank = _Row(self._cols)
                if bce_word != 0 or bce_ext is not None:
                    blank.clear_range(0, self._cols, bce_word, bce_ext)
                self._rows[dst] = blank
            return

        for dst in range(b_row, t_row + shift - 1, -1):
            src_row = self._rows[dst - shift]
            dst_row = self._rows[dst]
            dst_row._heal_wide_boundary(left)
            dst_row._heal_wide_boundary(r_col)
            src_row._heal_wide_boundary(left)
            src_row._heal_wide_boundary(r_col)
            for c in range(left, r_col + 1):
                dst_row.chars[c] = src_row.chars[c]
                dst_row.attrs[c] = src_row.attrs[c]
                if c in src_row.ext:
                    dst_row.ext[c] = src_row.ext[c]
                else:
                    dst_row.ext.pop(c, None)
            dst_row.used = max(dst_row.used, min(src_row.used, r_col + 1))
        for dst in range(t_row, t_row + shift):
            self._rows[dst].clear_range(left, r_col + 1, bce_word, bce_ext)

    def resize(self, *, rows: int, cols: int) -> None:
        """Resize screen grid in place to ``(rows, cols)``."""
        if rows < 1 or cols < 1:
            msg = f"rows and cols must be >= 1, got rows={rows}, cols={cols}"
            raise ValueError(msg)
        for r in self._rows:
            r.resize(cols)
        cur_rows = len(self._rows)
        if rows < cur_rows:
            del self._rows[rows:]
        elif rows > cur_rows:
            self._rows.extend(_Row(cols) for _ in range(rows - cur_rows))
        self._cols = cols
        self.scroll_region = ScrollRegion(
            top=0,
            bottom=rows - 1,
            left=0,
            right=cols - 1,
        )
        self.tabstops = set(range(8, cols, 8))
        self.cursor = Cursor(
            row=min(self.cursor.row, rows - 1),
            col=min(self.cursor.col, cols - 1),
            visible=self.cursor.visible,
            shape=self.cursor.shape,
            blinking=self.cursor.blinking,
            pending_wrap=False,
        )

    def reset(self) -> None:
        """Clear all visible rows, margins, cursor, and default tabstops."""
        rows = len(self._rows)
        self._rows = [_Row(self._cols) for _ in range(rows)]
        self.cursor = Cursor()
        self.scroll_region = ScrollRegion(
            top=0,
            bottom=rows - 1,
            left=0,
            right=self._cols - 1,
        )
        self.tabstops = set(range(8, self._cols, 8))


class ScreenSet:
    """Dual primary and alternate (``?1049``) screen pair."""

    __slots__ = ("active_is_alt", "alternate", "primary")

    def __init__(
        self,
        *,
        rows: int = 24,
        cols: int = 80,
        scrollback: int = 2_000,
    ) -> None:
        self.primary = Screen(rows=rows, cols=cols, scrollback=scrollback)
        self.alternate = Screen(rows=rows, cols=cols, scrollback=0)
        self.active_is_alt: bool = False

    @property
    def active(self) -> Screen:
        """Return the currently active :class:`Screen`."""
        return self.alternate if self.active_is_alt else self.primary

    def resize(self, *, rows: int, cols: int) -> None:
        """Resize both primary and alternate screens."""
        self.primary.resize(rows=rows, cols=cols)
        self.alternate.resize(rows=rows, cols=cols)


__all__ = [
    "BLANK_CELL",
    "Cell",
    "Cursor",
    "CursorShape",
    "EraseCause",
    "PromptZone",
    "Rect",
    "RegionView",
    "RowView",
    "Screen",
    "ScreenSet",
    "ScrollRegion",
    "WidthRole",
]
