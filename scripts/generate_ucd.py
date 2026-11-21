"""Generate the compact two-stage Unicode 17.0 width & UAX #29 table in libvt._ucd."""

from __future__ import annotations

import base64
import pathlib
import unicodedata
import zlib

PROP_NONE = 0
PROP_CONTROL = 1
PROP_EXTEND = 2
PROP_ZWJ = 3
PROP_REGIONAL_INDICATOR = 4
PROP_SPACING_MARK = 5
PROP_EXTENDED_PICTOGRAPHIC = 6
PROP_INCB_CONSONANT = 7
PROP_INCB_LINKER = 8
PROP_VS15 = 9
PROP_VS16 = 10
PROP_EMOJI_MODIFIER = 11

EXT_PICT_BIT = 0x40
BIDI_SPOOF_BIT = 0x80

# Unicode Indic_Conjunct_Break=Linker (Virama / Halant) codepoints across Indic scripts.
INCB_LINKERS: frozenset[int] = frozenset(
    {
        0x094D,  # Devanagari
        0x09CD,  # Bengali
        0x0A4D,  # Gurmukhi
        0x0ACD,  # Gujarati
        0x0B4D,  # Oriya
        0x0BCD,  # Tamil
        0x0C4D,  # Telugu
        0x0CCD,  # Kannada
        0x0D4D,  # Malayalam
        0x0DCA,  # Sinhala
        0x1039,  # Myanmar
        0x17D2,  # Khmer
        0x1A60,  # Tai Tham
        0x1B44,  # Balinese
        0xA9C0,  # Javanese
    },
)

INCB_CONSONANT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0915, 0x0939),
    (0x0958, 0x095F),
    (0x0978, 0x097F),
    (0x0995, 0x09B9),
    (0x09DC, 0x09DF),
    (0x09F0, 0x09F1),
    (0x0A15, 0x0A39),
    (0x0A59, 0x0A5E),
    (0x0A95, 0x0AB9),
    (0x0B15, 0x0B39),
    (0x0B5C, 0x0B5D),
    (0x0B95, 0x0BB9),
    (0x0C15, 0x0C39),
    (0x0C58, 0x0C5A),
    (0x0C95, 0x0CB9),
    (0x0D15, 0x0D3A),
    (0x0D9A, 0x0DC6),
)

# Official Unicode Extended_Pictographic core blocks for UAX #29 GB11 ZWJ sequences.
EXT_PICT_RANGES: tuple[tuple[int, int], ...] = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x2199),
    (0x21A9, 0x21AA),
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x23CF, 0x23CF),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x24C2, 0x24C2),
    (0x25AA, 0x25AB),
    (0x25B6, 0x25B6),
    (0x25C0, 0x25C0),
    (0x25FB, 0x25FE),
    (0x2600, 0x27BF),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3299),
    (0x1F000, 0x1FAFF),
)

BIDI_SPOOF: frozenset[int] = frozenset(
    {
        0x061C,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    },
)


def _in_ranges(cp: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def _classify(cp: int) -> int:
    if 0xD800 <= cp <= 0xDFFF:
        return PROP_CONTROL << 2
    ch = chr(cp)
    bidi_bit = BIDI_SPOOF_BIT if cp in BIDI_SPOOF else 0
    if cp < 0x20 or 0x7F <= cp < 0xA0:
        return (PROP_CONTROL << 2) | bidi_bit
    if 0x20 <= cp <= 0x7E:
        return 1  # width=1, prop=NONE

    if cp == 0x200D:
        return (PROP_ZWJ << 2) | bidi_bit
    if cp == 0xFE0E:
        return (PROP_VS15 << 2) | bidi_bit
    if cp == 0xFE0F:
        return (PROP_VS16 << 2) | bidi_bit
    if 0xFE00 <= cp <= 0xFE0D or 0xE0100 <= cp <= 0xE01EF:
        return (PROP_EXTEND << 2) | bidi_bit
    if 0x1F1E6 <= cp <= 0x1F1FF:
        # Single Regional Indicator has base width 1; pair widens to 2 under ?2027.
        return 1 | (PROP_REGIONAL_INDICATOR << 2) | bidi_bit
    if 0x1F3FB <= cp <= 0x1F3FF:
        return 2 | (PROP_EMOJI_MODIFIER << 2) | EXT_PICT_BIT | bidi_bit
    if cp in INCB_LINKERS:
        return (PROP_INCB_LINKER << 2) | bidi_bit

    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me") or unicodedata.combining(ch):
        return (PROP_EXTEND << 2) | bidi_bit
    if cat == "Mc":
        eaw = unicodedata.east_asian_width(ch)
        w = 2 if eaw in ("W", "F") else 1
        return w | (PROP_SPACING_MARK << 2) | bidi_bit
    if cat == "Cf":
        return (PROP_CONTROL << 2) | bidi_bit

    eaw = unicodedata.east_asian_width(ch)
    width = 2 if eaw in ("W", "F") else 1

    if _in_ranges(cp, INCB_CONSONANT_RANGES) and cat == "Lo":
        return width | (PROP_INCB_CONSONANT << 2) | bidi_bit
    if _in_ranges(cp, EXT_PICT_RANGES) and cat in ("So", "Sk", "Sm", "Po"):
        return width | (PROP_EXTENDED_PICTOGRAPHIC << 2) | EXT_PICT_BIT | bidi_bit

    return width | bidi_bit


def build_tables() -> tuple[bytes, bytes]:
    """Build deduplicated stage-1 (u16 LE) and stage-2 (u8) 256-codepoint tables."""
    block_map: dict[bytes, int] = {}
    stage1_indices: list[int] = []
    stage2_blocks: list[bytes] = []

    for block_idx in range(0x110000 >> 8):
        base = block_idx << 8
        block = bytes(_classify(base + offset) for offset in range(256))
        idx = block_map.get(block)
        if idx is None:
            idx = len(stage2_blocks)
            block_map[block] = idx
            stage2_blocks.append(block)
        stage1_indices.append(idx)

    stage1_bytes = b"".join(idx.to_bytes(2, "little") for idx in stage1_indices)
    stage2_bytes = b"".join(stage2_blocks)
    return stage1_bytes, stage2_bytes


def render_module(stage1: bytes, stage2: bytes) -> str:
    """Format ``src/libvt/_ucd.py`` source text."""
    blob = len(stage1).to_bytes(4, "little") + stage1 + stage2
    compressed = base64.b85encode(zlib.compress(blob, level=9)).decode("ascii")
    lines = [compressed[i : i + 76] for i in range(0, len(compressed), 76)]
    b85_literal = "\n".join(f'    "{ln}"' for ln in lines)

    ver = unicodedata.unidata_version
    return f'''"""Generated two-stage Unicode {ver} cell-width and UAX #29 table.

Regenerate with ``uv run python scripts/generate_ucd.py``.
"""

from __future__ import annotations

import array
import base64
import enum
import sys
import zlib


class WidthRole(enum.IntEnum):
    """Column occupancy role of a terminal grid cell."""

    NARROW = 0
    WIDE = 1
    SPACER_TAIL = 2
    SPACER_HEAD = 3


class ClusterState(enum.IntEnum):
    """Scalar UAX #29 grapheme cluster continuation state."""

    NONE = 0
    BASE = 1
    RI_ODD = 2
    EXT_PICT = 3
    EXT_PICT_ZWJ = 4
    INCB_CONS = 5
    INCB_LINKER = 6


PROP_NONE = 0
PROP_CONTROL = 1
PROP_EXTEND = 2
PROP_ZWJ = 3
PROP_REGIONAL_INDICATOR = 4
PROP_SPACING_MARK = 5
PROP_EXTENDED_PICTOGRAPHIC = 6
PROP_INCB_CONSONANT = 7
PROP_INCB_LINKER = 8
PROP_VS15 = 9
PROP_VS16 = 10
PROP_EMOJI_MODIFIER = 11

EXT_PICT_BIT = 0x40
BIDI_SPOOF_BIT = 0x80

UNICODE_VERSION = "{unicodedata.unidata_version}"

_PACKED_B85 = (
{b85_literal}
)


def _unpack_tables() -> tuple[array.array[int], bytes]:
    raw = zlib.decompress(base64.b85decode(_PACKED_B85))
    s1_len = int.from_bytes(raw[:4], "little")
    s1 = array.array("H")
    s1.frombytes(raw[4 : 4 + s1_len])
    if sys.byteorder != "little":
        s1.byteswap()
    return s1, raw[4 + s1_len :]


_STAGE1, _STAGE2 = _unpack_tables()


def ucd_lookup(cp: int) -> int:
    """Return packed 8-bit width and UAX #29 property entry for codepoint *cp*."""
    if 0 <= cp <= 0x10FFFF:
        return _STAGE2[(_STAGE1[cp >> 8] << 8) | (cp & 0xFF)]
    return PROP_CONTROL << 2


def char_width(ch: str) -> int:
    """Return default terminal column width (``0``, ``1``, or ``2``) for *ch*."""
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E:
        return 1
    return ucd_lookup(cp) & 0b11


def step_cluster(
    prev_state: ClusterState,
    cp: int,
    *,
    mode_2027: bool,
) -> tuple[bool, ClusterState, int]:
    """Evaluate UAX #29 continuation between *prev_state* and codepoint *cp*.

    Returns
    -------
    tuple[bool, ClusterState, int]
        ``(extends_previous, next_state, width_override)`` where
        ``width_override`` is ``-1`` (keep current cluster width), ``1``
        (narrow via ``VS15``), or ``2`` (widen via ``VS16``, emoji modifier,
        ZWJ pictographic, or Regional Indicator pair).
    """
    entry = ucd_lookup(cp)
    prop = (entry >> 2) & 0x0F

    if prev_state != ClusterState.NONE:
        if prop == PROP_EXTEND:
            return True, prev_state, -1
        if prop == PROP_VS16:
            return True, prev_state, (2 if mode_2027 else -1)
        if prop == PROP_VS15:
            return True, prev_state, (1 if mode_2027 else -1)
        if mode_2027:
            if prop == PROP_SPACING_MARK:
                next_st = (
                    prev_state
                    if prev_state in (ClusterState.INCB_CONS, ClusterState.EXT_PICT)
                    else ClusterState.BASE
                )
                return True, next_st, -1
            if prop == PROP_ZWJ:
                next_st = (
                    ClusterState.EXT_PICT_ZWJ
                    if prev_state == ClusterState.EXT_PICT
                    else ClusterState.BASE
                )
                return True, next_st, -1
            if prop == PROP_EMOJI_MODIFIER and prev_state == ClusterState.EXT_PICT:
                return True, ClusterState.EXT_PICT, 2
            if prev_state == ClusterState.EXT_PICT_ZWJ and (entry & EXT_PICT_BIT):
                return True, ClusterState.EXT_PICT, 2
            if prop == PROP_REGIONAL_INDICATOR and prev_state == ClusterState.RI_ODD:
                return True, ClusterState.BASE, 2
            if prop == PROP_INCB_LINKER and prev_state == ClusterState.INCB_CONS:
                return True, ClusterState.INCB_LINKER, -1
            if prop == PROP_INCB_CONSONANT and prev_state == ClusterState.INCB_LINKER:
                return True, ClusterState.INCB_CONS, -1

    # Start of a new base cell (or dropped standalone zero-width character).
    if prop == PROP_REGIONAL_INDICATOR and mode_2027:
        return False, ClusterState.RI_ODD, -1
    if entry & EXT_PICT_BIT:
        return False, ClusterState.EXT_PICT, -1
    if prop == PROP_INCB_CONSONANT:
        return False, ClusterState.INCB_CONS, -1
    if (entry & 0b11) == 0:
        return False, ClusterState.NONE, -1
    return False, ClusterState.BASE, -1


def display_width(text: str, *, mode_2027: bool = True) -> int:
    """Return rendered terminal column width of *text*."""
    if text.isascii():
        return sum(1 for ch in text if " " <= ch <= "~")
    total = 0
    cur_w = 0
    state = ClusterState.NONE
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E:
            total += cur_w
            cur_w = 1
            state = ClusterState.BASE
            continue
        extends, state, w_override = step_cluster(state, cp, mode_2027=mode_2027)
        if extends:
            if w_override != -1:
                cur_w = w_override
        else:
            total += cur_w
            cur_w = ucd_lookup(cp) & 0b11
    return total + cur_w


def sanitize_terminal_text(text: str) -> str:
    """Escape C0, C1, ``DEL``, and BiDi override codepoints for safe display."""
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E:
            out.append(ch)
        elif cp < 0x20 or 0x7F <= cp <= 0x9F or (ucd_lookup(cp) & BIDI_SPOOF_BIT):
            out.append(f"\\\\x{{cp:02x}}" if cp <= 0xFF else f"\\\\u{{cp:04x}}")
        else:
            out.append(ch)
    return "".join(out)


__all__ = [
    "BIDI_SPOOF_BIT",
    "UNICODE_VERSION",
    "ClusterState",
    "WidthRole",
    "char_width",
    "display_width",
    "sanitize_terminal_text",
    "step_cluster",
    "ucd_lookup",
]
'''


def main() -> int:
    """Regenerate ``src/libvt/_ucd.py`` from ``unicodedata``."""
    stage1, stage2 = build_tables()
    target = (
        pathlib.Path(__file__).resolve().parent.parent / "src" / "libvt" / "_ucd.py"
    )
    target.write_text(render_module(stage1, stage2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
