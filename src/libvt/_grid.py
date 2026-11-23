"""Two-tier row storage: lazy 64-bit packed viewport ``_Row`` and RLE ``FrozenRow``.

Bit layout of each 64-bit unsigned attribute word (``0`` == blank default cell):

- bits ``0..24`` (25b): foreground color (``0`` default, ``1..16`` ANSI,
  ``256..511`` 256-palette index, ``(1 << 24) | 0xRRGGBB`` RGB)
- bits ``25..49`` (25b): background color
- bits ``50..56`` (7b): ``bold``, ``dim``, ``italic``, ``blink``, ``reverse``,
  ``hidden``, ``strikethrough``
- bits ``57..59`` (3b): :class:`~libvt.style.UnderlineStyle`
- bits ``60..61`` (2b): :class:`~libvt._ucd.WidthRole`
- bit ``62`` (1b): ``overline``
- bit ``63`` (1b): ``HAS_EXT`` gating sparse :class:`_CellExt`
"""

from __future__ import annotations

import array
import bisect
import collections.abc
import operator
import typing as t
from dataclasses import dataclass

from libvt._ucd import WidthRole
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

_ITEM0 = operator.itemgetter(0)
_HISTORY_BLOCK_SHIFT = 8
_HISTORY_BLOCK_SIZE = 1 << _HISTORY_BLOCK_SHIFT
_HISTORY_BLOCK_MASK = _HISTORY_BLOCK_SIZE - 1


class _RowGenCounter:
    """Per-screen-set monotonic row-mutation counter."""

    __slots__ = ("value",)

    def __init__(self, initial: int = 0) -> None:
        self.value: int = initial

    def next(self) -> int:
        """Advance and return the next strictly monotonic generation."""
        self.value += 1
        return self.value


_COLOR_MASK = (1 << 25) - 1
_RGB_FLAG = 1 << 24
_IDX_BASE = 256

_BG_SHIFT = 25
_BOLD_BIT = 1 << 50
_DIM_BIT = 1 << 51
_ITALIC_BIT = 1 << 52
_BLINK_BIT = 1 << 53
_REVERSE_BIT = 1 << 54
_HIDDEN_BIT = 1 << 55
_STRIKE_BIT = 1 << 56
_UL_SHIFT = 57
_UL_MASK = 0b111
_WIDTH_SHIFT = 60
_WIDTH_MASK = 0b11
_WIDTH_CLEAR_MASK = ~(_WIDTH_MASK << _WIDTH_SHIFT) & ((1 << 64) - 1)
_OVERLINE_BIT = 1 << 62
_HAS_EXT_BIT = 1 << 63
_PEN_BASE_MASK = _WIDTH_CLEAR_MASK & ~_HAS_EXT_BIT
_PEN_BG_MASK = _COLOR_MASK << _BG_SHIFT


@dataclass(frozen=True, slots=True)
class _CellExt:
    """Sparse per-cell metadata stored only when ``HAS_EXT`` is set.

    Attributes
    ----------
    underline_color : Color
        Explicit underline color (``SGR 58``), if any.
    hyperlink : Hyperlink | None
        Active ``OSC 8`` hyperlink, if any.
    protected : bool
        ``DECSCA`` character-protection flag respected by ``DECSED``/``DECSEL``.
    """

    underline_color: Color = None
    hyperlink: Hyperlink | None = None
    protected: bool = False


def _pack_color(color: Color) -> int:
    if color is None:
        return 0
    if isinstance(color, AnsiColor):
        return int(color) + 1
    if isinstance(color, IndexedColor):
        return _IDX_BASE + color.index
    return _RGB_FLAG | (color.r << 16) | (color.g << 8) | color.b


def _unpack_color(bits: int) -> Color:
    if bits == 0:
        return None
    if bits & _RGB_FLAG:
        rgb = bits & 0xFFFFFF
        return RgbColor((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF)
    if bits >= _IDX_BASE:
        return IndexedColor(bits - _IDX_BASE)
    return AnsiColor(bits - 1)


def pack_pen_base(style: Style) -> int:
    """Pack *style* into a 64-bit word (leaving ``WidthRole`` and ``HAS_EXT`` clear)."""
    if style == DEFAULT_STYLE:
        return 0
    word = _pack_color(style.fg) | (_pack_color(style.bg) << _BG_SHIFT)
    if style.bold:
        word |= _BOLD_BIT
    if style.dim:
        word |= _DIM_BIT
    if style.italic:
        word |= _ITALIC_BIT
    if style.blink:
        word |= _BLINK_BIT
    if style.reverse:
        word |= _REVERSE_BIT
    if style.hidden:
        word |= _HIDDEN_BIT
    if style.strikethrough:
        word |= _STRIKE_BIT
    if style.underline != UnderlineStyle.NONE:
        word |= (int(style.underline) & _UL_MASK) << _UL_SHIFT
    if style.overline:
        word |= _OVERLINE_BIT
    return word


def make_cell_ext(
    style: Style,
    hyperlink: Hyperlink | None,
    *,
    protected: bool = False,
) -> _CellExt | None:
    """Build sparse :class:`_CellExt` when needed, else return ``None``."""
    if style.underline_color is None and hyperlink is None and not protected:
        return None
    return _CellExt(
        underline_color=style.underline_color,
        hyperlink=hyperlink,
        protected=protected,
    )


def unpack_style(word: int, ext: _CellExt | None) -> Style:
    """Unpack a 64-bit cell attribute word and optional :class:`_CellExt`."""
    style_bits = word & _WIDTH_CLEAR_MASK & ~_HAS_EXT_BIT
    if style_bits == 0 and (ext is None or ext.underline_color is None):
        return DEFAULT_STYLE
    fg = _unpack_color(word & _COLOR_MASK)
    bg = _unpack_color((word >> _BG_SHIFT) & _COLOR_MASK)
    ul_raw = (word >> _UL_SHIFT) & _UL_MASK
    ul_color = ext.underline_color if ext is not None else None
    return Style(
        fg=fg,
        bg=bg,
        underline_color=ul_color,
        bold=bool(word & _BOLD_BIT),
        dim=bool(word & _DIM_BIT),
        italic=bool(word & _ITALIC_BIT),
        underline=UnderlineStyle(ul_raw),
        blink=bool(word & _BLINK_BIT),
        reverse=bool(word & _REVERSE_BIT),
        hidden=bool(word & _HIDDEN_BIT),
        strikethrough=bool(word & _STRIKE_BIT),
        overline=bool(word & _OVERLINE_BIT),
    )


def unpack_width_role(word: int) -> WidthRole:
    """Extract :class:`~libvt._ucd.WidthRole` from a 64-bit cell word."""
    raw = (word >> _WIDTH_SHIFT) & _WIDTH_MASK
    if raw == 0:
        return WidthRole.NARROW
    return WidthRole(raw)


def raw_width_bits(word: int) -> int:
    """Extract raw 2-bit width role integer without constructing an Enum."""
    return (word >> _WIDTH_SHIFT) & _WIDTH_MASK


@dataclass(frozen=True, slots=True)
class FrozenRow:
    """Run-length-encoded immutable scrollback line.

    Attributes
    ----------
    text : str
        Right-trimmed rendered row text.
    cols : int
        Column count when the row was sealed.
    runs : tuple[tuple[int, int], ...]
        ``(end_col, attr_u64)`` run-length spans; ``()`` when all cells carry
        attribute word ``0``.
    exts : tuple[tuple[int, _CellExt], ...]
        Sorted ``(col, _CellExt)`` sparse metadata pairs.
    cells : tuple[str, ...] | None
        Per-column cell strings up to ``used`` when the row contains wide
        spacers or multi-codepoint clusters; ``None`` when ``text`` maps 1:1
        to narrow columns ``0..len(text) - 1``.
    wrapped : bool
        ``True`` when the row soft-wrapped onto the next row under ``DECAWM``.
    """

    text: str
    cols: int
    runs: tuple[tuple[int, int], ...] = ()
    exts: tuple[tuple[int, _CellExt], ...] = ()
    cells: tuple[str, ...] | None = None
    wrapped: bool = False

    @property
    def used(self) -> int:
        """Return count of written columns in this sealed row."""
        text_cols = len(self.cells) if self.cells is not None else len(self.text)
        run_end = self.runs[-1][0] if self.runs else 0
        ext_end = (self.exts[-1][0] + 1) if self.exts else 0
        return max(text_cols, run_end, ext_end)

    def rstrip_text(self) -> str:
        """Return right-trimmed row text."""
        return self.text

    def char_at(self, col: int) -> str:
        """Return cell string at *col*."""
        if col < 0:
            return " "
        if self.cells is not None:
            return self.cells[col] if col < len(self.cells) else " "
        return self.text[col] if col < len(self.text) else " "

    def attr_at(self, col: int) -> int:
        """Return packed 64-bit attribute word at *col*."""
        runs = self.runs
        if not runs or col < 0:
            return 0
        if len(runs) == 1:
            end_col, word = runs[0]
            return word if col < end_col else 0
        idx = bisect.bisect_right(runs, col, key=_ITEM0)
        return runs[idx][1] if idx < len(runs) else 0

    def ext_at(self, col: int) -> _CellExt | None:
        """Return sparse :class:`_CellExt` at *col*, if any."""
        exts = self.exts
        if not exts or col < 0:
            return None
        if len(exts) == 1:
            ext_col, ce = exts[0]
            return ce if ext_col == col else None
        idx = bisect.bisect_right(exts, col, key=_ITEM0) - 1
        if idx >= 0 and exts[idx][0] == col:
            return exts[idx][1]
        return None

    def thaw(
        self,
        target_cols: int | None = None,
        *,
        gen_counter: _RowGenCounter | None = None,
    ) -> _Row:
        """Materialize a mutable :class:`_Row` from this sealed line."""
        width = self.cols if target_cols is None else target_cols
        row = _Row(width, gen_counter=gen_counter)
        row._wrapped = self.wrapped if width == self.cols else False
        limit = min(self.used, width)
        if limit == 0 and not self.runs:
            row.stamp()
            return row
        for c in range(limit):
            row._chars[c] = self.char_at(c)
        truncated_wide = False
        if self.runs:
            attrs = row._ensure_attrs()
            start = 0
            for end_col, word in self.runs:
                stop = min(end_col, width)
                for c in range(start, stop):
                    attrs[c] = word
                    if raw_width_bits(word) != 0:
                        row._has_wide = True
                start = stop
                if start >= width:
                    break
            if width > 0 and raw_width_bits(attrs[width - 1]) == int(WidthRole.WIDE):
                row._chars[width - 1] = " "
                attrs[width - 1] &= _WIDTH_CLEAR_MASK
                truncated_wide = True
        if self.exts:
            row._ext = {c: ce for c, ce in self.exts if c < width}
        t_raw = len(self.cells) if self.cells is not None else len(self.text)
        row._used = limit
        row._text_used = min(t_raw, width - 1 if truncated_wide else width)
        if (
            truncated_wide
            and row._used == width
            and row.attr_at(width - 1) == 0
            and row.ext_at(width - 1) is None
        ):
            row._used = max(row._text_used, width - 1)
        if width != self.cols and 0 < self.cols <= width:
            row.clear_spacer_head(self.cols - 1)
        row.stamp()
        return row


class _Row:
    """Mutable viewport row guarded by write barriers on every cell/wrap edit."""

    __slots__ = (
        "_attrs",
        "_chars",
        "_content_gen",
        "_ext",
        "_frozen",
        "_gen_counter",
        "_has_wide",
        "_text_used",
        "_uniform_attr",
        "_used",
        "_wrapped",
    )

    def __init__(
        self,
        cols: int,
        *,
        gen_counter: _RowGenCounter | None = None,
    ) -> None:
        self._chars: list[str] = [" "] * cols
        self._attrs: array.array[int] | None = None
        self._uniform_attr: int = 0
        self._has_wide: bool = False
        self._ext: dict[int, _CellExt] | None = None
        self._used: int = 0
        self._text_used: int = 0
        self._wrapped: bool = False
        self._gen_counter: _RowGenCounter = (
            _RowGenCounter() if gen_counter is None else gen_counter
        )
        self._content_gen: int = 0
        self._frozen: FrozenRow | None = None

    @property
    def cols(self) -> int:
        """Return cell capacity of this row."""
        return len(self._chars)

    @property
    def used(self) -> int:
        """Return non-default cell high-water mark."""
        return self._used

    @property
    def text_used(self) -> int:
        """Return written-text column high-water mark."""
        return self._text_used

    @property
    def has_wide(self) -> bool:
        """Return ``True`` if this row may hold wide or spacer cells."""
        return self._has_wide

    @property
    def content_gen(self) -> int:
        """Return monotonic mutation stamp on this row's counter."""
        return self._content_gen

    @property
    def wrapped(self) -> bool:
        """Return ``True`` when this row soft-wrapped onto the next line."""
        return self._wrapped

    @wrapped.setter
    def wrapped(self, value: bool) -> None:
        if self._wrapped != value:
            self._wrapped = value
            self.stamp()

    def stamp(self) -> int:
        """Advance ``content_gen`` on the owning counter and clear ``_frozen``."""
        self._content_gen = self._gen_counter.next()
        self._frozen = None
        return self._content_gen

    def bind_counter(self, counter: _RowGenCounter, *, stamp: bool = True) -> None:
        """Attach *counter* to this row and optionally stamp a fresh generation."""
        self._gen_counter = counter
        if stamp:
            self.stamp()

    def _ensure_attrs(self) -> array.array[int]:
        if self._attrs is None:
            self._attrs = array.array("Q", [self._uniform_attr]) * len(self._chars)
        return self._attrs

    def char_at(self, col: int) -> str:
        """Return grapheme string at *col*."""
        return self._chars[col]

    def slice_text(self, start: int, stop: int) -> str:
        """Concatenate cell strings across ``[start, stop)``."""
        return "".join(self._chars[start:stop])

    def attr_at(self, col: int) -> int:
        """Return 64-bit attribute word at *col*."""
        if self._attrs is None:
            return self._uniform_attr
        return self._attrs[col]

    def ext_at(self, col: int) -> _CellExt | None:
        """Return sparse :class:`_CellExt` at *col*, if any."""
        return self._ext.get(col) if self._ext is not None else None

    def is_protected(self, col: int) -> bool:
        """Return ``True`` when cell *col* carries ``DECSCA`` protection."""
        if self._ext is None:
            return False
        ce = self._ext.get(col)
        return bool(ce is not None and ce.protected)

    def reset_blank(
        self,
        cols: int,
        bce_word: int = 0,
        bce_ext: _CellExt | None = None,
    ) -> None:
        """Reset this row in place for zero-allocation scroll recycling."""
        if len(self._chars) != cols:
            self._chars = [" "] * cols
        else:
            self._chars[:] = [" "] * cols
        self._attrs = None
        self._uniform_attr = bce_word | (_HAS_EXT_BIT if bce_ext is not None else 0)
        self._has_wide = False
        if bce_ext is not None:
            self._ext = dict.fromkeys(range(cols), bce_ext)
            self._used = cols
        else:
            if self._ext is not None:
                self._ext.clear()
            self._used = cols if bce_word != 0 else 0
        self._text_used = 0
        self._wrapped = False
        self.stamp()

    def _set_raw(
        self,
        col: int,
        ch: str,
        pen_word: int,
        role: WidthRole,
        cell_ext: _CellExt | None,
    ) -> None:
        word = pen_word | (int(role) << _WIDTH_SHIFT)
        if cell_ext is not None:
            word |= _HAS_EXT_BIT
            if self._ext is None:
                self._ext = {col: cell_ext}
            else:
                self._ext[col] = cell_ext
        elif self._ext is not None:
            self._ext.pop(col, None)
        if role != WidthRole.NARROW:
            self._has_wide = True
        self._chars[col] = ch
        if self._attrs is None:
            if word == self._uniform_attr:
                return
            self._ensure_attrs()[col] = word
        else:
            self._attrs[col] = word

    def _heal_wide_boundary(self, col: int) -> None:
        """Clear both halves when an edit or margin boundary splits a wide glyph."""
        if not self._has_wide or self._attrs is None:
            return
        cols = len(self._chars)
        if col < 0 or col >= cols:
            return
        role = raw_width_bits(self._attrs[col])
        healed_pair_end = -1
        if role == int(WidthRole.WIDE):
            self._chars[col] = " "
            self._attrs[col] &= _WIDTH_CLEAR_MASK
            healed_pair_end = col + 1
            if col + 1 < cols:
                self._chars[col + 1] = " "
                self._attrs[col + 1] &= _WIDTH_CLEAR_MASK
                healed_pair_end = col + 2
        elif role == int(WidthRole.SPACER_TAIL):
            self._chars[col] = " "
            self._attrs[col] &= _WIDTH_CLEAR_MASK
            healed_pair_end = col + 1
            if col - 1 >= 0:
                self._chars[col - 1] = " "
                self._attrs[col - 1] &= _WIDTH_CLEAR_MASK
        if healed_pair_end != -1:
            if self._text_used == healed_pair_end:
                pair_lo = max(0, healed_pair_end - 2)
                self._text_used = pair_lo
                if (
                    self._used == healed_pair_end
                    and self.attr_at(healed_pair_end - 1) == 0
                    and self.ext_at(healed_pair_end - 1) is None
                ):
                    self._used = pair_lo
            self.stamp()

    def clear_spacer_head(self, col: int) -> bool:
        """Clear ``SPACER_HEAD`` at *col* and tighten trailing default blanks."""
        if not self._has_wide or self._attrs is None:
            return False
        if not (0 <= col < len(self._chars)):
            return False
        if raw_width_bits(self._attrs[col]) != int(WidthRole.SPACER_HEAD):
            return False
        self._chars[col] = " "
        self._attrs[col] &= _WIDTH_CLEAR_MASK
        if self._text_used == col + 1:
            self._text_used = col
            while (
                self._text_used > 0
                and self._chars[self._text_used - 1] == " "
                and self.attr_at(self._text_used - 1) == 0
                and self.ext_at(self._text_used - 1) is None
            ):
                self._text_used -= 1
        while (
            self._used > 0
            and self._chars[self._used - 1] == " "
            and self.attr_at(self._used - 1) == 0
            and self.ext_at(self._used - 1) is None
        ):
            self._used -= 1
        self.stamp()
        return True

    def trim_trailing_blanks(self) -> None:
        """Trim unwrapped trailing space padding from ``text_used`` and ``used``."""
        orig_t = self._text_used
        orig_u = self._used
        while self._text_used > 0 and self._chars[self._text_used - 1] == " ":
            self._text_used -= 1
        while (
            self._used > self._text_used
            and self._chars[self._used - 1] == " "
            and self.attr_at(self._used - 1) == 0
            and self.ext_at(self._used - 1) is None
        ):
            self._used -= 1
        if self._text_used != orig_t or self._used != orig_u:
            self.stamp()

    def copy_region_from(self, src_row: _Row, left: int, right: int) -> None:
        """Copy cells ``[left..right]`` from *src_row* during partial scroll."""
        if right == len(src_row._chars) - 1 and src_row.clear_spacer_head(right):
            src_row.wrapped = False
        self._heal_wide_boundary(left)
        self._heal_wide_boundary(right)
        src_row._heal_wide_boundary(left)
        src_row._heal_wide_boundary(right)
        for c in range(left, right + 1):
            word = src_row.attr_at(c)
            role = unpack_width_role(word)
            ch = src_row._chars[c]
            if role == WidthRole.SPACER_HEAD and not self._wrapped:
                role = WidthRole.NARROW
                ch = " "
            base = word & _WIDTH_CLEAR_MASK
            self._set_raw(c, ch, base, role, src_row.ext_at(c))
        limit = right + 1
        if self._text_used <= limit:
            cand_t = (
                min(src_row._text_used, limit)
                if src_row._text_used > left
                else min(self._text_used, left)
            )
            if src_row._text_used > limit:
                floor_t = min(self._text_used, left)
                while cand_t > floor_t and self._chars[cand_t - 1] == " ":
                    cand_t -= 1
            self._text_used = cand_t
        if self._used <= limit:
            cand_u = (
                min(src_row._used, limit)
                if src_row._used > left
                else min(self._used, left)
            )
            floor_u = max(min(self._used, left), self._text_used)
            if src_row._used > limit:
                while (
                    cand_u > floor_u
                    and self._chars[cand_u - 1] == " "
                    and self.attr_at(cand_u - 1) == 0
                    and self.ext_at(cand_u - 1) is None
                ):
                    cand_u -= 1
            self._used = max(cand_u, self._text_used)
        self.stamp()

    def write_narrow(
        self,
        col: int,
        ch: str,
        pen_word: int,
        cell_ext: _CellExt | None,
    ) -> None:
        """Write a 1-column glyph at *col*, healing any split wide cell."""
        self._heal_wide_boundary(col)
        self._set_raw(col, ch, pen_word, WidthRole.NARROW, cell_ext)
        self._used = max(self._used, col + 1)
        self._text_used = max(self._text_used, col + 1)
        self.stamp()

    def write_spacer_head(
        self,
        col: int,
        bce_word: int,
        bce_ext: _CellExt | None,
    ) -> None:
        """Write ``WidthRole.SPACER_HEAD`` padding at right margin before wide wrap."""
        self._heal_wide_boundary(col)
        self._set_raw(col, "", bce_word, WidthRole.SPACER_HEAD, bce_ext)
        self._used = max(self._used, col + 1)
        self._text_used = max(self._text_used, col + 1)
        self.stamp()

    def write_ascii_slice(
        self,
        col: int,
        text: str,
        pen_word: int,
        cell_ext: _CellExt | None,
    ) -> None:
        """Fast-path bulk write of 1-column printable ASCII characters."""
        n = len(text)
        end = col + n
        if self._has_wide and self._attrs is not None:
            self._heal_wide_boundary(col)
            if end - 1 > col:
                self._heal_wide_boundary(end - 1)
            for idx in range(col, end):
                if (
                    raw_width_bits(self._attrs[idx]) == int(WidthRole.WIDE)
                    and idx + 1 >= end
                    and idx + 1 < len(self._chars)
                ):
                    self._chars[idx + 1] = " "
                    self._attrs[idx + 1] &= _WIDTH_CLEAR_MASK

        self._chars[col:end] = list(text)
        word = pen_word | (_HAS_EXT_BIT if cell_ext is not None else 0)
        if self._attrs is None:
            if word != self._uniform_attr:
                attrs = self._ensure_attrs()
                attrs[col:end] = array.array("Q", [word]) * n
        else:
            self._attrs[col:end] = array.array("Q", [word]) * n

        if cell_ext is not None:
            if self._ext is None:
                self._ext = {}
            for idx in range(col, end):
                self._ext[idx] = cell_ext
        elif self._ext:
            for idx in range(col, end):
                self._ext.pop(idx, None)

        self._used = max(self._used, end)
        self._text_used = max(self._text_used, end)
        self.stamp()

    def write_wide(
        self,
        col: int,
        ch: str,
        pen_word: int,
        cell_ext: _CellExt | None,
    ) -> None:
        """Write a 2-column glyph across ``col`` and ``col + 1``."""
        self._heal_wide_boundary(col)
        self._heal_wide_boundary(col + 1)
        self._set_raw(col, ch, pen_word, WidthRole.WIDE, cell_ext)
        self._set_raw(col + 1, "", pen_word, WidthRole.SPACER_TAIL, cell_ext)
        self._used = max(self._used, col + 2)
        self._text_used = max(self._text_used, col + 2)
        self.stamp()

    def append_grapheme(
        self,
        col: int,
        ch: str,
        *,
        width_override: int,
        bce_word: int = 0,
    ) -> None:
        """Append zero-width or cluster-extending *ch* onto cell *col*."""
        cols = len(self._chars)
        if col < 0 or col >= cols:
            return
        target = col
        if (
            self._has_wide
            and self._attrs is not None
            and raw_width_bits(self._attrs[col]) == int(WidthRole.SPACER_TAIL)
            and col > 0
        ):
            target = col - 1
        self._chars[target] = self._chars[target] + ch
        cur_role = raw_width_bits(self._attrs[target]) if self._attrs is not None else 0
        if (
            width_override == 2
            and cur_role == int(WidthRole.NARROW)
            and target + 1 < cols
        ):
            base_pen = self.attr_at(target) & _PEN_BASE_MASK
            base_ext = self.ext_at(target)
            self._heal_wide_boundary(target + 1)
            self._set_raw(
                target, self._chars[target], base_pen, WidthRole.WIDE, base_ext
            )
            self._set_raw(target + 1, "", base_pen, WidthRole.SPACER_TAIL, base_ext)
            self._used = max(self._used, target + 2)
            self._text_used = max(self._text_used, target + 2)
        elif (
            width_override == 1
            and cur_role == int(WidthRole.WIDE)
            and target + 1 < cols
        ):
            attrs = self._ensure_attrs()
            attrs[target] &= _WIDTH_CLEAR_MASK
            self._set_raw(target + 1, " ", bce_word, WidthRole.NARROW, None)
            if self._text_used == target + 2:
                self._text_used = target + 1
                if self._used == target + 2 and bce_word == 0:
                    self._used = target + 1
        self.stamp()

    def clear_range(
        self,
        start: int,
        end: int,
        bce_word: int,
        bce_ext: _CellExt | None,
        *,
        selective: bool = False,
    ) -> None:
        """Erase cells in ``[start, end)`` using background color erase (*bce*)."""
        cols = len(self._chars)
        lo = max(0, start)
        hi = min(cols, end)
        if lo >= hi:
            return

        if selective and self._ext:
            for c in range(lo, hi):
                if self.is_protected(c):
                    continue
                self._heal_wide_boundary(c)
                self._set_raw(c, " ", bce_word, WidthRole.NARROW, bce_ext)
            if bce_word != 0 or bce_ext is not None:
                self._used = max(self._used, hi)
            elif hi >= self._used:
                while (
                    self._used > lo
                    and self._chars[self._used - 1] == " "
                    and self.attr_at(self._used - 1) == 0
                    and self.ext_at(self._used - 1) is None
                ):
                    self._used -= 1
            if hi >= self._text_used:
                while self._text_used > lo and not self.is_protected(
                    self._text_used - 1
                ):
                    self._text_used -= 1
            self.stamp()
            return

        if lo == 0 and hi == cols:
            self.reset_blank(cols, bce_word, bce_ext)
            return

        if self._has_wide:
            self._heal_wide_boundary(lo)
            self._heal_wide_boundary(hi - 1)

        n = hi - lo
        self._chars[lo:hi] = [" "] * n
        word = bce_word | (_HAS_EXT_BIT if bce_ext is not None else 0)
        if self._attrs is None:
            if word != self._uniform_attr:
                attrs = self._ensure_attrs()
                attrs[lo:hi] = array.array("Q", [word]) * n
        else:
            self._attrs[lo:hi] = array.array("Q", [word]) * n

        if bce_ext is not None:
            if self._ext is None:
                self._ext = {}
            for c in range(lo, hi):
                self._ext[c] = bce_ext
            self._used = max(self._used, hi)
        else:
            if self._ext:
                for c in range(lo, hi):
                    self._ext.pop(c, None)
            if bce_word != 0:
                self._used = max(self._used, hi)
            elif hi >= self._used:
                # Clamp used to lo without erasing explicit spaces left of lo.
                self._used = min(self._used, lo)
        if hi >= self._text_used:
            self._text_used = min(self._text_used, lo)
        self.stamp()

    def insert_blanks(
        self,
        col: int,
        count: int,
        *,
        left: int,
        right: int,
        bce_word: int,
        bce_ext: _CellExt | None = None,
    ) -> None:
        """Insert *count* blank cells at *col* within ``[left, right]`` (``ICH``)."""
        if not (left <= col <= right) or count <= 0:
            return
        shift = min(count, right - col + 1)

        if self._has_wide and self._attrs is not None:
            if raw_width_bits(self._attrs[col]) == int(WidthRole.SPACER_TAIL):
                self._heal_wide_boundary(col)
            if raw_width_bits(self._attrs[right]) == int(WidthRole.WIDE):
                self._heal_wide_boundary(right)

        attrs = (
            self._ensure_attrs()
            if (
                self._attrs is not None
                or bce_word != self._uniform_attr
                or bce_ext is not None
            )
            else None
        )
        for dst in range(right, col + shift - 1, -1):
            src = dst - shift
            self._chars[dst] = self._chars[src]
            if attrs is not None:
                attrs[dst] = attrs[src]
            if self._ext is not None:
                if src in self._ext:
                    self._ext[dst] = self._ext[src]
                else:
                    self._ext.pop(dst, None)

        # Clear right when shifting pushes a WIDE head onto right without its tail.
        cleared_right_head = False
        if (
            self._has_wide
            and attrs is not None
            and raw_width_bits(attrs[right]) == int(WidthRole.WIDE)
        ):
            self._chars[right] = " "
            attrs[right] &= _WIDTH_CLEAR_MASK
            cleared_right_head = True

        for c in range(col, col + shift):
            self._set_raw(c, " ", bce_word, WidthRole.NARROW, bce_ext)
        t_cap = right if cleared_right_head else right + 1
        if self._text_used <= right + 1 and self._text_used > col:
            self._text_used = min(t_cap, self._text_used + shift)
        if self._used <= right + 1 and (
            bce_word != 0 or bce_ext is not None or self._used > col
        ):
            u_cap = (
                right
                if (
                    cleared_right_head
                    and self.attr_at(right) == 0
                    and self.ext_at(right) is None
                )
                else right + 1
            )
            self._used = max(
                self._text_used,
                min(u_cap, max(self._used + shift, col + shift)),
            )
        self.stamp()

    def delete_cells(
        self,
        col: int,
        count: int,
        *,
        left: int,
        right: int,
        bce_word: int,
        bce_ext: _CellExt | None = None,
    ) -> None:
        """Delete *count* cells at *col* within ``[left, right]`` (``DCH``)."""
        if not (left <= col <= right) or count <= 0:
            return
        shift = min(count, right - col + 1)

        if self._has_wide and self._attrs is not None:
            if raw_width_bits(self._attrs[col]) == int(WidthRole.SPACER_TAIL):
                self._heal_wide_boundary(col)
            if col + shift <= right and raw_width_bits(self._attrs[col + shift]) == int(
                WidthRole.SPACER_TAIL
            ):
                self._heal_wide_boundary(col + shift)
            if raw_width_bits(self._attrs[right]) == int(WidthRole.WIDE):
                self._heal_wide_boundary(right)

        attrs = (
            self._ensure_attrs()
            if (
                self._attrs is not None
                or bce_word != self._uniform_attr
                or bce_ext is not None
            )
            else None
        )
        for dst in range(col, right - shift + 1):
            src = dst + shift
            self._chars[dst] = self._chars[src]
            if attrs is not None:
                attrs[dst] = attrs[src]
            if self._ext is not None:
                if src in self._ext:
                    self._ext[dst] = self._ext[src]
                else:
                    self._ext.pop(dst, None)

        for c in range(right - shift + 1, right + 1):
            self._set_raw(c, " ", bce_word, WidthRole.NARROW, bce_ext)
        if self._used <= right + 1:
            if bce_word != 0 or bce_ext is not None:
                self._used = max(self._used, right + 1)
            elif self._used > col:
                self._used = max(col, self._used - shift)
        if self._text_used <= right + 1 and self._text_used > col:
            self._text_used = max(col, self._text_used - shift)
        self.stamp()

    def resize(self, cols: int) -> None:
        """Resize row in place to *cols* columns (non-reflow path)."""
        cur = len(self._chars)
        if cols == cur:
            return
        self._wrapped = False
        if cols < cur:
            healed_edge = False
            if (
                cols > 0
                and self._has_wide
                and self._attrs is not None
                and raw_width_bits(self._attrs[cols - 1])
                in (int(WidthRole.WIDE), int(WidthRole.SPACER_HEAD))
            ):
                self._chars[cols - 1] = " "
                self._attrs[cols - 1] &= _WIDTH_CLEAR_MASK
                healed_edge = True
            del self._chars[cols:]
            if self._attrs is not None:
                del self._attrs[cols:]
            if self._ext:
                for k in [k for k in self._ext if k >= cols]:
                    del self._ext[k]
            t_lim = cols - 1 if healed_edge else cols
            self._text_used = min(self._text_used, t_lim)
            u_lim = (
                cols - 1
                if (
                    healed_edge
                    and self.attr_at(cols - 1) == 0
                    and self.ext_at(cols - 1) is None
                )
                else cols
            )
            self._used = max(self._text_used, min(self._used, u_lim))
        else:
            if cur > 0:
                self.clear_spacer_head(cur - 1)
            pad = cols - cur
            self._chars.extend([" "] * pad)
            if self._attrs is not None:
                self._attrs.extend([0] * pad)
            elif self._uniform_attr != 0:
                attrs = array.array("Q", [self._uniform_attr]) * cur
                attrs.extend([0] * pad)
                self._attrs = attrs
        self.stamp()

    def clone(self) -> _Row:
        """Return an independent snapshot copy isolated from live counters."""
        dup = _Row.__new__(_Row)
        dup._chars = self._chars.copy()
        dup._attrs = array.array("Q", self._attrs) if self._attrs is not None else None
        dup._uniform_attr = self._uniform_attr
        dup._has_wide = self._has_wide
        dup._ext = self._ext.copy() if self._ext else None
        dup._used = self._used
        dup._text_used = self._text_used
        dup._wrapped = self._wrapped
        dup._gen_counter = _RowGenCounter(self._content_gen)
        dup._content_gen = self._content_gen
        dup._frozen = self._frozen
        return dup

    def debug_fingerprint(self) -> tuple[object, ...]:
        """Return complete structural state tuple for write-barrier verification."""
        return (
            self._wrapped,
            tuple(self._chars),
            tuple(self._attrs) if self._attrs is not None else self._uniform_attr,
            tuple(sorted(self._ext.items())) if self._ext else (),
            self._used,
            self._text_used,
        )

    def freeze(self) -> FrozenRow:
        """Seal this row into a compact run-length-encoded :class:`FrozenRow`."""
        if self._frozen is not None:
            return self._frozen
        cols = len(self._chars)
        t_used = self._text_used
        text = "".join(self._chars[:t_used])
        cells: tuple[str, ...] | None = None
        if len(text) != t_used or "" in self._chars[:t_used]:
            cells = tuple(self._chars[:t_used])

        u = min(cols, max(self._used, t_used))
        runs: tuple[tuple[int, int], ...]
        if u == 0:
            runs = ()
        elif self._attrs is None:
            runs = () if self._uniform_attr == 0 else ((u, self._uniform_attr),)
        else:
            run_list: list[tuple[int, int]] = []
            cur_word = self._attrs[0]
            for c in range(1, u):
                w = self._attrs[c]
                if w != cur_word:
                    run_list.append((c, cur_word))
                    cur_word = w
            if cur_word != 0:
                run_list.append((u, cur_word))
            runs = tuple(run_list)

        exts = tuple(sorted(self._ext.items())) if self._ext else ()
        fr = FrozenRow(
            text=text,
            cols=cols,
            runs=runs,
            exts=exts,
            cells=cells,
            wrapped=self._wrapped,
        )
        self._frozen = fr
        return fr

    def rstrip_text(self) -> str:
        """Return row text up to ``text_used`` written columns."""
        return "".join(self._chars[: self._text_used])


@dataclass(frozen=True, slots=True)
class _FrozenHistory(collections.abc.Sequence[FrozenRow]):
    """Immutable 256-row copy-on-write block snapshot of sealed scrollback."""

    blocks: tuple[tuple[FrozenRow, ...], ...] = ()
    tail: tuple[FrozenRow, ...] = ()
    head_offset: int = 0
    length: int = 0

    @classmethod
    def from_sequence(
        cls,
        rows: collections.abc.Sequence[FrozenRow],
    ) -> _FrozenHistory:
        """Pack an arbitrary :class:`FrozenRow` sequence into 256-row blocks."""
        if isinstance(rows, _FrozenHistory):
            return rows
        n = len(rows)
        if n == 0:
            return _EMPTY_FROZEN_HISTORY
        full_blocks = n >> _HISTORY_BLOCK_SHIFT
        blocks = tuple(
            tuple(rows[i << _HISTORY_BLOCK_SHIFT : (i + 1) << _HISTORY_BLOCK_SHIFT])
            for i in range(full_blocks)
        )
        tail = tuple(rows[full_blocks << _HISTORY_BLOCK_SHIFT :])
        return cls(blocks=blocks, tail=tail, head_offset=0, length=n)

    def __len__(self) -> int:
        return self.length

    def at(self, index: int) -> FrozenRow:
        """Return :class:`FrozenRow` at normalized ``0 <= index < length``."""
        raw = self.head_offset + index
        b_idx = raw >> _HISTORY_BLOCK_SHIFT
        off = raw & _HISTORY_BLOCK_MASK
        if b_idx < len(self.blocks):
            return self.blocks[b_idx][off]
        return self.tail[off]

    @t.overload
    def __getitem__(self, index: int) -> FrozenRow: ...

    @t.overload
    def __getitem__(self, index: slice) -> tuple[FrozenRow, ...]: ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> FrozenRow | tuple[FrozenRow, ...]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self.length)
            return tuple(self.at(i) for i in range(start, stop, step))
        if isinstance(index, bool) or not isinstance(index, int):
            msg = f"history indices must be int or slice, got {type(index).__name__}"
            raise TypeError(msg)
        norm = index + self.length if index < 0 else index
        if norm < 0 or norm >= self.length:
            msg = f"history index {index} out of range for size {self.length}"
            raise IndexError(msg)
        return self.at(norm)

    def __iter__(self) -> collections.abc.Iterator[FrozenRow]:
        if self.length == 0:
            return
        if not self.blocks:
            yield from self.tail[self.head_offset : self.head_offset + self.length]
            return
        yield from self.blocks[0][self.head_offset :]
        for blk in self.blocks[1:]:
            yield from blk
        yield from self.tail


_EMPTY_FROZEN_HISTORY = _FrozenHistory()


class _HistoryBuffer:
    """Bounded 256-row block scrollback buffer with ``O(1)`` CoW snapshots."""

    __slots__ = ("_blocks", "_head_offset", "_len", "_maxlen", "_snapshot", "_tail")

    def __init__(self, *, maxlen: int) -> None:
        self._maxlen: int = max(0, maxlen)
        self._blocks: list[tuple[FrozenRow, ...]] = []
        self._tail: list[FrozenRow] = []
        self._head_offset: int = 0
        self._len: int = 0
        self._snapshot: _FrozenHistory | None = _EMPTY_FROZEN_HISTORY

    @property
    def maxlen(self) -> int:
        """Return scrollback line capacity (``0`` when disabled)."""
        return self._maxlen

    def __len__(self) -> int:
        return self._len

    def __bool__(self) -> bool:
        return self._len > 0

    def __getitem__(self, index: int) -> FrozenRow:
        if isinstance(index, bool) or not isinstance(index, int):
            msg = f"history index must be int, got {type(index).__name__}"
            raise TypeError(msg)
        norm = index + self._len if index < 0 else index
        if norm < 0 or norm >= self._len:
            msg = f"history index {index} out of range for size {self._len}"
            raise IndexError(msg)
        raw = self._head_offset + norm
        b_idx = raw >> _HISTORY_BLOCK_SHIFT
        off = raw & _HISTORY_BLOCK_MASK
        if b_idx < len(self._blocks):
            return self._blocks[b_idx][off]
        return self._tail[off]

    def __iter__(self) -> collections.abc.Iterator[FrozenRow]:
        return iter(self.freeze())

    def clear(self) -> None:
        """Discard all stored scrollback blocks."""
        if self._len == 0:
            return
        self._blocks.clear()
        self._tail = []
        self._head_offset = 0
        self._len = 0
        self._snapshot = _EMPTY_FROZEN_HISTORY

    def append(self, row: FrozenRow) -> bool:
        """Append *row*, returning ``True`` when the oldest row was evicted."""
        if self._maxlen <= 0:
            return False
        self._snapshot = None
        self._tail.append(row)
        if len(self._tail) == _HISTORY_BLOCK_SIZE:
            self._blocks.append(tuple(self._tail))
            self._tail = []
        if self._len == self._maxlen:
            self._head_offset += 1
            if self._head_offset == _HISTORY_BLOCK_SIZE:
                del self._blocks[0]
                self._head_offset = 0
            return True
        self._len += 1
        return False

    def replace_all(self, rows: collections.abc.Sequence[RowLike]) -> int:
        """Replace buffer with trailing ``maxlen`` frozen rows; return evicted."""
        self.clear()
        n = len(rows)
        if self._maxlen <= 0 or n == 0:
            return n
        evicted = max(0, n - self._maxlen)
        kept_len = n - evicted
        full_blocks = kept_len >> _HISTORY_BLOCK_SHIFT
        blocks: list[tuple[FrozenRow, ...]] = []
        pos = evicted
        for _ in range(full_blocks):
            nxt = pos + _HISTORY_BLOCK_SIZE
            chunk = rows[pos:nxt]
            blocks.append(
                tuple(r if isinstance(r, FrozenRow) else r.freeze() for r in chunk)
            )
            pos = nxt
        self._blocks = blocks
        self._tail = [
            r if isinstance(r, FrozenRow) else r.freeze() for r in rows[pos:n]
        ]
        self._head_offset = 0
        self._len = kept_len
        self._snapshot = None
        return evicted

    def freeze(self) -> _FrozenHistory:
        """Capture an ``O(1)`` CoW :class:`_FrozenHistory` snapshot."""
        if self._snapshot is not None:
            return self._snapshot
        if self._len == 0:
            self._snapshot = _EMPTY_FROZEN_HISTORY
            return _EMPTY_FROZEN_HISTORY
        snap = _FrozenHistory(
            blocks=tuple(self._blocks),
            tail=tuple(self._tail),
            head_offset=self._head_offset,
            length=self._len,
        )
        self._snapshot = snap
        return snap


type RowLike = _Row | FrozenRow
