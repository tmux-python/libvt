"""Compile SHA-256-pinned Unicode 17.0.0 UCD & Emoji tables into ``libvt._ucd``."""

from __future__ import annotations

import argparse
import base64
import hashlib
import pathlib
import urllib.request
import zlib

UNICODE_VERSION = "17.0.0"
_BASE_URL = f"https://www.unicode.org/Public/{UNICODE_VERSION}"

PINNED_UCD_FILES: dict[str, tuple[str, str]] = {
    "DerivedCoreProperties.txt": (
        f"{_BASE_URL}/ucd/DerivedCoreProperties.txt",
        "24c7fed1195c482faaefd5c1e7eb821c5ee1fb6de07ecdbaa64b56a99da22c08",
    ),
    "DerivedGeneralCategory.txt": (
        f"{_BASE_URL}/ucd/extracted/DerivedGeneralCategory.txt",
        "d62e5bab70ca74f099343f71224fa051cb1fdd61a1ab45c0488c44cfc0b6102e",
    ),
    "EastAsianWidth.txt": (
        f"{_BASE_URL}/ucd/EastAsianWidth.txt",
        "ea7ce50f3444a050333448dffef1cadd9325af55cbb764b4a2280faf52170a33",
    ),
    "GraphemeBreakProperty.txt": (
        f"{_BASE_URL}/ucd/auxiliary/GraphemeBreakProperty.txt",
        "d6b51d1d2ae5c33b451b7ed994b48f1f4dc62b2272a5831e7fd418514a6bae89",
    ),
    "GraphemeBreakTest.txt": (
        f"{_BASE_URL}/ucd/auxiliary/GraphemeBreakTest.txt",
        "e2d134d2c52919bace503ebb6a551c1855fe1a1faec18478c78fff254a1793ec",
    ),
    "emoji-data.txt": (
        f"{_BASE_URL}/ucd/emoji/emoji-data.txt",
        "2cb2bb9455cda83e8481541ecf5b6dfda66a3bb89efa3fa7c5297eccf607b72b",
    ),
    "emoji-variation-sequences.txt": (
        f"{_BASE_URL}/ucd/emoji/emoji-variation-sequences.txt",
        "bb3d09ef03f206012c7532dd52dc0a21c9efddba0135ea4cf0d9201b8b9bba7e",
    ),
}

GCB_IDS: dict[str, int] = {
    "Other": 0,
    "CR": 1,
    "LF": 2,
    "Control": 3,
    "Extend": 4,
    "ZWJ": 5,
    "Regional_Indicator": 6,
    "Prepend": 7,
    "SpacingMark": 8,
    "L": 9,
    "V": 10,
    "T": 11,
    "LV": 12,
    "LVT": 13,
}

INCB_IDS: dict[str, int] = {
    "None": 0,
    "Linker": 1,
    "Consonant": 2,
    "Extend": 3,
}

BIDI_SPOOF: frozenset[int] = frozenset(
    {
        0x061C,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    },
)


def _ensure_verified_file(ucd_dir: pathlib.Path, filename: str) -> pathlib.Path:
    url, expected_sha256 = PINNED_UCD_FILES[filename]
    path = ucd_dir / filename
    if not path.exists():
        ucd_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read()
        path.write_bytes(data)
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        msg = (
            f"SHA-256 mismatch for {filename}: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )
        raise RuntimeError(msg)
    return path


def _parse_codepoint_ranges(
    path: pathlib.Path,
    *,
    value_col: int = 1,
    filter_col1: str | None = None,
) -> dict[int, str]:
    result: dict[int, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(";")]
        if filter_col1 is not None and parts[1] != filter_col1:
            continue
        val = parts[value_col]
        rng = parts[0]
        if ".." in rng:
            lo_s, hi_s = rng.split("..")
            lo, hi = int(lo_s, 16), int(hi_s, 16)
        else:
            lo = hi = int(rng, 16)
        for cp in range(lo, hi + 1):
            result[cp] = val
    return result


def _parse_emoji_sets(path: pathlib.Path) -> tuple[set[int], set[int]]:
    ext_pict: set[int] = set()
    emoji_mod: set[int] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(";")]
        prop = parts[1]
        if prop not in ("Extended_Pictographic", "Emoji_Modifier"):
            continue
        rng = parts[0]
        if ".." in rng:
            lo_s, hi_s = rng.split("..")
            lo, hi = int(lo_s, 16), int(hi_s, 16)
        else:
            lo = hi = int(rng, 16)
        target = ext_pict if prop == "Extended_Pictographic" else emoji_mod
        target.update(range(lo, hi + 1))
    return ext_pict, emoji_mod


def _parse_emoji_vs_bases(path: pathlib.Path) -> set[int]:
    bases: set[int] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        seq = [int(tok, 16) for tok in line.split(";")[0].split()]
        if len(seq) == 2 and seq[1] == 0xFE0F:
            bases.add(seq[0])
    return bases


def build_tables(ucd_dir: pathlib.Path) -> tuple[bytes, bytes]:
    """Compile deduplicated stage-1 and stage-2 u16LE 256-codepoint tables."""
    gcb_map = _parse_codepoint_ranges(
        _ensure_verified_file(ucd_dir, "GraphemeBreakProperty.txt"),
        value_col=1,
    )
    incb_map = _parse_codepoint_ranges(
        _ensure_verified_file(ucd_dir, "DerivedCoreProperties.txt"),
        value_col=2,
        filter_col1="InCB",
    )
    eaw_map = _parse_codepoint_ranges(
        _ensure_verified_file(ucd_dir, "EastAsianWidth.txt"),
        value_col=1,
    )
    gc_map = _parse_codepoint_ranges(
        _ensure_verified_file(ucd_dir, "DerivedGeneralCategory.txt"),
        value_col=1,
    )
    ext_pict, emoji_mod = _parse_emoji_sets(
        _ensure_verified_file(ucd_dir, "emoji-data.txt"),
    )
    vs_bases = _parse_emoji_vs_bases(
        _ensure_verified_file(ucd_dir, "emoji-variation-sequences.txt"),
    )
    _ensure_verified_file(ucd_dir, "GraphemeBreakTest.txt")

    block_map: dict[bytes, int] = {}
    stage1_indices: list[int] = []
    stage2_blocks: list[bytes] = []

    for block_idx in range(0x110000 >> 8):
        base = block_idx << 8
        words: list[bytes] = []
        for offset in range(256):
            cp = base + offset
            gcb = GCB_IDS[gcb_map.get(cp, "Other")]
            incb = INCB_IDS[incb_map.get(cp, "None")]
            gc = gc_map.get(cp, "Cn")
            eaw = eaw_map.get(cp, "N")
            if (
                0xD800 <= cp <= 0xDFFF
                or cp < 0x20
                or 0x7F <= cp < 0xA0
                or gc in ("Mn", "Me", "Cf", "Mc")
                or gcb in (GCB_IDS["V"], GCB_IDS["T"])
            ):
                width = 0
            elif cp == 0x00AD or 0x1F1E6 <= cp <= 0x1F1FF:
                width = 1
            elif eaw in ("W", "F"):
                width = 2
            else:
                width = 1

            entry = (
                width
                | (gcb << 2)
                | (incb << 6)
                | ((1 if cp in ext_pict else 0) << 8)
                | ((1 if cp in emoji_mod else 0) << 9)
                | ((1 if cp in vs_bases else 0) << 10)
                | ((1 if cp in BIDI_SPOOF else 0) << 11)
            )
            words.append(entry.to_bytes(2, "little"))

        block = b"".join(words)
        idx = block_map.get(block)
        if idx is None:
            idx = len(stage2_blocks)
            block_map[block] = idx
            stage2_blocks.append(block)
        stage1_indices.append(idx)

    stage1_bytes = b"".join(idx.to_bytes(2, "little") for idx in stage1_indices)
    stage2_bytes = b"".join(stage2_blocks)
    return stage1_bytes, stage2_bytes


def _format_bmp_escape(cp: int) -> str:
    if cp == ord('"'):
        return r"\x22"
    if cp in (ord("\\"), ord("-"), ord("]"), ord("^")):
        return "\\" + chr(cp)
    if 0x20 <= cp <= 0x7E:
        return chr(cp)
    if cp <= 0xFF:
        return f"\\x{cp:02x}"
    return f"\\u{cp:04x}"


def build_safe_narrow_bmp_pattern(stage1: bytes, stage2: bytes) -> str:
    """Build character-class regex pattern for bulk-sliceable narrow BMP runs."""
    safe_cps: list[int] = []
    for cp in range(0x0020, 0x10000):
        if 0x007F <= cp <= 0x009F or 0xD800 <= cp <= 0xDFFF:
            continue
        b_idx = int.from_bytes(stage1[(cp >> 8) * 2 : (cp >> 8) * 2 + 2], "little")
        off = ((b_idx << 8) | (cp & 0xFF)) * 2
        entry = int.from_bytes(stage2[off : off + 2], "little")
        width = entry & 0b11
        gcb = (entry >> 2) & 0x0F
        incb = (entry >> 6) & 0x03
        ext_pict = bool(entry & (1 << 8))
        emoji_mod = bool(entry & (1 << 9))
        vs_base = bool(entry & (1 << 10))
        bidi_spoof = bool(entry & (1 << 11))
        if (
            width == 1
            and gcb == 0
            and incb == 0
            and not ext_pict
            and not emoji_mod
            and not bidi_spoof
            and not (cp >= 0x80 and vs_base)
        ):
            safe_cps.append(cp)

    parts: list[str] = []
    idx = 0
    total = len(safe_cps)
    while idx < total:
        start = safe_cps[idx]
        end = start
        while idx + 1 < total and safe_cps[idx + 1] == end + 1:
            idx += 1
            end = safe_cps[idx]
        if start == end:
            parts.append(_format_bmp_escape(start))
        elif end == start + 1:
            parts.append(_format_bmp_escape(start) + _format_bmp_escape(end))
        else:
            parts.append(f"{_format_bmp_escape(start)}-{_format_bmp_escape(end)}")
        idx += 1
    return "[" + "".join(parts) + "]+"


def render_module(stage1: bytes, stage2: bytes) -> str:
    """Format ``src/libvt/_ucd.py`` source text."""
    expanded_hasher = hashlib.sha256()
    for i in range(0, len(stage1), 2):
        b_idx = int.from_bytes(stage1[i : i + 2], "little")
        expanded_hasher.update(stage2[b_idx * 512 : (b_idx + 1) * 512])
    expanded_sha256 = expanded_hasher.hexdigest()

    blob = len(stage1).to_bytes(4, "little") + stage1 + stage2
    compressed = base64.b85encode(zlib.compress(blob, level=9)).decode("ascii")
    lines = [compressed[i : i + 76] for i in range(0, len(compressed), 76)]
    b85_literal = "\n".join(f'    "{ln}"' for ln in lines)
    bmp_pat = build_safe_narrow_bmp_pattern(stage1, stage2)
    pat_chunks: list[str] = []
    p_idx = 0
    while p_idx < len(bmp_pat):
        end_p = min(len(bmp_pat), p_idx + 72)
        while end_p < len(bmp_pat) and bmp_pat[end_p - 1] == "\\":
            end_p -= 1
        pat_chunks.append(bmp_pat[p_idx:end_p])
        p_idx = end_p
    bmp_pat_literal = "\n".join(f'    r"{chunk}"' for chunk in pat_chunks)

    return f'''"""Generated two-stage Unicode {UNICODE_VERSION} width and UAX #29 table.

Regenerate with ``uv run python scripts/generate_ucd.py``.

Packed 16-bit entry layout in ``_STAGE2``:
- bits ``0..1``: cell width (``0``, ``1``, or ``2``)
- bits ``2..5``: ``Grapheme_Cluster_Break`` property ID (``0..13``)
- bits ``6..7``: ``Indic_Conjunct_Break`` property ID (``0..3``)
- bit ``8``: ``Extended_Pictographic``
- bit ``9``: ``Emoji_Modifier``
- bit ``10``: ``emoji-variation-sequences.txt`` ``FE0E``/``FE0F`` base
- bit ``11``: BiDi control / spoofing codepoint
"""

from __future__ import annotations

import array
import base64
import enum
import re
import sys
import typing as t
import zlib

if t.TYPE_CHECKING:
    from libvt.config import Profile


class WidthRole(enum.IntEnum):
    """Column occupancy role of a terminal grid cell."""

    NARROW = 0
    WIDE = 1
    SPACER_TAIL = 2
    SPACER_HEAD = 3


GCB_OTHER = 0
GCB_CR = 1
GCB_LF = 2
GCB_CONTROL = 3
GCB_EXTEND = 4
GCB_ZWJ = 5
GCB_REGIONAL_INDICATOR = 6
GCB_PREPEND = 7
GCB_SPACING_MARK = 8
GCB_L = 9
GCB_V = 10
GCB_T = 11
GCB_LV = 12
GCB_LVT = 13

INCB_NONE = 0
INCB_LINKER = 1
INCB_CONSONANT = 2
INCB_EXTEND = 3

EXT_PICT_BIT = 1 << 8
EMOJI_MOD_BIT = 1 << 9
EMOJI_VS_BASE_BIT = 1 << 10
BIDI_SPOOF_BIT = 1 << 11

CLUSTER_NONE = 0
_ACTIVE_BIT = 1

UNICODE_VERSION = "{UNICODE_VERSION}"
EXPANDED_UCD_SHA256 = "{expanded_sha256}"

SAFE_NARROW_BMP_RE: t.Final[re.Pattern[str]] = re.compile(
{bmp_pat_literal}
)

_PACKED_B85 = (
{b85_literal}
)


def _unpack_tables() -> tuple[array.array[int], array.array[int]]:
    raw = zlib.decompress(base64.b85decode(_PACKED_B85))
    s1_len = int.from_bytes(raw[:4], "little")
    s1 = array.array("H")
    s1.frombytes(raw[4 : 4 + s1_len])
    s2 = array.array("H")
    s2.frombytes(raw[4 + s1_len :])
    if sys.byteorder != "little":
        s1.byteswap()
        s2.byteswap()
    return s1, s2


_STAGE1, _STAGE2 = _unpack_tables()


def ucd_lookup(cp: int) -> int:
    """Return packed 16-bit width and UAX #29 property word for codepoint *cp*."""
    if 0 <= cp <= 0x10FFFF:
        return _STAGE2[(_STAGE1[cp >> 8] << 8) | (cp & 0xFF)]
    return GCB_CONTROL << 2


def _pack_state(
    gcb: int,
    ri_odd: int,
    ep_state: int,
    incb_state: int,
    vs_base: int,
    has_spacing: int,
    multi_scalar: int,
) -> int:
    return (
        _ACTIVE_BIT
        | (gcb << 1)
        | (ri_odd << 5)
        | (ep_state << 6)
        | (incb_state << 8)
        | (vs_base << 10)
        | (has_spacing << 11)
        | (multi_scalar << 12)
    )


def init_cluster_state(cp: int, *, mode_2027: bool = True) -> int:
    """Return initial packed cluster state for base codepoint *cp*."""
    entry = ucd_lookup(cp)
    w = entry & 0b11
    gcb = (entry >> 2) & 0x0F
    incb = (entry >> 6) & 0x03
    ep = 1 if (entry & EXT_PICT_BIT) else 0
    vs_b = 1 if (entry & EMOJI_VS_BASE_BIT) else 0
    ri_odd = 1 if (mode_2027 and gcb == GCB_REGIONAL_INDICATOR) else 0
    incb_st = 1 if incb == INCB_CONSONANT else 0
    has_sp = 1 if w >= 1 else 0
    return _pack_state(gcb, ri_odd, ep, incb_st, vs_b, has_sp, 0)


def char_width(ch: str) -> int:
    """Return default terminal column width (``0``, ``1``, or ``2``) for *ch*."""
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E:
        return 1
    return ucd_lookup(cp) & 0b11


def step_cluster(
    prev_state: int,
    cp: int,
    *,
    mode_2027: bool,
    vs15_narrows: bool = True,
    vs16_always_wide: bool = False,
) -> tuple[bool, int, int]:
    """Evaluate UAX #29 continuation between *prev_state* and codepoint *cp*.

    Returns
    -------
    tuple[bool, int, int]
        ``(extends_previous, next_state, width_override)`` where
        ``width_override`` is ``-1`` (keep current cluster width), ``1``
        (narrow via ``VS15`` on an emoji variation base or first narrow glyph
        after ``Prepend``), or ``2`` (widen via ``VS16``, spacing continuation,
        emoji modifier, ZWJ pictographic, or Regional Indicator pair).
    """
    entry = ucd_lookup(cp)
    gcb = (entry >> 2) & 0x0F
    incb = (entry >> 6) & 0x03
    ep = bool(entry & EXT_PICT_BIT)
    w = entry & 0b11

    if prev_state & _ACTIVE_BIT:
        prev_g = (prev_state >> 1) & 0x0F
        ri_odd = (prev_state >> 5) & 0x01
        ep_state = (prev_state >> 6) & 0x03
        incb_state = (prev_state >> 8) & 0x03
        vs_base = (prev_state >> 10) & 0x01
        has_spacing = (prev_state >> 11) & 0x01
        multi_scalar = (prev_state >> 12) & 0x01

        if not mode_2027:
            if gcb == GCB_EXTEND:
                return (
                    True,
                    _pack_state(gcb, 0, ep_state, incb_state, 0, has_spacing, 1),
                    -1,
                )
            if w == 0:
                return False, CLUSTER_NONE, -1
            return False, init_cluster_state(cp, mode_2027=False), -1

        joined = False
        if prev_g == GCB_CR and gcb == GCB_LF:
            joined = True
        elif prev_g in (GCB_CR, GCB_LF, GCB_CONTROL) or gcb in (
            GCB_CR,
            GCB_LF,
            GCB_CONTROL,
        ):
            joined = False
        elif (
            (prev_g == GCB_L and gcb in (GCB_L, GCB_V, GCB_LV, GCB_LVT))
            or (prev_g in (GCB_LV, GCB_V) and gcb in (GCB_V, GCB_T))
            or (prev_g in (GCB_LVT, GCB_T) and gcb == GCB_T)
            or gcb in (GCB_EXTEND, GCB_ZWJ, GCB_SPACING_MARK)
            or prev_g == GCB_PREPEND
            or (incb_state == 2 and incb == INCB_CONSONANT)
            or (ep_state == 2 and ep)
            or (ri_odd == 1 and gcb == GCB_REGIONAL_INDICATOR)
        ):
            joined = True

        if joined:
            w_override = -1
            if cp == 0xFE0F and (vs_base or (vs16_always_wide and has_spacing)):
                w_override = 2
            elif cp == 0xFE0E and vs_base and vs15_narrows and not multi_scalar:
                w_override = 1
            elif not has_spacing and w >= 1:
                w_override = w
            elif has_spacing and (
                w >= 1
                or gcb == GCB_SPACING_MARK
                or ((entry & EMOJI_MOD_BIT) and ep_state == 1)
                or (ep_state == 2 and ep)
                or (ri_odd == 1 and gcb == GCB_REGIONAL_INDICATOR)
            ):
                w_override = 2

            next_ri = (ri_odd ^ 1) if gcb == GCB_REGIONAL_INDICATOR else 0
            if ep_state == 1 and gcb == GCB_EXTEND:
                next_ep = 1
            elif ep_state == 1 and gcb == GCB_ZWJ:
                next_ep = 2
            elif ep:
                next_ep = 1
            else:
                next_ep = 0

            if incb == INCB_CONSONANT:
                next_incb = 1
            elif incb_state in (1, 2) and incb == INCB_LINKER:
                next_incb = 2
            elif incb_state in (1, 2) and incb == INCB_EXTEND:
                next_incb = incb_state
            else:
                next_incb = 0

            next_vs = 1 if (entry & EMOJI_VS_BASE_BIT) else 0
            next_sp = 1 if (has_spacing or w >= 1 or gcb == GCB_SPACING_MARK) else 0
            next_multi = 0 if (not has_spacing and w >= 1) else 1
            return (
                True,
                _pack_state(
                    gcb,
                    next_ri,
                    next_ep,
                    next_incb,
                    next_vs,
                    next_sp,
                    next_multi,
                ),
                w_override,
            )

    if w == 0 and gcb in (
        GCB_CR,
        GCB_LF,
        GCB_CONTROL,
        GCB_EXTEND,
        GCB_ZWJ,
        GCB_SPACING_MARK,
        GCB_V,
        GCB_T,
    ):
        return False, CLUSTER_NONE, -1
    return False, init_cluster_state(cp, mode_2027=mode_2027), -1


def segment_graphemes(text: str) -> list[str]:
    """Segment *text* into Unicode 17.0.0 UAX #29 extended grapheme clusters."""
    if not text:
        return []
    clusters: list[str] = []
    buf: list[str] = [text[0]]
    state = init_cluster_state(ord(text[0]), mode_2027=True)
    for ch in text[1:]:
        cp = ord(ch)
        entry = ucd_lookup(cp)
        gcb = (entry >> 2) & 0x0F
        prev_g = (state >> 1) & 0x0F
        if prev_g in (GCB_CR, GCB_LF, GCB_CONTROL) and not (
            prev_g == GCB_CR and gcb == GCB_LF
        ):
            clusters.append("".join(buf))
            buf = [ch]
            state = init_cluster_state(cp, mode_2027=True)
            continue
        extends, next_state, _ = step_cluster(state, cp, mode_2027=True)
        if extends:
            buf.append(ch)
            state = next_state
        else:
            clusters.append("".join(buf))
            buf = [ch]
            state = init_cluster_state(cp, mode_2027=True)
    clusters.append("".join(buf))
    return clusters


def display_width(
    text: str,
    *,
    mode_2027: bool = True,
    profile: Profile | None = None,
) -> int:
    """Return rendered terminal column width of *text*."""
    if text.isascii():
        return sum(1 for ch in text if " " <= ch <= "~")
    vs15_narrows = profile.vs15_narrows_wide_emoji if profile is not None else True
    vs16_wide = profile.vs16_always_wide if profile is not None else False
    total = 0
    cur_w = 0
    state = CLUSTER_NONE
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E and state == CLUSTER_NONE:
            cur_w = 1
            state = init_cluster_state(cp, mode_2027=mode_2027)
            continue
        extends, state, w_override = step_cluster(
            state,
            cp,
            mode_2027=mode_2027,
            vs15_narrows=vs15_narrows,
            vs16_always_wide=vs16_wide,
        )
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
    "CLUSTER_NONE",
    "EXPANDED_UCD_SHA256",
    "SAFE_NARROW_BMP_RE",
    "UNICODE_VERSION",
    "WidthRole",
    "char_width",
    "display_width",
    "init_cluster_state",
    "sanitize_terminal_text",
    "segment_graphemes",
    "step_cluster",
    "ucd_lookup",
]
'''


def main(argv: list[str] | None = None) -> int:
    """Regenerate ``src/libvt/_ucd.py`` from SHA-256-verified Unicode 17.0.0 data."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ucd-dir",
        type=pathlib.Path,
        default=repo_root / ".cache" / "ucd" / UNICODE_VERSION,
        help="Directory holding SHA-256-verified Unicode 17.0.0 UCD text files.",
    )
    args = parser.parse_args(argv)
    stage1, stage2 = build_tables(args.ucd_dir)
    target = repo_root / "src" / "libvt" / "_ucd.py"
    target.write_text(render_module(stage1, stage2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
