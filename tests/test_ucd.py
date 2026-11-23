"""Tests for ``libvt._ucd`` Unicode 17 width, UAX #29 clustering, and BMP regex."""

from __future__ import annotations

import array
import collections.abc
import hashlib
import pathlib
import sys
import time
import typing as t
import unicodedata

import pytest

import libvt
from libvt import _ucd
from libvt._ucd import (
    EXT_PICT_BIT,
    SAFE_NARROW_BMP_RE,
    WidthRole,
    char_width,
    display_width,
    segment_graphemes,
    ucd_lookup,
)
from libvt.config import DEFAULT_PROFILE, TMUX_PROFILE, Profile
from libvt.style import AnsiColor, Hyperlink, RgbColor, UnderlineStyle

EXPECTED_EXPANDED_UCD_SHA256 = (
    "a6a2bbf94b9ebb25bb22b06a46a605b468b0a398bd9ee64e60dbc717c2484cb3"
)


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


def _expanded_stage_sha256(
    stage1: collections.abc.Sequence[int],
    stage2: array.array[int],
) -> str:
    s2 = stage2
    if sys.byteorder != "little":
        s2 = array.array("H", s2)
        s2.byteswap()
    s2_bytes = memoryview(s2).cast("B")
    hasher = hashlib.sha256()
    for block_id in stage1:
        off = block_id * 512
        hasher.update(s2_bytes[off : off + 512])
    return hasher.hexdigest()


def test_expanded_ucd_4352_block_sha256_parity() -> None:
    """All 4,352 expanded ``_STAGE1`` blocks match the pinned UCD 17.0.0 digest."""
    assert len(_ucd._STAGE1) == 4352
    digest = _expanded_stage_sha256(_ucd._STAGE1, _ucd._STAGE2)
    assert _ucd.EXPANDED_UCD_SHA256 == EXPECTED_EXPANDED_UCD_SHA256
    assert digest == EXPECTED_EXPANDED_UCD_SHA256

    mutated_stage1 = array.array("H", _ucd._STAGE1)
    mutated_stage1[0] = (
        mutated_stage1[1] if mutated_stage1[0] != mutated_stage1[1] else 1
    )
    assert (
        _expanded_stage_sha256(mutated_stage1, _ucd._STAGE2)
        != EXPECTED_EXPANDED_UCD_SHA256
    )


def test_append_grapheme_spacer_tail_inherits_base_pen_and_decsca() -> None:
    """Widening a base cell copies SGR, hyperlink, and ``DECSCA`` to ``SPACER_TAIL``."""
    term = libvt.Terminal(rows=2, cols=8)
    term.feed(
        b"\x1b[31;44;4:3;53;58:2::0:255:0m"
        b'\x1b[1"q'
        b"\x1b]8;id=h1;https://example.com\x07"
        + "❤".encode()
        + b'\x1b[0m\x1b[0"q\x1b]8;;\x07'
        + "\ufe0f".encode()
        + b"Z"
    )
    snap = term.snapshot()
    head = snap[0, 0]
    tail = snap[0, 1]
    expected_link = Hyperlink(uri="https://example.com", id="h1")

    assert (head.text, head.width, head.width_role, head.protected) == (
        "❤\ufe0f",
        2,
        WidthRole.WIDE,
        True,
    )
    assert (tail.text, tail.width, tail.width_role, tail.protected) == (
        "",
        0,
        WidthRole.SPACER_TAIL,
        True,
    )
    assert head.style.fg == AnsiColor.RED
    assert head.style.bg == AnsiColor.BLUE
    assert head.style.underline == UnderlineStyle.CURLY
    assert head.style.overline is True
    assert head.style.underline_color == RgbColor(0, 255, 0)
    assert tail.style == head.style
    assert head.hyperlink == expected_link
    assert tail.hyperlink == expected_link

    runs = snap[0].runs
    assert len(runs) == 2
    assert (runs[0].start_col, runs[0].end_col, runs[0].text) == (0, 2, "❤\ufe0f")
    assert runs[0].hyperlink == expected_link
    assert (runs[1].start_col, runs[1].end_col, runs[1].text) == (2, 3, "Z")

    term.feed(b"\x1b[?2K")
    after_decsel = term.snapshot()
    assert after_decsel[0, 0].text == "❤\ufe0f"
    assert after_decsel[0, 0].width_role == WidthRole.WIDE
    assert after_decsel[0, 1].width_role == WidthRole.SPACER_TAIL
    assert after_decsel[0, 2].text == " "

    term.reset()
    term.feed(b'\x1b[0"q' + "❤".encode() + b'\x1b[1"q' + "\ufe0f".encode())
    term.feed(b"\x1b[?2K")
    assert term.lines[0] == ""

    term.reset()
    term.feed(
        b'\x1b[31;44;7;4m\x1b[1"q\x1b]8;;https://example.com\x07' + "⌚\ufe0e".encode()
    )
    narrowed = term.snapshot()
    assert (
        narrowed[0, 0].text,
        narrowed[0, 0].width,
        narrowed[0, 0].width_role,
        narrowed[0, 0].protected,
    ) == ("⌚\ufe0e", 1, WidthRole.NARROW, True)
    vacated = narrowed[0, 1]
    assert (vacated.text, vacated.width_role, vacated.protected) == (
        " ",
        WidthRole.NARROW,
        False,
    )
    assert vacated.style.fg is None
    assert vacated.style.bg == AnsiColor.BLUE
    assert vacated.style.reverse is False
    assert vacated.style.underline == UnderlineStyle.NONE
    assert vacated.hyperlink is None
    term.feed(b'\x1b[0m\x1b[0"q\x1b]8;;\x07\x1b[?2K')
    assert term[0, 0].text == "⌚\ufe0e"

    wrap_term = libvt.Terminal(rows=2, cols=4)
    wrap_term.feed(
        b"ABC"
        b"\x1b[31;44;4:3;53;58:2::0:255:0m"
        b'\x1b[1"q'
        b"\x1b]8;id=w1;https://example.com\x07"
        b"1"
        b'\x1b[0m\x1b[0"q\x1b]8;;\x07' + "\ufe0f\u20e3".encode() + b"Z"
    )
    wrap_link = Hyperlink(uri="https://example.com", id="w1")
    wrap_snap = wrap_term.snapshot()
    assert wrap_snap[0].wrapped is True
    assert (wrap_snap[0, 3].width_role, wrap_snap[0, 3].protected) == (
        WidthRole.SPACER_HEAD,
        False,
    )
    assert (
        wrap_snap[1, 0].text,
        wrap_snap[1, 0].width_role,
        wrap_snap[1, 0].protected,
    ) == ("1\ufe0f\u20e3", WidthRole.WIDE, True)
    assert (
        wrap_snap[1, 1].text,
        wrap_snap[1, 1].width_role,
        wrap_snap[1, 1].protected,
    ) == ("", WidthRole.SPACER_TAIL, True)
    assert wrap_snap[1, 1].style == wrap_snap[1, 0].style
    assert wrap_snap[1, 0].hyperlink == wrap_link
    assert wrap_snap[1, 1].hyperlink == wrap_link

    wrap_term.feed(b"\r\n\n")
    hist_row = wrap_term.history[1]
    assert (hist_row[0].text, hist_row[0].width_role, hist_row[0].protected) == (
        "1\ufe0f\u20e3",
        WidthRole.WIDE,
        True,
    )
    assert (hist_row[1].text, hist_row[1].width_role, hist_row[1].protected) == (
        "",
        WidthRole.SPACER_TAIL,
        True,
    )
    assert hist_row[1].style == hist_row[0].style
    assert hist_row[1].hyperlink == wrap_link
    assert (hist_row.runs[0].start_col, hist_row.runs[0].end_col) == (0, 2)

    wrap_term.resize(rows=4, cols=6)
    reflow_snap = wrap_term.snapshot()
    assert reflow_snap[0].text == "ABC1\ufe0f\u20e3Z"
    assert (reflow_snap[0, 3].width_role, reflow_snap[0, 3].protected) == (
        WidthRole.WIDE,
        True,
    )
    assert (reflow_snap[0, 4].width_role, reflow_snap[0, 4].protected) == (
        WidthRole.SPACER_TAIL,
        True,
    )
    assert reflow_snap[0, 4].style == reflow_snap[0, 3].style
    assert reflow_snap[0, 4].hyperlink == wrap_link
    wrap_term.feed(b"\x1b[1;1H\x1b[?2K")
    assert wrap_term.lines[0] == "   1\ufe0f\u20e3"
