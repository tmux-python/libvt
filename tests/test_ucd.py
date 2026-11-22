"""Tests for ``libvt._ucd`` Unicode 17 width, UAX #29 clustering, and BMP regex."""

from __future__ import annotations

import collections.abc
import pathlib
import time
import typing as t
import unicodedata

import pytest

import libvt
from libvt import _ucd
from libvt._ucd import (
    EXT_PICT_BIT,
    SAFE_NARROW_BMP_RE,
    char_width,
    display_width,
    segment_graphemes,
    ucd_lookup,
)
from libvt.config import DEFAULT_PROFILE, TMUX_PROFILE, Profile


class DisplayWidthCase(t.NamedTuple):
    """Typed ``display_width`` scenario under DEC ``?2027``."""

    test_id: str
    text: str
    profile: Profile
    expected_width: int


DISPLAY_WIDTH_CASES: list[DisplayWidthCase] = [
    DisplayWidthCase("ascii-ignores-vs16", "a\ufe0f", DEFAULT_PROFILE, 1),
    DisplayWidthCase("cjk-ignores-vs15", "日\ufe0e", DEFAULT_PROFILE, 2),
    DisplayWidthCase("heart-vs16-widens", "❤\ufe0f", DEFAULT_PROFILE, 2),
    DisplayWidthCase("keycap-hash-vs16", "#\ufe0f\u20e3", DEFAULT_PROFILE, 2),
    DisplayWidthCase("watch-vs15-narrows", "⌚\ufe0e", DEFAULT_PROFILE, 1),
    DisplayWidthCase("watch-vs15-then-vs16", "⌚\ufe0e\ufe0f", DEFAULT_PROFILE, 1),
    DisplayWidthCase("watch-mark-blocks-vs15", "⌚\u0301\ufe0e", DEFAULT_PROFILE, 2),
    DisplayWidthCase("watch-vs15-tmux-stays-wide", "⌚\ufe0e", TMUX_PROFILE, 2),
    DisplayWidthCase("devanagari-conjunct", "क्ष", DEFAULT_PROFILE, 2),
    DisplayWidthCase("bengali-split-vowel", "\u0995\u09cb", DEFAULT_PROFILE, 2),
    DisplayWidthCase("arabic-prepend-digit", "\u06001", DEFAULT_PROFILE, 1),
    DisplayWidthCase("hangul-jamo-lvt", "\u1100\u1161\u11a8", DEFAULT_PROFILE, 2),
]


@pytest.mark.parametrize(
    "case",
    DISPLAY_WIDTH_CASES,
    ids=[c.test_id for c in DISPLAY_WIDTH_CASES],
)
def test_display_width_cases(case: DisplayWidthCase) -> None:
    """``display_width`` obeys ``?2027`` UAX #29 and variation-selector rules."""
    assert display_width(case.text, profile=case.profile) == case.expected_width


def test_unicode_17_grapheme_break_vectors() -> None:
    """All 766 official Unicode 17.0.0 ``GraphemeBreakTest.txt`` vectors pass."""
    fixture = (
        pathlib.Path(__file__).resolve().parent
        / "fixtures"
        / "GraphemeBreakTest-17.0.0.txt"
    )
    count = 0
    for raw in fixture.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        expected = [
            "".join(chr(int(tok, 16)) for tok in chunk.strip().split("\u00d7"))
            for chunk in line.split("\u00f7")
            if chunk.strip()
        ]
        assert segment_graphemes("".join(expected)) == expected, raw
        count += 1
    assert count == 766


def test_unicode_17_stdlib_stage_table_parity() -> None:
    """Every ``_STAGE2`` block and ``_STAGE1`` slot matches ``unicodedata`` 17.0."""
    if unicodedata.unidata_version != "17.0.0" or not hasattr(
        unicodedata,
        "grapheme_cluster_break",
    ):
        return

    gcb_names = (
        "Other",
        "CR",
        "LF",
        "Control",
        "Extend",
        "ZWJ",
        "Regional_Indicator",
        "Prepend",
        "SpacingMark",
        "L",
        "V",
        "T",
        "LV",
        "LVT",
    )
    incb_names = ("None", "Linker", "Consonant", "Extend")
    gcb_fn = unicodedata.grapheme_cluster_break
    incb_fn = unicodedata.indic_conjunct_break
    ep_fn = unicodedata.extended_pictographic

    seen_blocks: set[int] = set()
    for hi, block_id in enumerate(_ucd._STAGE1):
        offsets: collections.abc.Iterable[int]
        if block_id not in seen_blocks:
            seen_blocks.add(block_id)
            offsets = range(256)
        else:
            offsets = (0x00, 0x7F, 0x80, 0xFF)
        base = hi << 8
        for lo in offsets:
            cp = base | lo
            if 0xD800 <= cp <= 0xDFFF:
                continue
            ch = chr(cp)
            word = ucd_lookup(cp)
            assert gcb_names[(word >> 2) & 0x0F] == gcb_fn(ch), hex(cp)
            assert incb_names[(word >> 6) & 0x03] == incb_fn(ch), hex(cp)
            assert bool(word & EXT_PICT_BIT) == ep_fn(ch), hex(cp)


def test_safe_narrow_bmp_regex_and_streaming_clusters() -> None:
    """``SAFE_NARROW_BMP_RE`` covers narrow BMP runs and matches scalar feeding."""
    assert (char_width("\u09cb"), char_width("\u1161"), char_width("\u0600")) == (
        0,
        0,
        0,
    )
    bmp_all = "".join(chr(cp) for cp in range(0x10000) if not (0xD800 <= cp <= 0xDFFF))
    matched_count = sum(len(m.group(0)) for m in SAFE_NARROW_BMP_RE.finditer(bmp_all))
    assert matched_count >= 18_000
    for sample in ("┌", "─", "│", "█", "Ω", "д", "→", "⠿"):
        assert SAFE_NARROW_BMP_RE.match(sample) is not None
    for unsafe in (
        "\u0301",
        "\ufe0f",
        "\u200d",
        "中",
        "\u202e",
        "\x00",
        "\x7f",
        "\x85",
    ):
        assert SAFE_NARROW_BMP_RE.match(unsafe) is None

    mixed = "┌── Ωμέγα 🚀 Кириллица ──┐#1\ufe0f\u20e3*2\ufe0f🇯🇵🧑‍🌾क्ष"
    bulk = libvt.Terminal(rows=4, cols=16)
    bulk.feed_str(mixed)
    scalar = libvt.Terminal(rows=4, cols=16)
    for b in mixed.encode("utf-8"):
        scalar.feed(bytes([b]))
    assert (bulk.lines, bulk.cursor) == (scalar.lines, scalar.cursor)

    # Disabling ?2027 leaves narrow base + VS16 at width 1.
    legacy = libvt.Terminal(rows=2, cols=6)
    legacy.feed(b"\x1b[?2027l" + "❤\ufe0fX".encode())
    assert (legacy[0, 0].width, legacy[0, 1].text) == (1, "X")


def test_combining_mark_cap_and_cluster_anchor_invalidation() -> None:
    """Combining floods stay ``O(1)`` capped and cursor moves clear cluster anchors."""
    term = libvt.Terminal(rows=2, cols=10)
    t0 = time.perf_counter()
    term.feed(("a" + ("\u0301" * 1_000)).encode("utf-8"))
    assert time.perf_counter() - t0 < 0.1
    assert len(term[0, 0].text) == term.limits.max_grapheme_codepoints

    term.reset()
    term.feed(b"ab\x1b[1;1H" + "\u0301".encode())
    assert (term[0, 0].text, term[0, 1].text) == ("a", "b")
