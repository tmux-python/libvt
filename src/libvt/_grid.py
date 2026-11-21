"""Packed row storage using ``chars: list[str]`` and 64-bit ``array('Q')`` words.

Bit layout of each 64-bit unsigned attribute word (``0`` == blank default cell):

- bits ``0..24`` (25b): foreground color
- bits ``25..49`` (25b): background color
- bits ``50..56`` (7b): ``bold``, ``dim``, ``italic``, ``blink``, ``reverse``,
  ``hidden``, ``strikethrough``
- bits ``57..59`` (3b): :class:`~libvt.style.UnderlineStyle`
- bits ``60..61`` (2b): :class:`~libvt._width.WidthRole`
- bit ``62`` (1b): ``overline``
- bit ``63`` (1b): ``HAS_EXT`` gating sparse :class:`_CellExt`
"""

from __future__ import annotations

import array
from dataclasses import dataclass

from libvt._width import WidthRole
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


@dataclass(frozen=True, slots=True)
class _CellExt:
    """Sparse per-cell metadata stored only when ``HAS_EXT`` is set.

    Attributes
    ----------
    underline_color : Color
        Explicit underline color (``SGR 58``), if any.
    hyperlink : Hyperlink | None
        Active ``OSC 8`` hyperlink, if any.
    prompt_mark : str
        ``OSC 133`` marker kind recorded at this cell, if any.
    """

    underline_color: Color = None
    hyperlink: Hyperlink | None = None
    prompt_mark: str = ""


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
    if 1 <= bits <= 16:
        return AnsiColor(bits - 1)
    if _IDX_BASE <= bits < _IDX_BASE + 256:
        return IndexedColor(bits - _IDX_BASE)
    return None


def pack_pen_base(style: Style) -> int:
    """Pack *style* into a 64-bit word with ``WidthRole.NARROW`` and no ``HAS_EXT``."""
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


def unpack_width_role(word: int) -> WidthRole:
    """Extract :class:`~libvt._width.WidthRole` from a 64-bit cell word."""
    return WidthRole((word >> _WIDTH_SHIFT) & _WIDTH_MASK)


def unpack_style(word: int, ext: _CellExt | None = None) -> Style:
    """Unpack :class:`~libvt.style.Style` from a 64-bit cell word and optional ext."""
    style_bits = word & _WIDTH_CLEAR_MASK & ~_HAS_EXT_BIT
    ul_color = ext.underline_color if ext is not None else None
    if style_bits == 0 and ul_color is None:
        return DEFAULT_STYLE
    return Style(
        fg=_unpack_color(word & _COLOR_MASK),
        bg=_unpack_color((word >> _BG_SHIFT) & _COLOR_MASK),
        underline_color=ul_color,
        bold=bool(word & _BOLD_BIT),
        dim=bool(word & _DIM_BIT),
        italic=bool(word & _ITALIC_BIT),
        underline=UnderlineStyle((word >> _UL_SHIFT) & _UL_MASK),
        blink=bool(word & _BLINK_BIT),
        reverse=bool(word & _REVERSE_BIT),
        hidden=bool(word & _HIDDEN_BIT),
        strikethrough=bool(word & _STRIKE_BIT),
        overline=bool(word & _OVERLINE_BIT),
    )


def make_cell_ext(
    style: Style,
    hyperlink: Hyperlink | None,
    prompt_mark: str = "",
) -> _CellExt | None:
    """Build a :class:`_CellExt` when sparse cell metadata is non-empty."""
    if style.underline_color is None and hyperlink is None and not prompt_mark:
        return None
    return _CellExt(
        underline_color=style.underline_color,
        hyperlink=hyperlink,
        prompt_mark=prompt_mark,
    )


class _Row:
    """Compact mutable terminal row backed by ``list[str]`` and ``array('Q')``."""

    __slots__ = ("attrs", "chars", "ext", "used", "wrapped")

    def __init__(self, cols: int) -> None:
        self.chars: list[str] = [" "] * cols
        self.attrs: array.array[int] = array.array("Q", [0]) * cols
        self.ext: dict[int, _CellExt] = {}
        self.used: int = 0
        self.wrapped: bool = False

    @property
    def cols(self) -> int:
        """Return number of columns in this row."""
        return len(self.chars)

    def _heal_wide_boundary(self, col: int) -> None:
        """Clear orphaned half of a wide glyph if *col* splits a 2-cell pair."""
        if not 0 <= col < len(self.chars):
            return
        role = unpack_width_role(self.attrs[col])
        if (
            role == WidthRole.WIDE
            and col + 1 < len(self.chars)
            and unpack_width_role(self.attrs[col + 1]) == WidthRole.SPACER_TAIL
        ):
            self.chars[col + 1] = " "
            self.attrs[col + 1] &= _WIDTH_CLEAR_MASK
        elif (
            role == WidthRole.SPACER_TAIL
            and col - 1 >= 0
            and unpack_width_role(self.attrs[col - 1]) == WidthRole.WIDE
        ):
            self.chars[col - 1] = " "
            self.attrs[col - 1] &= _WIDTH_CLEAR_MASK

    def _set_raw(
        self,
        col: int,
        ch: str,
        pen_word: int,
        role: WidthRole,
        cell_ext: _CellExt | None,
    ) -> None:
        word = (pen_word & _WIDTH_CLEAR_MASK) | (int(role) << _WIDTH_SHIFT)
        if cell_ext is not None:
            word |= _HAS_EXT_BIT
            self.ext[col] = cell_ext
        elif self.ext:
            self.ext.pop(col, None)
        self.chars[col] = ch
        self.attrs[col] = word

    def write_ascii_slice(
        self,
        col: int,
        seg: str,
        pen_word: int,
        cell_ext: _CellExt | None,
    ) -> None:
        """Write a contiguous narrow ASCII slice starting at *col*."""
        end = col + len(seg)
        self._heal_wide_boundary(col)
        self._heal_wide_boundary(end - 1)
        self.chars[col:end] = list(seg)
        word = pen_word & _WIDTH_CLEAR_MASK
        if cell_ext is not None:
            word |= _HAS_EXT_BIT
            for c in range(col, end):
                self.ext[c] = cell_ext
        elif self.ext:
            for c in range(col, end):
                self.ext.pop(c, None)
        self.attrs[col:end] = array.array("Q", [word]) * len(seg)
        self.used = max(self.used, end)

    def write_narrow(
        self,
        col: int,
        ch: str,
        pen_word: int,
        cell_ext: _CellExt | None,
    ) -> None:
        """Write a 1-column character at *col*, healing any split wide glyph."""
        self._heal_wide_boundary(col)
        self._set_raw(col, ch, pen_word, WidthRole.NARROW, cell_ext)
        self.used = max(self.used, col + 1)

    def write_wide(
        self,
        col: int,
        ch: str,
        pen_word: int,
        cell_ext: _CellExt | None,
    ) -> None:
        """Write a 2-column glyph at ``(col, col + 1)``, healing split neighbors."""
        self._heal_wide_boundary(col)
        self._heal_wide_boundary(col + 1)
        self._set_raw(col, ch, pen_word, WidthRole.WIDE, cell_ext)
        self._set_raw(col + 1, "", pen_word, WidthRole.SPACER_TAIL, cell_ext)
        self.used = max(self.used, col + 2)

    def append_grapheme(
        self,
        col: int,
        ch: str,
        *,
        widen: bool = False,
        pen_word: int = 0,
        cell_ext: _CellExt | None = None,
    ) -> None:
        """Append combining/ZWJ/VS16 codepoint *ch* to the cell at *col*."""
        target = col
        if (
            unpack_width_role(self.attrs[target]) == WidthRole.SPACER_TAIL
            and target > 0
        ):
            target -= 1
        self.chars[target] = self.chars[target] + ch
        if (
            widen
            and unpack_width_role(self.attrs[target]) == WidthRole.NARROW
            and target + 1 < len(self.chars)
        ):
            self._heal_wide_boundary(target + 1)
            self.attrs[target] = (self.attrs[target] & _WIDTH_CLEAR_MASK) | (
                int(WidthRole.WIDE) << _WIDTH_SHIFT
            )
            self._set_raw(
                target + 1,
                "",
                pen_word,
                WidthRole.SPACER_TAIL,
                cell_ext,
            )
            self.used = max(self.used, target + 2)

    def clear_range(
        self,
        start: int,
        end: int,
        bce_word: int = 0,
        bce_ext: _CellExt | None = None,
    ) -> None:
        """Erase ``[start, end)`` using background color erase (*bce_word*)."""
        cols = len(self.chars)
        lo = max(0, start)
        hi = min(cols, end)
        if lo >= hi:
            return
        self._heal_wide_boundary(lo)
        self._heal_wide_boundary(hi - 1)
        for c in range(lo, hi):
            self._set_raw(c, " ", bce_word, WidthRole.NARROW, bce_ext)
        if hi == cols:
            self.wrapped = False
            if bce_word == 0 and bce_ext is None:
                self.used = min(self.used, lo)
                while (
                    self.used > 0
                    and self.chars[self.used - 1] == " "
                    and self.attrs[self.used - 1] == 0
                ):
                    self.used -= 1
            else:
                self.used = hi
        elif bce_word != 0 or bce_ext is not None:
            self.used = max(self.used, hi)

    def insert_blanks(
        self,
        col: int,
        count: int,
        *,
        left: int,
        right: int,
        bce_word: int = 0,
    ) -> None:
        """Insert *count* blank cells at *col* within ``[left, right]`` (``ICH``)."""
        if not left <= col <= right or count <= 0:
            return
        self._heal_wide_boundary(col)
        self._heal_wide_boundary(right)
        shift = min(count, right - col + 1)
        for dst in range(right, col + shift - 1, -1):
            src = dst - shift
            self.chars[dst] = self.chars[src]
            self.attrs[dst] = self.attrs[src]
            if src in self.ext:
                self.ext[dst] = self.ext[src]
            else:
                self.ext.pop(dst, None)
        for c in range(col, col + shift):
            self._set_raw(c, " ", bce_word, WidthRole.NARROW, None)
        self._heal_wide_boundary(right)
        if self.used > col:
            self.used = min(right + 1, self.used + shift)

    def delete_cells(
        self,
        col: int,
        count: int,
        *,
        left: int,
        right: int,
        bce_word: int = 0,
    ) -> None:
        """Delete *count* cells at *col* within ``[left, right]`` (``DCH``)."""
        if not left <= col <= right or count <= 0:
            return
        self._heal_wide_boundary(col)
        shift = min(count, right - col + 1)
        if col + shift <= right:
            self._heal_wide_boundary(col + shift)
        for dst in range(col, right - shift + 1):
            src = dst + shift
            self.chars[dst] = self.chars[src]
            self.attrs[dst] = self.attrs[src]
            if src in self.ext:
                self.ext[dst] = self.ext[src]
            else:
                self.ext.pop(dst, None)
        for c in range(right - shift + 1, right + 1):
            self._set_raw(c, " ", bce_word, WidthRole.NARROW, None)
        if bce_word != 0:
            self.used = max(self.used, right + 1)
        elif self.used > col:
            self.used = max(col, self.used - shift)

    def resize(self, cols: int) -> None:
        """Resize row in place to *cols* columns."""
        cur = len(self.chars)
        if cols == cur:
            return
        if cols < cur:
            if cols > 0 and unpack_width_role(self.attrs[cols - 1]) == WidthRole.WIDE:
                self.chars[cols - 1] = " "
                self.attrs[cols - 1] &= _WIDTH_CLEAR_MASK
            del self.chars[cols:]
            del self.attrs[cols:]
            self.used = min(self.used, cols)
            if self.ext:
                for k in [k for k in self.ext if k >= cols]:
                    del self.ext[k]
        else:
            pad = cols - cur
            self.chars.extend([" "] * pad)
            self.attrs.extend([0] * pad)

    def clone(self) -> _Row:
        """Return an independent shallow-immutable copy of this row."""
        dup = _Row.__new__(_Row)
        dup.chars = self.chars.copy()
        dup.attrs = array.array("Q", self.attrs)
        dup.ext = self.ext.copy()
        dup.used = self.used
        dup.wrapped = self.wrapped
        return dup

    def rstrip_text(self) -> str:
        """Return row text up to ``used`` written columns."""
        return "".join(self.chars[: self.used])
