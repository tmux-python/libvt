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
        if self.cells is not None:
            return len(self.cells)
        run_end = self.runs[-1][0] if self.runs else 0
        ext_end = (self.exts[-1][0] + 1) if self.exts else 0
        return max(len(self.text), run_end, ext_end)

    def rstrip_text(self) -> str:
        """Return right-trimmed row text."""
        return self.text

    def char_at(self, col: int) -> str:
        """Return cell string at *col*."""
        if self.cells is not None:
            return self.cells[col] if col < len(self.cells) else " "
        return self.text[col] if col < len(self.text) else " "

    def attr_at(self, col: int) -> int:
        """Return packed 64-bit attribute word at *col*."""
        for end_col, word in self.runs:
            if col < end_col:
                return word
        return 0

    def ext_at(self, col: int) -> _CellExt | None:
        """Return sparse :class:`_CellExt` at *col*, if any."""
        for ext_col, ce in self.exts:
            if ext_col == col:
                return ce
            if ext_col > col:
                break
        return None

    def thaw(self, target_cols: int | None = None) -> _Row:
        """Materialize a mutable :class:`_Row` from this sealed line."""
        width = self.cols if target_cols is None else target_cols
        row = _Row(width)
        row.wrapped = self.wrapped
        limit = min(self.used, width)
        if limit == 0 and not self.runs:
            return row
        for c in range(limit):
            row.chars[c] = self.char_at(c)
        if self.runs:
            attrs = row._ensure_attrs()
            start = 0
            for end_col, word in self.runs:
                stop = min(end_col, width)
                for c in range(start, stop):
                    attrs[c] = word
                    if raw_width_bits(word) != 0:
                        row.has_wide = True
                start = stop
                if start >= width:
                    break
            if width > 0 and raw_width_bits(attrs[width - 1]) == int(WidthRole.WIDE):
                row.chars[width - 1] = " "
                attrs[width - 1] &= _WIDTH_CLEAR_MASK
        if self.exts:
            row.ext = {c: ce for c, ce in self.exts if c < width}
        row.used = limit
        row.text_used = min(
            len(self.cells) if self.cells is not None else len(self.text), width
        )
        return row


class _Row:
    """Mutableviewport row with lazy 64-bit ``array('Q')`` promotion."""

    __slots__ = (
        "attrs",
        "chars",
        "ext",
        "has_wide",
        "text_used",
        "uniform_attr",
        "used",
        "wrapped",
    )

    def __init__(self, cols: int) -> None:
        self.chars: list[str] = [" "] * cols
        self.attrs: array.array[int] | None = None
        self.uniform_attr: int = 0
        self.has_wide: bool = False
        self.ext: dict[int, _CellExt] | None = None
        self.used: int = 0
        self.text_used: int = 0
        self.wrapped: bool = False

    def _ensure_attrs(self) -> array.array[int]:
        if self.attrs is None:
            self.attrs = array.array("Q", [self.uniform_attr]) * len(self.chars)
        return self.attrs

    def attr_at(self, col: int) -> int:
        """Return 64-bit attribute word at *col*."""
        if self.attrs is None:
            return self.uniform_attr
        return self.attrs[col]

    def ext_at(self, col: int) -> _CellExt | None:
        """Return sparse :class:`_CellExt` at *col*, if any."""
        return self.ext.get(col) if self.ext is not None else None

    def is_protected(self, col: int) -> bool:
        """Return ``True`` when cell *col* carries ``DECSCA`` protection."""
        if self.ext is None:
            return False
        ce = self.ext.get(col)
        return bool(ce is not None and ce.protected)

    def reset_blank(
        self,
        cols: int,
        bce_word: int = 0,
        bce_ext: _CellExt | None = None,
    ) -> None:
        """Reset this row in place for zero-allocation scroll recycling."""
        if len(self.chars) != cols:
            self.chars = [" "] * cols
        else:
            self.chars[:] = [" "] * cols
        self.attrs = None
        self.uniform_attr = bce_word | (_HAS_EXT_BIT if bce_ext is not None else 0)
        self.has_wide = False
        if bce_ext is not None:
            self.ext = dict.fromkeys(range(cols), bce_ext)
            self.used = cols
        else:
            if self.ext is not None:
                self.ext.clear()
            self.used = cols if bce_word != 0 else 0
        self.text_used = 0
        self.wrapped = False

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
            if self.ext is None:
                self.ext = {col: cell_ext}
            else:
                self.ext[col] = cell_ext
        elif self.ext is not None:
            self.ext.pop(col, None)
        if role != WidthRole.NARROW:
            self.has_wide = True
        self.chars[col] = ch
        if self.attrs is None:
            if word == self.uniform_attr:
                return
            self._ensure_attrs()[col] = word
        else:
            self.attrs[col] = word

    def _heal_wide_boundary(self, col: int) -> None:
        """Clear both halves when an edit or margin boundary splits a wide glyph."""
        if not self.has_wide or self.attrs is None:
            return
        cols = len(self.chars)
        if col < 0 or col >= cols:
            return
        role = raw_width_bits(self.attrs[col])
        healed_pair_end = -1
        if role == int(WidthRole.WIDE):
            self.chars[col] = " "
            self.attrs[col] &= _WIDTH_CLEAR_MASK
            healed_pair_end = col + 1
            if col + 1 < cols:
                self.chars[col + 1] = " "
                self.attrs[col + 1] &= _WIDTH_CLEAR_MASK
                healed_pair_end = col + 2
        elif role == int(WidthRole.SPACER_TAIL):
            self.chars[col] = " "
            self.attrs[col] &= _WIDTH_CLEAR_MASK
            healed_pair_end = col + 1
            if col - 1 >= 0:
                self.chars[col - 1] = " "
                self.attrs[col - 1] &= _WIDTH_CLEAR_MASK
        if healed_pair_end != -1 and self.text_used == healed_pair_end:
            pair_lo = max(0, healed_pair_end - 2)
            self.text_used = pair_lo
            if (
                self.used == healed_pair_end
                and self.attr_at(healed_pair_end - 1) == 0
                and self.ext_at(healed_pair_end - 1) is None
            ):
                self.used = pair_lo

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
        self.used = max(self.used, col + 1)
        self.text_used = max(self.text_used, col + 1)

    def write_spacer_head(
        self,
        col: int,
        bce_word: int,
        bce_ext: _CellExt | None,
    ) -> None:
        """Write ``WidthRole.SPACER_HEAD`` padding at right margin before wide wrap."""
        self._heal_wide_boundary(col)
        self._set_raw(col, "", bce_word, WidthRole.SPACER_HEAD, bce_ext)
        self.used = max(self.used, col + 1)
        self.text_used = max(self.text_used, col + 1)

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
        if self.has_wide and self.attrs is not None:
            self._heal_wide_boundary(col)
            if end - 1 > col:
                self._heal_wide_boundary(end - 1)
            for idx in range(col, end):
                if (
                    raw_width_bits(self.attrs[idx]) == int(WidthRole.WIDE)
                    and idx + 1 >= end
                    and idx + 1 < len(self.chars)
                ):
                    self.chars[idx + 1] = " "
                    self.attrs[idx + 1] &= _WIDTH_CLEAR_MASK

        self.chars[col:end] = list(text)
        word = pen_word | (_HAS_EXT_BIT if cell_ext is not None else 0)
        if self.attrs is None:
            if word != self.uniform_attr:
                attrs = self._ensure_attrs()
                attrs[col:end] = array.array("Q", [word]) * n
        else:
            self.attrs[col:end] = array.array("Q", [word]) * n

        if cell_ext is not None:
            if self.ext is None:
                self.ext = {}
            for idx in range(col, end):
                self.ext[idx] = cell_ext
        elif self.ext:
            for idx in range(col, end):
                self.ext.pop(idx, None)

        self.used = max(self.used, end)
        self.text_used = max(self.text_used, end)

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
        self.used = max(self.used, col + 2)
        self.text_used = max(self.text_used, col + 2)

    def append_grapheme(
        self,
        col: int,
        ch: str,
        *,
        width_override: int,
        pen_word: int,
        cell_ext: _CellExt | None,
    ) -> None:
        """Append zero-width or cluster-extending *ch* onto cell *col*."""
        cols = len(self.chars)
        if col < 0 or col >= cols:
            return
        target = col
        if (
            self.has_wide
            and self.attrs is not None
            and raw_width_bits(self.attrs[col]) == int(WidthRole.SPACER_TAIL)
            and col > 0
        ):
            target = col - 1
        self.chars[target] = self.chars[target] + ch
        cur_role = raw_width_bits(self.attrs[target]) if self.attrs is not None else 0
        if (
            width_override == 2
            and cur_role == int(WidthRole.NARROW)
            and target + 1 < cols
        ):
            self._heal_wide_boundary(target + 1)
            attrs = self._ensure_attrs()
            attrs[target] = (attrs[target] & _WIDTH_CLEAR_MASK) | (
                int(WidthRole.WIDE) << _WIDTH_SHIFT
            )
            self.has_wide = True
            self._set_raw(target + 1, "", pen_word, WidthRole.SPACER_TAIL, cell_ext)
            self.used = max(self.used, target + 2)
            self.text_used = max(self.text_used, target + 2)
        elif (
            width_override == 1
            and cur_role == int(WidthRole.WIDE)
            and target + 1 < cols
        ):
            attrs = self._ensure_attrs()
            attrs[target] &= _WIDTH_CLEAR_MASK
            self._set_raw(target + 1, " ", pen_word, WidthRole.NARROW, cell_ext)
            if self.text_used == target + 2:
                self.text_used = target + 1
                if self.used == target + 2 and pen_word == 0 and cell_ext is None:
                    self.used = target + 1

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
        cols = len(self.chars)
        lo = max(0, start)
        hi = min(cols, end)
        if lo >= hi:
            return

        if selective and self.ext:
            for c in range(lo, hi):
                if self.is_protected(c):
                    continue
                self._heal_wide_boundary(c)
                self._set_raw(c, " ", bce_word, WidthRole.NARROW, bce_ext)
            if bce_word != 0 or bce_ext is not None:
                self.used = max(self.used, hi)
            elif hi >= self.used:
                while (
                    self.used > lo
                    and self.chars[self.used - 1] == " "
                    and self.attr_at(self.used - 1) == 0
                    and self.ext_at(self.used - 1) is None
                ):
                    self.used -= 1
            if hi >= self.text_used:
                while self.text_used > lo and not self.is_protected(self.text_used - 1):
                    self.text_used -= 1
            return

        if lo == 0 and hi == cols:
            self.reset_blank(cols, bce_word, bce_ext)
            return

        if self.has_wide:
            self._heal_wide_boundary(lo)
            self._heal_wide_boundary(hi - 1)

        n = hi - lo
        self.chars[lo:hi] = [" "] * n
        word = bce_word | (_HAS_EXT_BIT if bce_ext is not None else 0)
        if self.attrs is None:
            if word != self.uniform_attr:
                attrs = self._ensure_attrs()
                attrs[lo:hi] = array.array("Q", [word]) * n
        else:
            self.attrs[lo:hi] = array.array("Q", [word]) * n

        if bce_ext is not None:
            if self.ext is None:
                self.ext = {}
            for c in range(lo, hi):
                self.ext[c] = bce_ext
            self.used = max(self.used, hi)
        else:
            if self.ext:
                for c in range(lo, hi):
                    self.ext.pop(c, None)
            if bce_word != 0:
                self.used = max(self.used, hi)
            elif hi >= self.used:
                # Clamp used to lo without erasing explicit spaces printed left of lo!
                self.used = min(self.used, lo)
        if hi >= self.text_used:
            self.text_used = min(self.text_used, lo)

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

        # Splitting a SPACER_TAIL at col orphans its WIDE head at col - 1.
        if (
            self.has_wide
            and self.attrs is not None
            and raw_width_bits(self.attrs[col]) == int(WidthRole.SPACER_TAIL)
        ):
            if col > 0:
                self.chars[col - 1] = " "
                self.attrs[col - 1] &= _WIDTH_CLEAR_MASK
            self.chars[col] = " "
            self.attrs[col] &= _WIDTH_CLEAR_MASK

        # Discarding a WIDE head at right orphans its SPACER_TAIL at right + 1.
        if (
            self.has_wide
            and self.attrs is not None
            and right + 1 < len(self.chars)
            and raw_width_bits(self.attrs[right + 1]) == int(WidthRole.SPACER_TAIL)
        ):
            self.chars[right + 1] = " "
            self.attrs[right + 1] &= _WIDTH_CLEAR_MASK

        attrs = (
            self._ensure_attrs()
            if (
                self.attrs is not None
                or bce_word != self.uniform_attr
                or bce_ext is not None
            )
            else None
        )
        for dst in range(right, col + shift - 1, -1):
            src = dst - shift
            self.chars[dst] = self.chars[src]
            if attrs is not None:
                attrs[dst] = attrs[src]
            if self.ext is not None:
                if src in self.ext:
                    self.ext[dst] = self.ext[src]
                else:
                    self.ext.pop(dst, None)

        # 3. If shifting pushed a WIDE head onto right without its tail, clear right!
        if (
            self.has_wide
            and attrs is not None
            and raw_width_bits(attrs[right]) == int(WidthRole.WIDE)
        ):
            self.chars[right] = " "
            attrs[right] &= _WIDTH_CLEAR_MASK

        for c in range(col, col + shift):
            self._set_raw(c, " ", bce_word, WidthRole.NARROW, bce_ext)
        if self.used <= right + 1 and (
            bce_word != 0 or bce_ext is not None or self.used > col
        ):
            self.used = min(right + 1, max(self.used + shift, col + shift))
        if self.text_used <= right + 1 and self.text_used > col:
            self.text_used = min(right + 1, self.text_used + shift)

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

        if self.has_wide and self.attrs is not None:
            # 1. Deleting SPACER_TAIL at col orphans WIDE head at col - 1.
            if (
                raw_width_bits(self.attrs[col]) == int(WidthRole.SPACER_TAIL)
                and col > 0
            ):
                self.chars[col - 1] = " "
                self.attrs[col - 1] &= _WIDTH_CLEAR_MASK
            # First surviving cell at col + shift lost its WIDE head if SPACER_TAIL.
            if col + shift <= right and raw_width_bits(self.attrs[col + shift]) == int(
                WidthRole.SPACER_TAIL
            ):
                self.chars[col + shift] = " "
                self.attrs[col + shift] &= _WIDTH_CLEAR_MASK
            # Shifting WIDE head at right leftwards splits (right, right + 1).
            if right + 1 < len(self.chars) and raw_width_bits(
                self.attrs[right + 1]
            ) == int(WidthRole.SPACER_TAIL):
                self.chars[right] = " "
                self.attrs[right] &= _WIDTH_CLEAR_MASK
                self.chars[right + 1] = " "
                self.attrs[right + 1] &= _WIDTH_CLEAR_MASK

        attrs = (
            self._ensure_attrs()
            if (
                self.attrs is not None
                or bce_word != self.uniform_attr
                or bce_ext is not None
            )
            else None
        )
        for dst in range(col, right - shift + 1):
            src = dst + shift
            self.chars[dst] = self.chars[src]
            if attrs is not None:
                attrs[dst] = attrs[src]
            if self.ext is not None:
                if src in self.ext:
                    self.ext[dst] = self.ext[src]
                else:
                    self.ext.pop(dst, None)

        for c in range(right - shift + 1, right + 1):
            self._set_raw(c, " ", bce_word, WidthRole.NARROW, bce_ext)
        if self.used <= right + 1:
            if bce_word != 0 or bce_ext is not None:
                self.used = max(self.used, right + 1)
            elif self.used > col:
                self.used = max(col, self.used - shift)
        if self.text_used <= right + 1 and self.text_used > col:
            self.text_used = max(col, self.text_used - shift)

    def resize(self, cols: int) -> None:
        """Resize row in place to *cols* columns (non-reflow path)."""
        cur = len(self.chars)
        if cols == cur:
            return
        if cols < cur:
            if (
                cols > 0
                and self.has_wide
                and self.attrs is not None
                and raw_width_bits(self.attrs[cols - 1]) == int(WidthRole.WIDE)
            ):
                self.chars[cols - 1] = " "
                self.attrs[cols - 1] &= _WIDTH_CLEAR_MASK
            del self.chars[cols:]
            if self.attrs is not None:
                del self.attrs[cols:]
            self.used = min(self.used, cols)
            self.text_used = min(self.text_used, cols)
            if self.ext:
                for k in [k for k in self.ext if k >= cols]:
                    del self.ext[k]
        else:
            pad = cols - cur
            self.chars.extend([" "] * pad)
            if self.attrs is not None:
                self.attrs.extend([0] * pad)
            elif self.uniform_attr != 0:
                attrs = array.array("Q", [self.uniform_attr]) * cur
                attrs.extend([0] * pad)
                self.attrs = attrs

    def clone(self) -> _Row:
        """Return an independent copy of this row."""
        dup = _Row.__new__(_Row)
        dup.chars = self.chars.copy()
        dup.attrs = array.array("Q", self.attrs) if self.attrs is not None else None
        dup.uniform_attr = self.uniform_attr
        dup.has_wide = self.has_wide
        dup.ext = self.ext.copy() if self.ext else None
        dup.used = self.used
        dup.text_used = self.text_used
        dup.wrapped = self.wrapped
        return dup

    def freeze(self) -> FrozenRow:
        """Seal this row into a compact run-length-encoded :class:`FrozenRow`."""
        cols = len(self.chars)
        t_used = self.text_used
        text = "".join(self.chars[:t_used])
        cells: tuple[str, ...] | None = None
        if len(text) != t_used:
            cells = tuple(self.chars[:t_used])

        u = min(cols, self.used)
        runs: tuple[tuple[int, int], ...]
        if u == 0:
            runs = ()
        elif self.attrs is None:
            runs = () if self.uniform_attr == 0 else ((u, self.uniform_attr),)
        else:
            run_list: list[tuple[int, int]] = []
            cur_word = self.attrs[0]
            for c in range(1, u):
                w = self.attrs[c]
                if w != cur_word:
                    run_list.append((c, cur_word))
                    cur_word = w
            if cur_word != 0:
                run_list.append((u, cur_word))
            runs = tuple(run_list)

        exts = tuple(sorted(self.ext.items())) if self.ext else ()
        return FrozenRow(
            text=text,
            cols=cols,
            runs=runs,
            exts=exts,
            cells=cells,
            wrapped=self.wrapped,
        )

    def rstrip_text(self) -> str:
        """Return row text up to ``text_used`` written columns."""
        return "".join(self.chars[: self.text_used])


type RowLike = _Row | FrozenRow
