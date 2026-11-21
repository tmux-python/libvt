"""Terminal cell colors, SGR attributes, and hyperlink metadata."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class AnsiColor(enum.IntEnum):
    """Standard and bright ECMA-48 / aixterm 16-color palette indices."""

    BLACK = 0
    RED = 1
    GREEN = 2
    YELLOW = 3
    BLUE = 4
    MAGENTA = 5
    CYAN = 6
    WHITE = 7
    BRIGHT_BLACK = 8
    BRIGHT_RED = 9
    BRIGHT_GREEN = 10
    BRIGHT_YELLOW = 11
    BRIGHT_BLUE = 12
    BRIGHT_MAGENTA = 13
    BRIGHT_CYAN = 14
    BRIGHT_WHITE = 15


@dataclass(frozen=True, slots=True)
class IndexedColor:
    """256-color palette entry (``SGR 38;5;n`` / ``SGR 48;5;n``).

    Attributes
    ----------
    index : int
        Palette index in ``0..255``. Kept distinct from :class:`AnsiColor` for
        indices ``0..15`` so ``SGR 38;5;1`` round-trips separately from
        ``SGR 31``.
    """

    index: int

    def __post_init__(self) -> None:
        """Validate that ``index`` falls within ``0..255``."""
        if not 0 <= self.index <= 255:
            msg = f"IndexedColor index must be in 0..255, got {self.index}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RgbColor:
    """24-bit direct RGB color (``SGR 38;2;r;g;b`` / ``SGR 38:2::r:g:b``).

    Attributes
    ----------
    r : int
        Red channel in ``0..255``.
    g : int
        Green channel in ``0..255``.
    b : int
        Blue channel in ``0..255``.
    """

    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        """Validate that all RGB channels fall within ``0..255``."""
        if not (0 <= self.r <= 255 and 0 <= self.g <= 255 and 0 <= self.b <= 255):
            msg = f"RGB channels must be in 0..255, got ({self.r}, {self.g}, {self.b})"
            raise ValueError(msg)


type Color = AnsiColor | IndexedColor | RgbColor | None


class UnderlineStyle(enum.IntEnum):
    """ITU T.416 / Kitty extended underline styles (``SGR 4:x``)."""

    NONE = 0
    SINGLE = 1
    DOUBLE = 2
    CURLY = 3
    DOTTED = 4
    DASHED = 5


@dataclass(frozen=True, slots=True)
class Hyperlink:
    """``OSC 8`` hyperlink attached to printed cells independent of ``SGR 0``.

    Attributes
    ----------
    uri : str
        Target URI string.
    id : str
        Optional explicit ``id=`` parameter grouping multi-line link spans.
    """

    uri: str
    id: str = ""


@dataclass(frozen=True, slots=True)
class Style:
    """Immutable SGR graphic rendition attributes for a cell or active pen.

    Attributes
    ----------
    fg : Color
        Foreground color, or ``None`` for terminal default foreground.
    bg : Color
        Background color, or ``None`` for terminal default background.
    underline_color : Color
        Underline color (``SGR 58``), or ``None`` to inherit foreground.
    bold : bool
        Bold / increased intensity (``SGR 1``).
    dim : bool
        Faint / decreased intensity (``SGR 2``).
    italic : bool
        Italicized glyph posture (``SGR 3``).
    underline : UnderlineStyle
        Underline style (``SGR 4`` / ``SGR 4:x`` / ``SGR 21``).
    blink : bool
        Blink attribute (``SGR 5`` / ``SGR 6``).
    reverse : bool
        Reverse video foreground/background swap (``SGR 7``).
    hidden : bool
        Concealed / invisible text (``SGR 8``).
    strikethrough : bool
        Crossed-out text (``SGR 9``).
    overline : bool
        Overlined text (``SGR 53``).
    """

    fg: Color = None
    bg: Color = None
    underline_color: Color = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: UnderlineStyle = UnderlineStyle.NONE
    blink: bool = False
    reverse: bool = False
    hidden: bool = False
    strikethrough: bool = False
    overline: bool = False


DEFAULT_STYLE = Style()

__all__ = [
    "DEFAULT_STYLE",
    "AnsiColor",
    "Color",
    "Hyperlink",
    "IndexedColor",
    "RgbColor",
    "Style",
    "UnderlineStyle",
]
