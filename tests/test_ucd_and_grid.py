"""Unicode 17 clustering, cell-grid invariants, RLE scrollback, and fuzz tests."""

from __future__ import annotations

import pathlib
import random
import time
import unicodedata

import libvt
from libvt._ucd import (
    EXT_PICT_BIT,
    WidthRole,
    char_width,
    display_width,
    segment_graphemes,
    ucd_lookup,
)
from libvt.config import TMUX_PROFILE, XTERM_PROFILE
from libvt.modes import Mode
from libvt.terminal import Formatter, OutputFormat


def test_unicode_17_grapheme_break_test_vectors() -> None:
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
        expected: list[str] = []
        for chunk in line.split("\u00f7"):
            chunk = chunk.strip()
            if not chunk:
                continue
            expected.append(
                "".join(chr(int(tok, 16)) for tok in chunk.split("\u00d7")),
            )
        text = "".join(expected)
        assert segment_graphemes(text) == expected, raw
        count += 1
    assert count == 766


def test_unicode_17_stdlib_parity_when_available() -> None:
    """Verify 0 property mismatches against Python 3.15+ Unicode 17 ``unicodedata``."""
    if unicodedata.unidata_version != "17.0.0" or not hasattr(
        unicodedata, "grapheme_cluster_break"
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

    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        word = ucd_lookup(cp)
        assert gcb_names[(word >> 2) & 0x0F] == gcb_fn(ch), hex(cp)
        assert incb_names[(word >> 6) & 0x03] == incb_fn(ch), hex(cp)
        assert bool(word & EXT_PICT_BIT) == ep_fn(ch), hex(cp)


def test_unicode_width_flags_zwj_incb_and_variation_selectors() -> None:
    """Flags, ZWJ emoji, Indic conjuncts, and VS15/VS16 obey ``?2027`` width rules."""
    # Non-emoji bases (ASCII 'a', CJK '日') ignore VS16/VS15 width overrides!
    assert display_width("a\ufe0f") == 1
    assert display_width("日\ufe0e") == 2
    assert display_width("❤\ufe0f") == 2
    assert display_width("#\ufe0f\u20e3") == 2
    assert display_width("⌚\ufe0e") == 1
    assert display_width("⌚\ufe0e\ufe0f") == 1
    assert display_width("⌚\u0301\ufe0e") == 2
    assert display_width("⌚\ufe0e", profile=TMUX_PROFILE) == 2
    assert display_width("क्ष") == 2
    assert display_width("स्त्र") == 2
    assert display_width("\u0995\u09cb") == 2
    assert char_width("\u09cb") == 0
    assert char_width("\u1161") == 0
    assert char_width("\u0600") == 0
    assert display_width("\u06001") == 1
    assert display_width("\u1100\u1161\u11a8") == 2

    term = libvt.Terminal(rows=4, cols=10)
    term.feed("🇯🇵🇺🇸!".encode())
    assert term.cursor.col == 5
    assert term[0, 0].text == "🇯🇵"
    assert term[0, 0].width == 2
    assert term[0, 2].text == "🇺🇸"
    assert term[0, 2].width == 2
    assert term[0, 4].text == "!"

    # Streaming 1-byte chunks of ZWJ emoji and Indic conjuncts.
    term.reset()
    for b in "🧑‍🌾क्षA".encode():
        term.feed(bytes([b]))
    assert term[0, 0].text == "🧑‍🌾"
    assert term[0, 0].width == 2
    assert term[0, 2].text == "क्ष"
    assert term[0, 2].width == 2
    assert term[0, 4].text == "A"

    # Prepend + narrow ASCII stays 1 column on Terminal as well as display_width.
    term.reset()
    term.feed_str("\u06001!")
    assert term[0, 0].text == "\u06001"
    assert term[0, 0].width == 1
    assert term[0, 1].text == "!"

    # TMUX_PROFILE preserves width 2 on VS15 (matching live tmux 3.8a).
    tmux_term = libvt.Terminal(rows=2, cols=10, profile=TMUX_PROFILE)
    tmux_term.feed_str("⌚\ufe0e!")
    assert tmux_term[0, 0].width == 2
    assert tmux_term.cursor.col == 3
    assert XTERM_PROFILE.name == "xterm"

    # VS16 widening at right margin (col == cols - 1) wraps cleanly with SPACER_HEAD.
    narrow_term = libvt.Terminal(rows=3, cols=4)
    narrow_term.feed("abc❤️".encode())
    assert narrow_term[0].text == "abc"
    assert narrow_term[0].wrapped is True
    assert narrow_term[0, 3].width_role == WidthRole.SPACER_HEAD
    assert narrow_term[1, 0].text == "❤️"
    assert narrow_term[1, 0].width == 2
    assert narrow_term.cursor.row == 1
    assert narrow_term.cursor.col == 2

    # Overwriting (1, 0) with narrow 'Z' heals (0, 3) SPACER_HEAD and clears wrapped!
    narrow_term.feed(b"\x1b[2;1HZ")
    assert narrow_term[0, 3].width_role == WidthRole.NARROW
    assert narrow_term[0, 3].text == " "
    assert narrow_term[0].wrapped is False
    assert narrow_term[1, 0].text == "Z"
    assert narrow_term[1, 1].text == " "
    assert narrow_term[1, 1].width_role == WidthRole.NARROW
    vt_replayed = libvt.render(
        Formatter(OutputFormat.VT).format_snapshot(narrow_term.snapshot()),
        rows=3,
        cols=8,
    )
    narrow_term.resize(rows=3, cols=8)
    assert narrow_term.text == "abc\nZ"
    assert vt_replayed == narrow_term.text
    narrow_term.resize(rows=3, cols=4)
    assert narrow_term.screen._rows[0].used == 3

    # Wide-to-wide CUP overwrite at (1, 0) also repairs pre-edit SPACER_HEAD!
    w2w_term = libvt.Terminal(rows=3, cols=4)
    w2w_term.feed_str("abc❤️")
    w2w_term.feed_str("\x1b[2;1H日")
    assert w2w_term[0, 3].width_role == WidthRole.NARROW
    assert w2w_term[0].wrapped is False
    assert w2w_term[1, 0].text == "日"

    # BCE EL 0 with colored background keeps background style without padding .text!
    bce_term = libvt.Terminal(rows=3, cols=80)
    bce_term.feed(b"\x1b[44mfoo\x1b[K\r\nbar")
    assert bce_term[0].text == "foo"
    assert bce_term[1].text == "bar"
    assert bce_term[0, 10].style.bg is not None
    bce_term.resize(rows=3, cols=40)
    assert bce_term[0].text == "foo"
    assert bce_term[1].text == "bar"

    # RowView stays anchored to viewport row coordinates across scroll recycling!
    scroll_term = libvt.Terminal(rows=2, cols=10, scrollback=5)
    scroll_term.feed(b"row-0\r\nrow-1")
    top_view = scroll_term[0]
    assert top_view.text == "row-0"
    scroll_term.feed(b"\r\nrow-2")
    assert top_view.text == "row-1"

    # Disabling ?2027 leaves narrow base + VS16 at width 1.
    legacy = libvt.Terminal(rows=2, cols=6)
    legacy.feed(b"\x1b[?2027l" + "❤\ufe0fX".encode())
    assert legacy[0, 0].width == 1
    assert legacy[0, 1].text == "X"


def test_combining_mark_cap_and_cluster_anchor_invalidation() -> None:
    """Combining floods stay O(1) capped and cursor moves clear cluster anchors."""
    term = libvt.Terminal(rows=4, cols=10)
    t0 = time.perf_counter()
    term.feed(("a" + ("\u0301" * 5_000)).encode("utf-8"))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25
    assert len(term[0, 0].text) == term.limits.max_grapheme_codepoints

    term.reset()
    term.feed(b"ab")
    term.feed(b"\x1b[1;1H" + "\u0301".encode())
    assert term[0, 0].text == "a"
    assert term[0, 1].text == "b"


def test_row_clear_range_ich_dch_wide_healing_and_frozen_rle() -> None:
    """Explicit spaces survive ``EL 0``, ``ICH``/``DCH`` heal pairs, and RLE works."""
    term = libvt.Terminal(rows=3, cols=6, scrollback=20)
    term.feed(b"   \x1b[K")
    assert term.screen._rows[0].used == 3

    # DCH clears pending_wrap and clears wrapped when shifting to right edge.
    term.reset()
    term.feed(b"12345678")
    assert term[0].wrapped is True
    term.feed(b"\x1b[1;6H!\x1b[P")
    assert term.cursor.pending_wrap is False
    assert term[0].wrapped is False
    assert term[0].text == "12345"

    term.reset()
    term.feed("日本語".encode())
    term.feed(b"\x1b[1;2H\x1b[1@")
    _assert_grid_invariants(term)

    term.reset()
    term.feed("日本語".encode())
    term.feed(b"\x1b[1;2H\x1b[2P")
    _assert_grid_invariants(term)

    term.reset()
    for idx in range(8):
        term.feed(f"\x1b[38;2;10;20;{idx * 20}m日{idx}\r\n".encode())
    assert len(term.history) >= 5
    hist_row = term.history[0]
    assert hist_row.text == "日0"
    assert hist_row[0].text == "日"
    assert hist_row[0].width == 2
    assert len(hist_row.runs) >= 1


def _assert_grid_invariants(term: libvt.Terminal) -> None:
    """Assert cursor bounds and wide-cell head/tail/spacer_head invariants."""
    scr = term.screen
    declrmm = term.modes.is_set(Mode.DECLRMM)
    assert 0 <= scr.cursor.row < scr.rows
    assert 0 <= scr.cursor.col < scr.cols
    for r_idx, raw_row in enumerate(scr._rows):
        assert len(raw_row.chars) == scr.cols
        assert 0 <= raw_row.used <= scr.cols
        if raw_row.attrs is None:
            continue
        for c_idx in range(scr.cols):
            role = WidthRole((raw_row.attrs[c_idx] >> 60) & 0b11)
            if role == WidthRole.WIDE:
                assert c_idx + 1 < scr.cols, f"orphan WIDE at ({r_idx}, {c_idx})"
                tail_role = WidthRole((raw_row.attrs[c_idx + 1] >> 60) & 0b11)
                assert tail_role == WidthRole.SPACER_TAIL
            elif role == WidthRole.SPACER_TAIL:
                assert c_idx > 0, f"orphan SPACER_TAIL at ({r_idx}, 0)"
                head_role = WidthRole((raw_row.attrs[c_idx - 1] >> 60) & 0b11)
                assert head_role == WidthRole.WIDE
            elif (
                role == WidthRole.SPACER_HEAD
                and not declrmm
                and c_idx == scr.cols - 1
                and r_idx + 1 < scr.rows
                and raw_row.wrapped
            ):
                next_row = scr._rows[r_idx + 1]
                next_role = (
                    WidthRole((next_row.attrs[0] >> 60) & 0b11)
                    if next_row.attrs is not None
                    else WidthRole.NARROW
                )
                assert next_role == WidthRole.WIDE, (
                    f"stale SPACER_HEAD at ({r_idx}, {c_idx}) before {next_role}"
                )


def test_randomized_invariants_400_trials() -> None:
    """400-trial seeded fuzzing preserves grid, wide-spacer, and cursor invariants."""
    rng = random.Random(20261121)
    tokens: tuple[bytes, ...] = (
        b"abc",
        "日本".encode(),
        "🧑‍🌾".encode(),
        "🇯🇵".encode(),
        "❤\ufe0f".encode(),
        "a\u0301".encode(),
        b"\r",
        b"\n",
        b"\x08",
        b"\t",
        b"\x1b[A",
        b"\x1b[B",
        b"\x1b[C",
        b"\x1b[D",
        b"\x1b[Z",
        b"\x1b[I",
        b"\x1b[2@",
        b"\x1b[2P",
        b"\x1b[2X",
        b"\x1b[K",
        b"\x1b[1K",
        b"\x1b[2K",
        b"\x1b[J",
        b"\x1b[L",
        b"\x1b[M",
        b"\x1b[2;5r",
        b"\x1b[r",
        b"\x1b[?69h\x1b[2;7s",
        b"\x1b[?69l",
        b"\x1b(0lqqqk\x1b(B",
        b"\x1b[38;2;12;34;56;4:3m",
        b"\x1b[0m",
        b"\x1b[?1049h",
        b"\x1b[?1049l",
        b"\x1b[?2026h",
        b"\x1b[?2026l",
    )

    for trial in range(400):
        rows = rng.choice((4, 6, 8))
        cols = rng.choice((6, 8, 12))
        term = libvt.Terminal(rows=rows, cols=cols, scrollback=50)
        stream = b"".join(rng.choice(tokens) for _ in range(32))
        idx = 0
        while idx < len(stream):
            step = rng.randint(1, 9)
            term.feed(stream[idx : idx + step])
            idx += step
            _assert_grid_invariants(term)
        if trial % 5 == 0:
            term.resize(rows=rng.choice((3, 6, 9)), cols=rng.choice((5, 8, 14)))
            _assert_grid_invariants(term)
        if term.modes.is_set(Mode.ALT_SCREEN):
            term.feed(b"\x1b[?1049l")
        snap = term.snapshot()
        assert snap.rows == term.rows
