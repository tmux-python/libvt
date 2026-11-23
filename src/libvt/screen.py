"""Zero-based screen buffer, lazy cell/row/region views, reflow, and dual screens."""

from __future__ import annotations

import bisect
import collections.abc
import enum
import operator
import sys
import typing as t
from dataclasses import dataclass

from libvt._grid import (
    _EMPTY_FROZEN_HISTORY,
    _PEN_BASE_MASK,
    _WIDTH_CLEAR_MASK,
    FrozenRow,
    RowLike,
    _CellExt,
    _FrozenHistory,
    _HistoryBuffer,
    _Row,
    bump_row_gen,
    raw_width_bits,
    unpack_style,
    unpack_width_role,
)
from libvt._ucd import WidthRole
from libvt.graphics import GraphicsTracker, ImageMeta, ImagePlacement
from libvt.input import KittyKeyboardStack
from libvt.style import DEFAULT_STYLE, Hyperlink, Style

_ITEM0 = operator.itemgetter(0)


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
    left: int
    right: int


@dataclass(frozen=True, slots=True)
class Cell:
    """Immutable view of a single terminal grid cell.

    Attributes
    ----------
    text : str
        Grapheme cluster string (``" "`` for narrow blanks, ``""`` for
        :attr:`WidthRole.SPACER_TAIL` and :attr:`WidthRole.SPACER_HEAD`).
    style : Style
        Unpacked SGR rendition attributes.
    width_role : WidthRole
        Column occupancy role (narrow, wide lead, or spacer).
    hyperlink : Hyperlink | None
        Active ``OSC 8`` hyperlink, when present.
    protected : bool
        ``DECSCA`` protection attribute respected by ``DECSED`` / ``DECSEL``.
    """

    text: str = " "
    style: Style = DEFAULT_STYLE
    width_role: WidthRole = WidthRole.NARROW
    hyperlink: Hyperlink | None = None
    protected: bool = False

    @property
    def width(self) -> int:
        """Return column width (``2`` wide, ``0`` spacer, ``1`` narrow)."""
        if self.width_role == WidthRole.WIDE:
            return 2
        if self.width_role in (WidthRole.SPACER_TAIL, WidthRole.SPACER_HEAD):
            return 0
        return 1

    @property
    def is_blank(self) -> bool:
        """Return ``True`` when the cell holds an unstyled narrow space."""
        return (
            self.text == " "
            and self.style == DEFAULT_STYLE
            and self.width_role == WidthRole.NARROW
            and self.hyperlink is None
        )


BLANK_CELL = Cell()


@dataclass(frozen=True, slots=True)
class RowRun:
    """Contiguous run of identically styled cells within a :class:`RowView`.

    Attributes
    ----------
    start_col : int
        Inclusive zero-based start column.
    end_col : int
        Exclusive zero-based end column.
    text : str
        Rendered text across ``start_col..end_col`` (omitting spacer empty strings).
    style : Style
        Shared :class:`~libvt.style.Style` across the run.
    hyperlink : Hyperlink | None
        Shared ``OSC 8`` hyperlink across the run, if any.
    """

    start_col: int
    end_col: int
    text: str
    style: Style = DEFAULT_STYLE
    hyperlink: Hyperlink | None = None


def _materialize_cell(row: RowLike, col: int) -> Cell:
    ch = row.chars[col] if isinstance(row, _Row) else row.char_at(col)
    word = row.attr_at(col)
    if word == 0 and ch == " ":
        return BLANK_CELL
    ext = row.ext_at(col)
    style = unpack_style(word, ext)
    role = unpack_width_role(word)
    hyperlink = ext.hyperlink if ext is not None else None
    protected = bool(ext is not None and ext.protected)
    return Cell(
        text=ch,
        style=style,
        width_role=role,
        hyperlink=hyperlink,
        protected=protected,
    )


def _normalize_index(idx: int, length: int, *, axis: str) -> int:
    norm = idx + length if idx < 0 else idx
    if norm < 0 or norm >= length:
        msg = f"{axis} index {idx} out of range for size {length}"
        raise IndexError(msg)
    return norm


def _slice_indices(s: slice, length: int) -> range:
    start, stop, step = s.indices(length)
    if step != 1:
        msg = "grid slices only support step=1"
        raise ValueError(msg)
    return range(start, stop)


def _frozen_row_runs(
    row: FrozenRow,
    start_c: int,
    limit: int,
) -> tuple[RowRun, ...]:
    """Coalesce ``(style, hyperlink)`` spans on *row* in ``O(runs + exts)``."""
    if not row.runs and not row.exts and row.cells is None:
        t_len = len(row.text)
        if limit <= t_len:
            txt = row.text[start_c:limit]
        elif start_c >= t_len:
            txt = " " * (limit - start_c)
        else:
            txt = row.text[start_c:t_len] + (" " * (limit - t_len))
        return (
            RowRun(
                start_col=start_c,
                end_col=limit,
                text=txt,
                style=DEFAULT_STYLE,
                hyperlink=None,
            ),
        )

    out: list[RowRun] = []
    runs = row.runs
    exts = row.exts
    r_len = len(runs)
    e_len = len(exts)
    r_idx = bisect.bisect_right(runs, start_c, key=_ITEM0) if r_len else 0
    e_idx = bisect.bisect_left(exts, start_c, key=_ITEM0) if e_len else 0

    has_cur = False
    run_start = start_c
    cur_style = DEFAULT_STYLE
    cur_link: Hyperlink | None = None
    buf: list[str] = []
    pos = start_c

    while pos < limit:
        if r_idx < r_len:
            raw_r_end, word = runs[r_idx]
            r_end = min(limit, raw_r_end)
        else:
            raw_r_end = limit + 1
            word = 0
            r_end = limit

        while e_idx < e_len and exts[e_idx][0] < pos:
            e_idx += 1
        if e_idx >= e_len:
            ext: _CellExt | None = None
            e_end = limit
        elif exts[e_idx][0] > pos:
            ext = None
            e_end = min(limit, exts[e_idx][0])
        else:
            ext = exts[e_idx][1]
            scan_c = pos + 1
            scan_i = e_idx + 1
            while (
                scan_c < r_end
                and scan_i < e_len
                and exts[scan_i][0] == scan_c
                and exts[scan_i][1] == ext
            ):
                scan_c += 1
                scan_i += 1
            e_end = scan_c
            e_idx = scan_i

        seg_end = min(r_end, e_end)
        if seg_end == raw_r_end:
            r_idx += 1

        role_bits = raw_width_bits(word)
        if role_bits in (int(WidthRole.SPACER_TAIL), int(WidthRole.SPACER_HEAD)):
            if not has_cur:
                cur_style = unpack_style(word, ext)
                cur_link = ext.hyperlink if ext is not None else None
                run_start = pos
                has_cur = True
            pos = seg_end
            continue

        if row.cells is not None:
            c_len = len(row.cells)
            if seg_end <= c_len:
                seg_txt = "".join(row.cells[pos:seg_end])
            elif pos >= c_len:
                seg_txt = " " * (seg_end - pos)
            else:
                seg_txt = "".join(row.cells[pos:c_len]) + (" " * (seg_end - c_len))
        else:
            t_len = len(row.text)
            if seg_end <= t_len:
                seg_txt = row.text[pos:seg_end]
            elif pos >= t_len:
                seg_txt = " " * (seg_end - pos)
            else:
                seg_txt = row.text[pos:t_len] + (" " * (seg_end - t_len))

        st = unpack_style(word, ext)
        lnk = ext.hyperlink if ext is not None else None
        if not has_cur:
            run_start = pos
            cur_style = st
            cur_link = lnk
            buf = [seg_txt]
            has_cur = True
        elif st != cur_style or lnk != cur_link:
            out.append(
                RowRun(
                    start_col=run_start,
                    end_col=pos,
                    text="".join(buf),
                    style=cur_style,
                    hyperlink=cur_link,
                ),
            )
            run_start = pos
            cur_style = st
            cur_link = lnk
            buf = [seg_txt]
        else:
            buf.append(seg_txt)
        pos = seg_end

    out.append(
        RowRun(
            start_col=run_start,
            end_col=limit,
            text="".join(buf),
            style=cur_style,
            hyperlink=cur_link,
        ),
    )
    return tuple(out)


class RowView(collections.abc.Sequence[Cell]):
    """Read-only sequence view over one live viewport or scrollback row."""

    __slots__ = ("_col_range", "_row", "_row_idx", "_screen")

    def __init__(
        self,
        row: RowLike,
        col_range: range | None = None,
        *,
        screen: Screen | None = None,
        row_idx: int = -1,
    ) -> None:
        self._row = row
        self._screen = screen
        self._row_idx = row_idx
        total_cols = (
            screen.cols
            if screen is not None
            else (len(row.chars) if isinstance(row, _Row) else row.cols)
        )
        self._col_range = range(total_cols) if col_range is None else col_range

    def _current_row(self) -> RowLike:
        if self._screen is not None and 0 <= self._row_idx < len(self._screen._rows):
            return self._screen._rows[self._row_idx]
        return self._row

    @property
    def wrapped(self) -> bool:
        """Return ``True`` when this row soft-wrapped onto the next line."""
        return self._current_row().wrapped

    @property
    def text(self) -> str:
        """Return right-trimmed rendered text for this row slice."""
        row = self._current_row()
        row_t_used = (
            row.text_used
            if isinstance(row, _Row)
            else (len(row.cells) if row.cells is not None else len(row.text))
        )
        if self._col_range.start == 0 and self._col_range.stop >= row_t_used:
            return row.rstrip_text()
        limit = min(self._col_range.stop, row_t_used)
        if limit <= self._col_range.start:
            return ""
        if isinstance(row, _Row):
            raw = "".join(row.chars[self._col_range.start : limit])
        else:
            raw = "".join(row.char_at(c) for c in range(self._col_range.start, limit))
        return raw.rstrip(" ")

    @property
    def runs(self) -> tuple[RowRun, ...]:
        """Return coalesced ``(style, hyperlink)`` spans up to ``used`` columns."""
        row = self._current_row()
        limit = min(self._col_range.stop, row.used)
        start_c = self._col_range.start
        if limit <= start_c:
            return ()
        if isinstance(row, FrozenRow):
            return _frozen_row_runs(row, start_c, limit)
        out: list[RowRun] = []
        run_start = start_c
        first_ext = row.ext_at(start_c)
        cur_style = unpack_style(row.attr_at(start_c), first_ext)
        cur_link = first_ext.hyperlink if first_ext is not None else None
        buf: list[str] = []

        for c in range(start_c, limit):
            word = row.attr_at(c)
            role_bits = raw_width_bits(word)
            if role_bits in (int(WidthRole.SPACER_TAIL), int(WidthRole.SPACER_HEAD)):
                continue
            ext = row.ext_at(c)
            st = unpack_style(word, ext)
            lnk = ext.hyperlink if ext is not None else None
            ch = row.chars[c] if isinstance(row, _Row) else row.char_at(c)
            if st != cur_style or lnk != cur_link:
                out.append(
                    RowRun(
                        start_col=run_start,
                        end_col=c,
                        text="".join(buf),
                        style=cur_style,
                        hyperlink=cur_link,
                    ),
                )
                run_start = c
                cur_style = st
                cur_link = lnk
                buf = [ch]
            else:
                buf.append(ch)

        out.append(
            RowRun(
                start_col=run_start,
                end_col=limit,
                text="".join(buf),
                style=cur_style,
                hyperlink=cur_link,
            ),
        )
        return tuple(out)

    def __len__(self) -> int:
        """Return column count in this view."""
        return len(self._col_range)

    @t.overload
    def __getitem__(self, index: int) -> Cell: ...

    @t.overload
    def __getitem__(self, index: slice) -> RowView: ...

    def __getitem__(self, index: int | slice) -> Cell | RowView:
        """Index one :class:`Cell` or slice a sub-range :class:`RowView`."""
        row = self._current_row()
        if isinstance(index, slice):
            sub = _slice_indices(index, len(self._col_range))
            base = self._col_range.start
            return RowView(
                row,
                range(base + sub.start, base + sub.stop),
                screen=self._screen,
                row_idx=self._row_idx,
            )
        norm = _normalize_index(index, len(self._col_range), axis="col")
        return _materialize_cell(row, self._col_range.start + norm)

    def __eq__(self, other: object) -> bool:
        """Compare two :class:`RowView` instances by wrapped state and cells."""
        if self is other:
            return True
        if not isinstance(other, RowView):
            return NotImplemented
        if len(self._col_range) != len(other._col_range):
            return False
        r1 = self._current_row()
        r2 = other._current_row()
        if r1 is r2 and self._col_range == other._col_range:
            return True
        if (
            self._col_range == other._col_range
            and self._col_range.start == 0
            and self._col_range.step == 1
        ):
            f1 = r1.freeze() if isinstance(r1, _Row) else r1
            f2 = r2.freeze() if isinstance(r2, _Row) else r2
            if self._col_range.stop >= max(f1.used, f2.used):
                return _frozen_content_key(f1) == _frozen_content_key(f2)
        if r1.wrapped != r2.wrapped:
            return False
        return all(a == b for a, b in zip(self, other, strict=True))


def _frozen_content_key(
    fr: FrozenRow,
) -> tuple[
    bool,
    str,
    tuple[tuple[int, int], ...],
    tuple[tuple[int, _CellExt], ...],
    tuple[str, ...] | None,
]:
    return (
        fr.wrapped,
        fr.text,
        fr.runs,
        fr.exts,
        fr.cells,
    )


def _row_view_to_full_frozen(value: object) -> tuple[int, FrozenRow] | None:
    if not isinstance(value, RowView):
        return None
    r = value._current_row()
    fr = r.freeze() if isinstance(r, _Row) else r
    v_cols = len(value._col_range)
    if value._col_range.start == 0 and value._col_range.step == 1 and v_cols >= fr.used:
        return v_cols, fr
    return None


class _HistoryView(collections.abc.Sequence[RowView]):
    """Lazy ``O(1)`` immutable sequence view over scrollback :class:`RowView` lines."""

    __slots__ = ("_cols", "_data", "_range")

    def __init__(
        self,
        data: _FrozenHistory | None = None,
        index_range: range | None = None,
        *,
        cols: int | None = None,
    ) -> None:
        eff = _EMPTY_FROZEN_HISTORY if data is None else data
        self._data = eff
        self._range = range(len(eff)) if index_range is None else index_range
        self._cols = cols

    def _eff_cols(self, fr: FrozenRow) -> int:
        if self._cols is None:
            return fr.cols
        return max(self._cols, fr.used)

    def _wrap_row(self, fr: FrozenRow) -> RowView:
        return RowView(fr, range(self._eff_cols(fr)))

    def __len__(self) -> int:
        """Return number of scrollback rows in this view."""
        return len(self._range)

    def __bool__(self) -> bool:
        """Return ``True`` when at least one scrollback row is present."""
        return bool(self._range)

    @t.overload
    def __getitem__(self, index: int) -> RowView: ...

    @t.overload
    def __getitem__(self, index: slice) -> _HistoryView: ...

    def __getitem__(self, index: int | slice) -> RowView | _HistoryView:
        """Index one :class:`RowView` in ``O(1)`` or slice a sub-view."""
        if isinstance(index, slice):
            return _HistoryView(self._data, self._range[index], cols=self._cols)
        if isinstance(index, bool) or not isinstance(index, int):
            msg = f"history indices must be int or slice, got {type(index).__name__}"
            raise TypeError(msg)
        return self._wrap_row(self._data.at(self._range[index]))

    def __contains__(self, value: object) -> bool:
        """Return ``True`` when *value* matches a scrollback :class:`RowView`."""
        if not isinstance(value, RowView):
            return False
        v_len = len(value)
        if self._cols is not None and v_len < self._cols:
            return False
        full = _row_view_to_full_frozen(value)
        r = self._range
        if full is None:
            at = self._data.at
            return any(
                self._eff_cols(fr := at(idx)) == v_len and self._wrap_row(fr) == value
                for idx in r
            )
        v_cols, target_fr = full
        target_key = _frozen_content_key(target_fr)
        if r.start == 0 and r.stop == len(self._data) and r.step == 1:
            return any(
                self._eff_cols(fr) == v_cols and _frozen_content_key(fr) == target_key
                for fr in self._data
            )
        at = self._data.at
        return any(
            self._eff_cols(fr := at(idx)) == v_cols
            and _frozen_content_key(fr) == target_key
            for idx in r
        )

    def count(self, value: object) -> int:
        """Return count of scrollback rows equal to *value*."""
        if not isinstance(value, RowView):
            return 0
        v_len = len(value)
        if self._cols is not None and v_len < self._cols:
            return 0
        full = _row_view_to_full_frozen(value)
        at = self._data.at
        if full is None:
            return sum(
                1
                for idx in self._range
                if self._eff_cols(fr := at(idx)) == v_len
                and self._wrap_row(fr) == value
            )
        v_cols, target_fr = full
        target_key = _frozen_content_key(target_fr)
        return sum(
            1
            for idx in self._range
            if self._eff_cols(fr := at(idx)) == v_cols
            and _frozen_content_key(fr) == target_key
        )

    def index(
        self,
        value: object,
        start: int = 0,
        stop: int = sys.maxsize,
    ) -> int:
        """Return first zero-based index of *value* in ``[start, stop)``."""
        if isinstance(value, RowView):
            v_len = len(value)
            if self._cols is None or v_len >= self._cols:
                sub_indices = range(len(self._range))[start:stop]
                full = _row_view_to_full_frozen(value)
                at = self._data.at
                if full is None:
                    for pos in sub_indices:
                        fr = at(self._range[pos])
                        if self._eff_cols(fr) == v_len and self._wrap_row(fr) == value:
                            return pos
                else:
                    v_cols, target_fr = full
                    target_key = _frozen_content_key(target_fr)
                    for pos in sub_indices:
                        fr = at(self._range[pos])
                        if (
                            self._eff_cols(fr) == v_cols
                            and _frozen_content_key(fr) == target_key
                        ):
                            return pos
        msg = "RowView is not in history"
        raise ValueError(msg)

    def __iter__(self) -> collections.abc.Iterator[RowView]:
        """Yield :class:`RowView` items across this view."""
        r = self._range
        if not r:
            return
        if r.start == 0 and r.stop == len(self._data) and r.step == 1:
            for fr in self._data:
                yield self._wrap_row(fr)
            return
        at = self._data.at
        for idx in r:
            yield self._wrap_row(at(idx))

    def __reversed__(self) -> collections.abc.Iterator[RowView]:
        """Yield :class:`RowView` items in reverse order."""
        at = self._data.at
        for idx in reversed(self._range):
            yield self._wrap_row(at(idx))

    def __eq__(self, other: object) -> bool:
        """Compare two :class:`_HistoryView` instances by view-width rows."""
        if self is other:
            return True
        if not isinstance(other, _HistoryView):
            return NotImplemented
        if len(self._range) != len(other._range):
            return False
        if not self._range:
            return True
        if (
            self._data is other._data
            and self._range == other._range
            and self._cols == other._cols
        ):
            return True
        return all(
            self._eff_cols(f1 := self._data.at(i))
            == other._eff_cols(f2 := other._data.at(j))
            and _frozen_content_key(f1) == _frozen_content_key(f2)
            for i, j in zip(self._range, other._range, strict=True)
        )

    def __repr__(self) -> str:
        """Return concise summary representation."""
        return f"HistoryView(len={len(self._range)})"


class RegionView(collections.abc.Sequence[RowView]):
    """Read-only 2D rectangular slice view over ``(rows, cols)``."""

    __slots__ = ("_col_range", "_rows")

    def __init__(
        self, rows: collections.abc.Sequence[RowLike], col_range: range
    ) -> None:
        self._rows = tuple(rows)
        self._col_range = col_range

    @property
    def rows(self) -> int:
        """Return height of this region in rows."""
        return len(self._rows)

    @property
    def cols(self) -> int:
        """Return width of this region in columns."""
        return len(self._col_range)

    @property
    def lines(self) -> tuple[str, ...]:
        """Return right-trimmed text for each row in the region."""
        return tuple(RowView(r, self._col_range).text for r in self._rows)

    @property
    def text(self) -> str:
        """Return newline-joined text with trailing empty rows omitted."""
        lines = list(self.lines)
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    def __len__(self) -> int:
        """Return number of rows in this region."""
        return len(self._rows)

    @t.overload
    def __getitem__(self, key: int) -> RowView: ...

    @t.overload
    def __getitem__(self, key: slice) -> RegionView: ...

    @t.overload
    def __getitem__(self, key: tuple[int, int]) -> Cell: ...

    def __getitem__(
        self,
        key: int | slice | tuple[int, int],
    ) -> RowView | RegionView | Cell:
        """Index a row, sub-region, or ``(row, col)`` cell within this region."""
        if isinstance(key, tuple):
            r_idx, c_idx = key
            nr = _normalize_index(r_idx, len(self._rows), axis="row")
            nc = _normalize_index(c_idx, len(self._col_range), axis="col")
            return _materialize_cell(self._rows[nr], self._col_range.start + nc)
        if isinstance(key, slice):
            sub = _slice_indices(key, len(self._rows))
            return RegionView(self._rows[sub.start : sub.stop], self._col_range)
        nr = _normalize_index(key, len(self._rows), axis="row")
        return RowView(self._rows[nr], self._col_range)


def format_rows_text(rows: collections.abc.Sequence[RowLike]) -> str:
    """Format *rows* as newline-joined text omitting trailing blank lines."""
    lines = [r.rstrip_text() for r in rows]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def slice_grid(
    rows: collections.abc.Sequence[RowLike],
    cols: int,
    key: int | slice | tuple[slice | int, slice | int],
    *,
    screen: Screen | None = None,
) -> RowView | Cell | RegionView:
    """Implement ``grid[r]``, ``grid[r, c]``, and ``grid[r1:r2, c1:c2]``."""
    height = len(rows)
    if isinstance(key, int):
        nr = _normalize_index(key, height, axis="row")
        return RowView(rows[nr], screen=screen, row_idx=nr)
    if isinstance(key, slice):
        r_range = _slice_indices(key, height)
        return RegionView(rows[r_range.start : r_range.stop], range(cols))

    r_key, c_key = key
    if isinstance(r_key, int) and isinstance(c_key, int):
        nr = _normalize_index(r_key, height, axis="row")
        nc = _normalize_index(c_key, cols, axis="col")
        return _materialize_cell(rows[nr], nc)
    if isinstance(r_key, int) and isinstance(c_key, slice):
        nr = _normalize_index(r_key, height, axis="row")
        c_range = _slice_indices(c_key, cols)
        return RegionView((rows[nr],), c_range)
    if isinstance(r_key, slice) and isinstance(c_key, int):
        r_range = _slice_indices(r_key, height)
        nc = _normalize_index(c_key, cols, axis="col")
        return RegionView(rows[r_range.start : r_range.stop], range(nc, nc + 1))
    if isinstance(r_key, slice) and isinstance(c_key, slice):
        r_range = _slice_indices(r_key, height)
        c_range = _slice_indices(c_key, cols)
        return RegionView(rows[r_range.start : r_range.stop], c_range)
    msg = f"unsupported grid index {key!r}"
    raise TypeError(msg)


class Screen:
    """Mutable 2D character grid with ``FrozenRow`` RLE scrollback and reflow."""

    __slots__ = (
        "_cell_height_px",
        "_cols",
        "_cur_blinking",
        "_cur_col",
        "_cur_row",
        "_cur_shape",
        "_cur_visible",
        "_evicted_total",
        "_gfx",
        "_history",
        "_history_clear_at",
        "_history_clear_seq",
        "_is_alt",
        "_max_image_dimension",
        "_max_images",
        "_max_placements",
        "_pending_wrap",
        "_rows",
        "_scrolled_total",
        "_tabstops",
        "_tabstops_sorted",
        "keyboard_stack",
        "scroll_region",
    )

    def __init__(
        self,
        *,
        rows: int = 24,
        cols: int = 80,
        scrollback: int = 2_000,
        keyboard_stack_depth: int = 8,
        cell_height_px: int = 16,
        max_images: int = 256,
        max_placements: int = 1_024,
        max_image_dimension: int = 10_000,
        is_alt: bool = False,
    ) -> None:
        if rows < 1 or cols < 1:
            msg = f"rows and cols must be >= 1, got ({rows}, {cols})"
            raise ValueError(msg)
        self._cols = cols
        self._rows: list[_Row] = [_Row(cols) for _ in range(rows)]
        self._history: _HistoryBuffer = _HistoryBuffer(maxlen=max(0, scrollback))
        self._scrolled_total: int = 0
        self._evicted_total: int = 0
        self._history_clear_at: int = 0
        self._history_clear_seq: int = 0
        self._is_alt: bool = is_alt
        self._cell_height_px: int = max(1, cell_height_px)
        self._max_images: int = max_images
        self._max_placements: int = max_placements
        self._max_image_dimension: int = max_image_dimension
        self._gfx: GraphicsTracker | None = None
        self._cur_row: int = 0
        self._cur_col: int = 0
        self._cur_visible: bool = True
        self._cur_shape: CursorShape = CursorShape.BLOCK
        self._cur_blinking: bool = True
        self._pending_wrap: bool = False
        self.scroll_region = ScrollRegion(
            top=0,
            bottom=rows - 1,
            left=0,
            right=cols - 1,
        )
        self._tabstops: set[int] = set(range(8, cols, 8))
        self._tabstops_sorted: list[int] = sorted(self._tabstops)
        self.keyboard_stack = KittyKeyboardStack(max_depth=keyboard_stack_depth)

    @property
    def rows(self) -> int:
        """Return visible screen height in rows."""
        return len(self._rows)

    @property
    def cols(self) -> int:
        """Return screen width in columns."""
        return self._cols

    @property
    def scrolled_total(self) -> int:
        """Return cumulative primary rows scrolled off the top margin."""
        return self._scrolled_total

    @property
    def cursor(self) -> Cursor:
        """Return current zero-based :class:`Cursor`."""
        return Cursor(
            row=self._cur_row,
            col=self._cur_col,
            visible=self._cur_visible,
            shape=self._cur_shape,
            blinking=self._cur_blinking,
            pending_wrap=self._pending_wrap,
        )

    @cursor.setter
    def cursor(self, value: Cursor) -> None:
        self._cur_row = value.row
        self._cur_col = value.col
        self._cur_visible = value.visible
        self._cur_shape = value.shape
        self._cur_blinking = value.blinking
        self._pending_wrap = value.pending_wrap

    @property
    def history(self) -> collections.abc.Sequence[RowView]:
        """Return ``O(1)`` immutable :class:`RowView` sequence over scrollback."""
        return _HistoryView(self._history.freeze(), cols=self._cols)

    @property
    def tabstops(self) -> tuple[int, ...]:
        """Return sorted horizontal tabstop columns."""
        return tuple(self._tabstops_sorted)

    def add_tabstop(self, col: int) -> None:
        """Set a horizontal tabstop at *col* (``HTS``)."""
        if 0 <= col < self._cols and col not in self._tabstops:
            self._tabstops.add(col)
            self._tabstops_sorted = sorted(self._tabstops)

    def clear_tabstop(self, col: int) -> None:
        """Remove the horizontal tabstop at *col* (``TBC 0``)."""
        if col in self._tabstops:
            self._tabstops.discard(col)
            self._tabstops_sorted = sorted(self._tabstops)

    def clear_all_tabstops(self) -> None:
        """Remove all horizontal tabstops (``TBC 3``)."""
        self._tabstops.clear()
        self._tabstops_sorted.clear()

    def clear_history(self) -> None:
        """Clear all sealed scrollback rows (``ED 3``)."""
        self._evicted_total += len(self._history)
        self._history.clear()
        self._history_clear_at = self._scrolled_total
        self._history_clear_seq += 1
        if self._gfx is not None:
            self._gfx.clear_history(self._scrolled_total)

    def ensure_graphics(self) -> GraphicsTracker:
        """Return (lazily creating) this screen's :class:`GraphicsTracker`."""
        if self._gfx is None:
            self._gfx = GraphicsTracker(
                max_images=self._max_images,
                max_placements=self._max_placements,
                max_image_dimension=self._max_image_dimension,
            )
        return self._gfx

    @property
    def images(self) -> dict[int, ImageMeta]:
        """Return stored ``SIXEL`` / Kitty :class:`ImageMeta` records."""
        return self._gfx.images if self._gfx is not None else {}

    @property
    def placements(self) -> tuple[ImagePlacement, ...]:
        """Return active + scrollback :class:`ImagePlacement` records."""
        if self._gfx is None:
            return ()
        return self._gfx.snapshot_placements()

    def _append_history(self, frozen: FrozenRow) -> None:
        if self._history.append(frozen):
            self._evicted_total += 1

    def _clear_row_spacer_head(self, row_obj: _Row) -> bool:
        if not row_obj.has_wide or row_obj.attrs is None:
            return False
        last = self._cols - 1
        if raw_width_bits(row_obj.attrs[last]) == int(WidthRole.SPACER_HEAD):
            row_obj.chars[last] = " "
            row_obj.attrs[last] &= _WIDTH_CLEAR_MASK
            if row_obj.text_used == last + 1:
                row_obj.text_used = last
                while (
                    row_obj.text_used > 0
                    and row_obj.chars[row_obj.text_used - 1] == " "
                    and row_obj.attr_at(row_obj.text_used - 1) == 0
                    and row_obj.ext_at(row_obj.text_used - 1) is None
                ):
                    row_obj.text_used -= 1
            while (
                row_obj.used > 0
                and row_obj.chars[row_obj.used - 1] == " "
                and row_obj.attr_at(row_obj.used - 1) == 0
                and row_obj.ext_at(row_obj.used - 1) is None
            ):
                row_obj.used -= 1
            bump_row_gen(row_obj)
            return True
        return False

    def heal_before_edit(self, row: int, col_start: int, col_end: int) -> None:
        """Repair cross-row ``SPACER_HEAD`` pairs before mutating cells."""
        if not (0 <= row < len(self._rows)):
            return
        if col_start <= 1 and row > 0:
            prev = self._rows[row - 1]
            if self._clear_row_spacer_head(prev):
                prev.wrapped = False
                self._rows[row]._heal_wide_boundary(0)
        if col_end > self._cols - 1:
            cur = self._rows[row]
            if self._clear_row_spacer_head(cur):
                cur.wrapped = False
                if row + 1 < len(self._rows):
                    self._rows[row + 1]._heal_wide_boundary(0)

    def heal_wrapped_spacer_head(self, row: int) -> None:
        """Clear preceding ``SPACER_HEAD`` and ``wrapped`` if ``(row, 0)`` splits."""
        if row <= 0 or row > len(self._rows):
            return
        prev = self._rows[row - 1]
        if not prev.has_wide or prev.attrs is None:
            return
        if row == len(self._rows) or not prev.wrapped:
            if self._clear_row_spacer_head(prev):
                prev.wrapped = False
            return
        cur = self._rows[row]
        if raw_width_bits(cur.attr_at(0)) != int(
            WidthRole.WIDE
        ) and self._clear_row_spacer_head(prev):
            prev.wrapped = False

    @property
    def lines(self) -> tuple[str, ...]:
        """Return right-trimmed text for every visible row."""
        return tuple(r.rstrip_text() for r in self._rows)

    @property
    def text(self) -> str:
        """Return visible screen text with trailing blank rows omitted."""
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
        return slice_grid(self._rows, self._cols, key, screen=self)

    def scroll_up(
        self,
        count: int = 1,
        *,
        top: int | None = None,
        bottom: int | None = None,
        left: int = 0,
        right: int | None = None,
        save_history: bool = True,
        bce_word: int = 0,
        bce_ext: _CellExt | None = None,
        is_line_op: bool = False,
    ) -> None:
        """Scroll region ``[top..bottom, left..right]`` upward by *count* lines."""
        t_row = self.scroll_region.top if top is None else top
        b_row = self.scroll_region.bottom if bottom is None else bottom
        r_col = (self._cols - 1) if right is None else right
        if count <= 0 or t_row > b_row:
            return
        shift = min(count, b_row - t_row + 1)
        full_width = left == 0 and r_col == self._cols - 1
        full_screen = full_width and t_row == 0 and b_row == len(self._rows) - 1

        if full_width:
            if t_row > 0:
                if self._rows[t_row - 1].wrapped:
                    self._rows[t_row - 1].wrapped = False
                    bump_row_gen(self._rows[t_row - 1])
                self._clear_row_spacer_head(self._rows[t_row - 1])
            if b_row < len(self._rows) - 1:
                if self._rows[b_row].wrapped:
                    self._rows[b_row].wrapped = False
                    bump_row_gen(self._rows[b_row])
                self._clear_row_spacer_head(self._rows[b_row])
            advances_abs = save_history and t_row == 0 and not self._is_alt
            has_hist_cap = self._history.maxlen is not None and self._history.maxlen > 0
            hist_adv = shift if (advances_abs and not full_screen) else 0
            if self._gfx is not None and not (full_screen and advances_abs):
                self._gfx.on_region_scroll(
                    top=t_row,
                    bottom=b_row,
                    left=left,
                    right=r_col,
                    delta_rows=-count,
                    is_scroll=not is_line_op,
                    scrolled_total=self._scrolled_total,
                    cell_height_px=self._cell_height_px,
                    history_advanced=(
                        count if (advances_abs and not full_screen) else 0
                    ),
                )
            evicted = self._rows[t_row : t_row + shift]
            if advances_abs:
                if has_hist_cap:
                    for row_ev in evicted:
                        self._append_history(row_ev.freeze())
                    extra = count - shift
                    if extra > 0:
                        maxlen = self._history.maxlen or 0
                        blank_row = _Row(self._cols)
                        blank_row.reset_blank(self._cols, bce_word, bce_ext)
                        blank_frozen = blank_row.freeze()
                        skip_evict = max(0, extra - maxlen)
                        if skip_evict > 0:
                            self._evicted_total += len(self._history) + skip_evict
                            self._history.clear()
                        for _ in range(extra - skip_evict):
                            self._append_history(blank_frozen)
                else:
                    self._evicted_total += count
                self._scrolled_total += count
                if self._gfx is not None:
                    self._gfx.reap_evicted_history(
                        self._scrolled_total - len(self._history),
                    )
            del self._rows[t_row : t_row + shift]
            for row_ev in evicted:
                row_ev.reset_blank(self._cols, bce_word, bce_ext)
            self._rows[b_row - shift + 1 : b_row - shift + 1] = evicted
            if not (full_screen and advances_abs):
                stamp_end = len(self._rows) if hist_adv > 0 else (b_row + 1)
                for idx_s in range(t_row, stamp_end):
                    bump_row_gen(self._rows[idx_s])
            return

        if self._gfx is not None:
            self._gfx.on_region_scroll(
                top=t_row,
                bottom=b_row,
                left=left,
                right=r_col,
                delta_rows=-count,
                is_scroll=not is_line_op,
                scrolled_total=self._scrolled_total,
                cell_height_px=self._cell_height_px,
            )
        for dst_r in range(t_row, b_row - shift + 1):
            src_row = self._rows[dst_r + shift]
            dst_row = self._rows[dst_r]
            dst_row.wrapped = False
            self._clear_row_spacer_head(dst_row)
            if dst_r > 0 and left <= 1:
                self._clear_row_spacer_head(self._rows[dst_r - 1])
            dst_row._heal_wide_boundary(left)
            dst_row._heal_wide_boundary(r_col)
            src_row._heal_wide_boundary(left)
            src_row._heal_wide_boundary(r_col)
            for c in range(left, r_col + 1):
                word = src_row.attr_at(c)
                role = unpack_width_role(word)
                base = word & _WIDTH_CLEAR_MASK
                dst_row._set_raw(c, src_row.chars[c], base, role, src_row.ext_at(c))
            _sync_partial_scroll_bounds(dst_row, src_row, left, r_col)
            bump_row_gen(dst_row)

        for clr_r in range(b_row - shift + 1, b_row + 1):
            self._rows[clr_r].wrapped = False
            self._clear_row_spacer_head(self._rows[clr_r])
            if clr_r > 0 and left <= 1:
                self._clear_row_spacer_head(self._rows[clr_r - 1])
            self._rows[clr_r].clear_range(left, r_col + 1, bce_word, bce_ext)

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
        is_line_op: bool = False,
    ) -> None:
        """Scroll region ``[top..bottom, left..right]`` downward by *count* lines."""
        t_row = self.scroll_region.top if top is None else top
        b_row = self.scroll_region.bottom if bottom is None else bottom
        r_col = (self._cols - 1) if right is None else right
        if count <= 0 or t_row > b_row:
            return
        shift = min(count, b_row - t_row + 1)
        full_width = left == 0 and r_col == self._cols - 1

        if self._gfx is not None:
            self._gfx.on_region_scroll(
                top=t_row,
                bottom=b_row,
                left=left,
                right=r_col,
                delta_rows=count,
                is_scroll=not is_line_op,
                scrolled_total=self._scrolled_total,
                cell_height_px=self._cell_height_px,
            )

        if full_width:
            if t_row > 0:
                if self._rows[t_row - 1].wrapped:
                    self._rows[t_row - 1].wrapped = False
                    bump_row_gen(self._rows[t_row - 1])
                self._clear_row_spacer_head(self._rows[t_row - 1])
            recycled = self._rows[b_row - shift + 1 : b_row + 1]
            del self._rows[b_row - shift + 1 : b_row + 1]
            for row_ev in recycled:
                row_ev.reset_blank(self._cols, bce_word, bce_ext)
            self._rows[t_row:t_row] = recycled
            if b_row < len(self._rows) - 1:
                if self._rows[b_row].wrapped:
                    self._rows[b_row].wrapped = False
                    bump_row_gen(self._rows[b_row])
                self._clear_row_spacer_head(self._rows[b_row])
            for idx_s in range(t_row, b_row + 1):
                bump_row_gen(self._rows[idx_s])
            return

        for dst_r in range(b_row, t_row + shift - 1, -1):
            src_row = self._rows[dst_r - shift]
            dst_row = self._rows[dst_r]
            dst_row.wrapped = False
            self._clear_row_spacer_head(dst_row)
            if dst_r > 0 and left <= 1:
                self._clear_row_spacer_head(self._rows[dst_r - 1])
            dst_row._heal_wide_boundary(left)
            dst_row._heal_wide_boundary(r_col)
            src_row._heal_wide_boundary(left)
            src_row._heal_wide_boundary(r_col)
            for c in range(left, r_col + 1):
                word = src_row.attr_at(c)
                role = unpack_width_role(word)
                base = word & _WIDTH_CLEAR_MASK
                dst_row._set_raw(c, src_row.chars[c], base, role, src_row.ext_at(c))
            _sync_partial_scroll_bounds(dst_row, src_row, left, r_col)
            bump_row_gen(dst_row)

        for clr_r in range(t_row, t_row + shift):
            self._rows[clr_r].wrapped = False
            self._clear_row_spacer_head(self._rows[clr_r])
            if clr_r > 0 and left <= 1:
                self._clear_row_spacer_head(self._rows[clr_r - 1])
            self._rows[clr_r].clear_range(left, r_col + 1, bce_word, bce_ext)

    def push_scrollback_and_clear(
        self,
        bce_word: int = 0,
        bce_ext: _CellExt | None = None,
        *,
        include_cursor_row: bool = True,
    ) -> None:
        """Push active lines up to last non-blank row into history."""
        last_used = -1
        for idx in range(len(self._rows) - 1, -1, -1):
            has_img = self._gfx is not None and self._gfx.has_placement_on_row(
                idx,
                self._scrolled_total,
            )
            if (
                self._rows[idx].used > 0
                or (include_cursor_row and idx == self._cur_row)
                or has_img
            ):
                last_used = idx
                break
        if last_used >= 0 and not self._is_alt:
            if self._history.maxlen is not None and self._history.maxlen > 0:
                for idx in range(last_used + 1):
                    self._append_history(self._rows[idx].freeze())
            else:
                self._evicted_total += last_used + 1
            self._scrolled_total += last_used + 1
            if self._gfx is not None:
                self._gfx.reap_evicted_history(
                    self._scrolled_total - len(self._history),
                )
        elif self._gfx is not None:
            self._gfx.clear_active_screen(self._scrolled_total, len(self._rows))
        for r in self._rows:
            r.reset_blank(self._cols, bce_word, bce_ext)

    def _reflow_primary(
        self,
        *,
        rows: int,
        cols: int,
        extra_pins: collections.abc.Sequence[tuple[int, int]] = (),
    ) -> tuple[tuple[int, int], ...]:
        hist_rows = list(self._history.freeze())
        hist_len = len(hist_rows)
        old_first_abs = self._scrolled_total - hist_len

        last_active = self._cur_row
        for r_idx in range(len(self._rows) - 1, self._cur_row, -1):
            if self._rows[r_idx].used > 0 or self._rows[r_idx].wrapped:
                last_active = r_idx
                break

        total = hist_len + last_active + 1
        cur_abs_idx = hist_len + self._cur_row
        eff_cur_col = self._cur_col + (1 if self._pending_wrap else 0)

        gfx_roots = self._gfx.root_placement_pins() if self._gfx is not None else []
        n_extra = len(extra_pins)
        all_pins: list[tuple[int, int]] = list(extra_pins)
        for _, g_abs, g_col in gfx_roots:
            all_pins.append((g_abs, g_col))

        mapped_out: list[tuple[int, int] | None] = [None] * len(all_pins)
        pins_by_row: dict[int, list[tuple[int, int]]] = {}
        for p_idx, (p_abs, p_col) in enumerate(all_pins):
            rel_in = p_abs - old_first_abs
            if 0 <= rel_in < total:
                pins_by_row.setdefault(rel_in, []).append((p_idx, p_col))

        new_all: list[RowLike] = []
        new_cur_abs = 0
        new_cur_col = 0
        new_pending_wrap = False
        cursor_placed = False

        idx = 0
        while idx < total:
            if idx < hist_len:
                fr = hist_rows[idx]
                if not fr.wrapped:
                    fr_t_cols = len(fr.cells) if fr.cells is not None else len(fr.text)
                    fr_used = (
                        fr_t_cols
                        if (not fr.runs and not fr.exts)
                        else max(
                            fr_t_cols,
                            fr.runs[-1][0] if fr.runs else 0,
                            (fr.exts[-1][0] + 1) if fr.exts else 0,
                        )
                    )
                    if fr_used <= cols:
                        out_idx = len(new_all)
                        new_all.append(fr)
                        if pins_by_row:
                            row_ps = pins_by_row.get(idx)
                            if row_ps is not None:
                                for p_idx, p_col in row_ps:
                                    mapped_out[p_idx] = (
                                        out_idx,
                                        min(cols - 1, max(0, p_col)),
                                    )
                        idx += 1
                        continue

            group_start = idx
            while idx + 1 < total:
                cur_src = (
                    hist_rows[idx] if idx < hist_len else self._rows[idx - hist_len]
                )
                if not cur_src.wrapped:
                    break
                idx += 1
            group_end = idx
            idx += 1

            if group_start <= cur_abs_idx < group_end:
                cur_r_src = (
                    hist_rows[cur_abs_idx]
                    if cur_abs_idx < hist_len
                    else self._rows[cur_abs_idx - hist_len]
                )
                cur_r_cols = (
                    len(cur_r_src.chars)
                    if isinstance(cur_r_src, _Row)
                    else cur_r_src.cols
                )
                if eff_cur_col >= cur_r_cols:
                    cur_abs_idx += 1
                    eff_cur_col = 0

            cells: list[tuple[str, int, int, _CellExt | None, int]] = []
            cell_pins: dict[int, list[tuple[int, int]]] = {}
            carried_pins: list[int] = []
            after_group_pins: list[tuple[int, int]] = []
            cur_after_extra: int | None = None
            carry_cur = False

            for r_i in range(group_start, group_end + 1):
                src = hist_rows[r_i] if r_i < hist_len else self._rows[r_i - hist_len]
                is_last = r_i == group_end
                src_cols = len(src.chars) if isinstance(src, _Row) else src.cols
                src_t_used = (
                    src.text_used
                    if isinstance(src, _Row)
                    else (len(src.cells) if src.cells is not None else len(src.text))
                )
                row_limit = src_cols if not is_last else src_t_used
                if r_i == cur_abs_idx and eff_cur_col >= row_limit:
                    cur_after_extra = eff_cur_col - row_limit

                row_ps = pins_by_row.get(r_i)
                col_pins_map: dict[int, list[int]] | None = None
                if row_ps is not None:
                    col_pins_map = {}
                    for p_idx, p_col in row_ps:
                        eff_pc = max(0, p_col)
                        if eff_pc >= row_limit:
                            if is_last:
                                after_group_pins.append((p_idx, eff_pc - row_limit))
                            else:
                                carried_pins.append(p_idx)
                        else:
                            col_pins_map.setdefault(eff_pc, []).append(p_idx)

                for c_i in range(row_limit):
                    word = src.attr_at(c_i)
                    rb = raw_width_bits(word)
                    is_cur = r_i == cur_abs_idx and c_i == eff_cur_col
                    here_pins = (
                        col_pins_map.get(c_i) if col_pins_map is not None else None
                    )
                    if rb == int(WidthRole.SPACER_HEAD):
                        if is_cur:
                            carry_cur = True
                        if here_pins:
                            carried_pins.extend(here_pins)
                        continue
                    if rb == int(WidthRole.SPACER_TAIL):
                        if is_cur and cells:
                            p_ch, p_w, p_cw, p_ext, _ = cells[-1]
                            cells[-1] = (p_ch, p_w, p_cw, p_ext, 1)
                        if here_pins:
                            if cells:
                                prev_k = len(cells) - 1
                                tgt = cell_pins.setdefault(prev_k, [])
                                for p_idx in here_pins:
                                    tgt.append((p_idx, 1))
                            else:
                                carried_pins.extend(here_pins)
                        continue
                    cw = 2 if rb == int(WidthRole.WIDE) else 1
                    base_word = word & _PEN_BASE_MASK
                    cur_off = 0 if (is_cur or carry_cur) else -1
                    carry_cur = False
                    cell_k = len(cells)
                    if carried_pins or here_pins:
                        tgt = cell_pins.setdefault(cell_k, [])
                        for p_idx in carried_pins:
                            tgt.append((p_idx, 0))
                        carried_pins.clear()
                        if here_pins:
                            for p_idx in here_pins:
                                tgt.append((p_idx, 0))
                    ch_val = (
                        src.chars[c_i] if isinstance(src, _Row) else src.char_at(c_i)
                    )
                    cells.append(
                        (
                            ch_val,
                            base_word,
                            cw,
                            src.ext_at(c_i),
                            cur_off,
                        )
                    )

            if not cells:
                empty_row = _Row(cols)
                new_all.append(empty_row)
                out_idx = len(new_all) - 1
                if group_start <= cur_abs_idx <= group_end and not cursor_placed:
                    extra = 0 if cur_after_extra is None else cur_after_extra
                    new_cur_abs = out_idx
                    new_cur_col = min(cols - 1, extra)
                    new_pending_wrap = False
                    cursor_placed = True
                for p_idx in carried_pins:
                    mapped_out[p_idx] = (out_idx, 0)
                for p_idx, p_extra in after_group_pins:
                    mapped_out[p_idx] = (out_idx, min(cols - 1, p_extra))
                continue

            cur_out = _Row(cols)
            new_all.append(cur_out)
            out_col = 0
            for cell_k, (ch, base_w, cw, ce, cur_off) in enumerate(cells):
                eff_cw = cw
                eff_ch = ch
                if eff_cw == 2 and cols == 1:
                    eff_ch = "\ufffd"
                    eff_cw = 1

                if out_col + eff_cw > cols:
                    if out_col < cols:
                        cur_out.write_spacer_head(out_col, 0, None)
                    cur_out.wrapped = True
                    cur_out = _Row(cols)
                    new_all.append(cur_out)
                    out_col = 0

                out_idx = len(new_all) - 1
                if cur_off >= 0 and not cursor_placed:
                    new_cur_abs = out_idx
                    new_cur_col = min(cols - 1, out_col + cur_off)
                    new_pending_wrap = False
                    cursor_placed = True

                c_ps = cell_pins.get(cell_k)
                if c_ps is not None:
                    for p_idx, p_off in c_ps:
                        mapped_out[p_idx] = (
                            out_idx,
                            min(cols - 1, out_col + p_off),
                        )

                if eff_cw == 2:
                    cur_out.write_wide(out_col, eff_ch, base_w, ce)
                    out_col += 2
                else:
                    cur_out.write_narrow(out_col, eff_ch, base_w, ce)
                    out_col += 1

            last_out_idx = len(new_all) - 1
            for p_idx in carried_pins:
                mapped_out[p_idx] = (last_out_idx, min(cols - 1, out_col))
            for p_idx, p_extra in after_group_pins:
                mapped_out[p_idx] = (
                    last_out_idx,
                    min(cols - 1, out_col + p_extra),
                )

            if (cur_after_extra is not None or carry_cur) and not cursor_placed:
                extra = 0 if cur_after_extra is None else cur_after_extra
                new_cur_abs = last_out_idx
                if extra == 0 and out_col == cols:
                    new_cur_col = cols - 1
                    new_pending_wrap = True
                else:
                    new_cur_col = min(cols - 1, out_col + extra)
                    new_pending_wrap = False
                cursor_placed = True

        if not new_all:
            new_all.append(_Row(cols))

        last_in_abs = old_first_abs + total - 1
        last_out_base = len(new_all) - 1
        for p_idx, (p_abs, p_col) in enumerate(all_pins):
            if p_abs > last_in_abs:
                mapped_out[p_idx] = (
                    last_out_base + (p_abs - last_in_abs),
                    min(cols - 1, max(0, p_col)),
                )

        hist_cut = max(0, len(new_all) - rows)
        hist_cut = min(hist_cut, new_cur_abs)

        prev_scrolled = self._scrolled_total
        prev_evicted = self._evicted_total
        evicted_in_reflow = self._history.replace_all(new_all[:hist_cut])
        new_hist_len = len(self._history)
        new_evicted = max(
            prev_evicted + evicted_in_reflow,
            prev_scrolled - new_hist_len,
        )
        self._evicted_total = new_evicted
        self._scrolled_total = new_evicted + new_hist_len

        viewport: list[_Row] = [
            r_v.thaw(cols) if isinstance(r_v, FrozenRow) else r_v
            for r_v in new_all[hist_cut : hist_cut + rows]
        ]
        while len(viewport) < rows:
            viewport.append(_Row(cols))
        for r_v in viewport:
            bump_row_gen(r_v)
        self._rows = viewport
        self._cols = cols
        self._cur_row = max(0, min(rows - 1, new_cur_abs - hist_cut))
        self._cur_col = max(0, min(cols - 1, new_cur_col))
        self._pending_wrap = new_pending_wrap

        if not all_pins:
            if self._gfx is not None:
                self._gfx.apply_reflow_pins(
                    (),
                    new_cols=cols,
                    min_retained_abs_row=self._evicted_total,
                    max_abs=self._scrolled_total + rows,
                )
            return ()

        final_pins: list[tuple[int, int]] = []
        new_first_abs = self._scrolled_total - hist_cut
        for p_idx, (old_abs, old_col) in enumerate(all_pins):
            m_pair = mapped_out[p_idx]
            if m_pair is None:
                if old_abs < old_first_abs:
                    mapped_abs = new_first_abs + (old_abs - old_first_abs)
                else:
                    mapped_abs = old_abs
                final_pins.append((mapped_abs, min(cols - 1, max(0, old_col))))
            else:
                out_r, new_c = m_pair
                final_pins.append((new_first_abs + out_r, new_c))

        if self._gfx is not None:
            remapped_gfx = [
                (gfx_idx, final_pins[n_extra + k][0], final_pins[n_extra + k][1])
                for k, (gfx_idx, _, _) in enumerate(gfx_roots)
            ]
            self._gfx.apply_reflow_pins(
                remapped_gfx,
                new_cols=cols,
                min_retained_abs_row=self._evicted_total,
                max_abs=self._scrolled_total + rows,
            )

        return tuple(final_pins[:n_extra])

    def _resize_with_pins(
        self,
        *,
        rows: int,
        cols: int,
        reflow: bool = True,
        extra_pins: collections.abc.Sequence[tuple[int, int]] = (),
    ) -> tuple[tuple[int, int], ...]:
        if rows < 1 or cols < 1:
            msg = f"rows and cols must be >= 1, got ({rows}, {cols})"
            raise ValueError(msg)

        old_cols = self._cols
        if rows == len(self._rows) and cols == old_cols:
            return tuple(extra_pins)

        if reflow and cols != old_cols:
            remapped = self._reflow_primary(
                rows=rows,
                cols=cols,
                extra_pins=extra_pins,
            )
        else:
            old_cur_col = self._cur_col
            if cols != old_cols:
                for r in self._rows:
                    r.resize(cols)
                self._cols = cols

            cur_rows = len(self._rows)
            top_drop_no_hist = 0
            if rows < cur_rows:
                drop_needed = cur_rows - rows
                top_excess = max(0, self._cur_row - (rows - 1))
                top_drop = min(drop_needed, top_excess)
                if top_drop > 0:
                    if not self._is_alt:
                        if self._history.maxlen > 0:
                            for r_ev in self._rows[:top_drop]:
                                self._append_history(r_ev.freeze())
                        else:
                            self._evicted_total += top_drop
                        self._scrolled_total += top_drop
                        if self._gfx is not None:
                            self._gfx.reap_evicted_history(
                                self._scrolled_total - len(self._history),
                            )
                    else:
                        top_drop_no_hist = top_drop
                    del self._rows[:top_drop]
                    self._cur_row -= top_drop
                if len(self._rows) > rows:
                    del self._rows[rows:]
            elif rows > cur_rows:
                self._rows.extend(_Row(cols) for _ in range(rows - cur_rows))

            if self._gfx is not None:
                self._gfx.on_non_reflow_resize(
                    top_drop_without_history=top_drop_no_hist,
                    new_rows=rows,
                    new_cols=cols,
                    scrolled_total=self._scrolled_total,
                    min_retained_abs_row=self._evicted_total,
                )

            for r_v in self._rows:
                bump_row_gen(r_v)

            self._cur_row = min(self._cur_row, rows - 1)
            self._cur_col = min(self._cur_col, cols - 1)
            self._pending_wrap = self._pending_wrap and old_cur_col < cols
            remapped = (
                tuple(
                    (p_abs, min(cols - 1, max(0, p_col))) for p_abs, p_col in extra_pins
                )
                if extra_pins
                else ()
            )

        self.scroll_region = ScrollRegion(
            top=0,
            bottom=rows - 1,
            left=0,
            right=cols - 1,
        )
        if cols > old_cols:
            first_new = ((old_cols // 8) + 1) * 8
            self._tabstops.update(range(first_new, cols, 8))
        else:
            self._tabstops = {t for t in self._tabstops if t < cols}
        self._tabstops_sorted = sorted(self._tabstops)
        return remapped

    def resize(
        self,
        *,
        rows: int,
        cols: int,
        reflow: bool = True,
    ) -> None:
        """Resize screen to ``(rows, cols)``, optionally reflowing soft wraps."""
        self._resize_with_pins(rows=rows, cols=cols, reflow=reflow)

    def reset(self) -> None:
        """Clear all visible rows, margins, cursor, keyboard stack, and tabstops."""
        rows = len(self._rows)
        for r in self._rows:
            r.reset_blank(self._cols)
        self._cur_row = 0
        self._cur_col = 0
        self._cur_visible = True
        self._cur_shape = CursorShape.BLOCK
        self._cur_blinking = True
        self._pending_wrap = False
        self.scroll_region = ScrollRegion(
            top=0,
            bottom=rows - 1,
            left=0,
            right=self._cols - 1,
        )
        self._tabstops = set(range(8, self._cols, 8))
        self._tabstops_sorted = sorted(self._tabstops)
        self.keyboard_stack.reset()
        if self._gfx is not None:
            self._gfx.reset()


def _sync_partial_scroll_bounds(
    dst_row: _Row,
    src_row: _Row,
    left: int,
    r_col: int,
) -> None:
    limit = r_col + 1
    if dst_row.text_used <= limit:
        cand_t = (
            min(src_row.text_used, limit)
            if src_row.text_used > left
            else min(dst_row.text_used, left)
        )
        if src_row.text_used > limit:
            floor_t = min(dst_row.text_used, left)
            while cand_t > floor_t and dst_row.chars[cand_t - 1] == " ":
                cand_t -= 1
        dst_row.text_used = cand_t
    if dst_row.used <= limit:
        cand_u = (
            min(src_row.used, limit) if src_row.used > left else min(dst_row.used, left)
        )
        floor_u = max(min(dst_row.used, left), dst_row.text_used)
        if src_row.used > limit:
            while (
                cand_u > floor_u
                and dst_row.chars[cand_u - 1] == " "
                and dst_row.attr_at(cand_u - 1) == 0
                and dst_row.ext_at(cand_u - 1) is None
            ):
                cand_u -= 1
        dst_row.used = max(cand_u, dst_row.text_used)


class ScreenSet:
    """Dual primary and alternate (``?1049``) screen pair."""

    __slots__ = ("active_is_alt", "alternate", "primary")

    def __init__(
        self,
        *,
        rows: int = 24,
        cols: int = 80,
        scrollback: int = 2_000,
        keyboard_stack_depth: int = 8,
        cell_height_px: int = 16,
        max_images: int = 256,
        max_placements: int = 1_024,
        max_image_dimension: int = 10_000,
    ) -> None:
        self.primary = Screen(
            rows=rows,
            cols=cols,
            scrollback=scrollback,
            keyboard_stack_depth=keyboard_stack_depth,
            cell_height_px=cell_height_px,
            max_images=max_images,
            max_placements=max_placements,
            max_image_dimension=max_image_dimension,
        )
        self.alternate = Screen(
            rows=rows,
            cols=cols,
            scrollback=0,
            keyboard_stack_depth=keyboard_stack_depth,
            cell_height_px=cell_height_px,
            max_images=max_images,
            max_placements=max_placements,
            max_image_dimension=max_image_dimension,
            is_alt=True,
        )
        self.active_is_alt: bool = False

    @property
    def active(self) -> Screen:
        """Return the currently active :class:`Screen`."""
        return self.alternate if self.active_is_alt else self.primary

    def _resize_with_pins(
        self,
        *,
        rows: int,
        cols: int,
        reflow: bool = True,
        primary_pins: collections.abc.Sequence[tuple[int, int]] = (),
    ) -> tuple[tuple[int, int], ...]:
        remapped = self.primary._resize_with_pins(
            rows=rows,
            cols=cols,
            reflow=reflow,
            extra_pins=primary_pins,
        )
        self.alternate._resize_with_pins(rows=rows, cols=cols, reflow=False)
        return remapped

    def resize(
        self,
        *,
        rows: int,
        cols: int,
        reflow: bool = True,
    ) -> None:
        """Resize both screens (reflowing primary when *reflow* is ``True``)."""
        self._resize_with_pins(rows=rows, cols=cols, reflow=reflow)


__all__ = [
    "BLANK_CELL",
    "Cell",
    "Cursor",
    "CursorShape",
    "RegionView",
    "RowRun",
    "RowView",
    "Screen",
    "ScreenSet",
    "ScrollRegion",
]
