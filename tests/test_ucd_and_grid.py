"""Unicode 17 clustering, cell-grid invariants, RLE scrollback, and fuzz tests."""

from __future__ import annotations

import random
import time

import libvt
from libvt._ucd import WidthRole
from libvt.modes import Mode


def test_unicode_width_flags_zwj_incb_and_variation_selectors() -> None:
    """Flags, ZWJ emoji, Indic conjuncts, and VS15/VS16 obey ``?2027`` width rules."""
    term = libvt.Terminal(rows=4, cols=10)
    # Two adjacent Regional Indicator flag pairs occupy 2 + 2 = 4 columns.
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
    assert term[0, 3].text == "A"

    # VS16 widening at right margin (col == cols - 1) wraps cleanly with SPACER_HEAD.
    narrow_term = libvt.Terminal(rows=3, cols=4)
    narrow_term.feed("abc❤️".encode())
    assert narrow_term[0].text == "abc"
    assert narrow_term[0].wrapped is True
    assert narrow_term[1, 0].text == "❤️"
    assert narrow_term[1, 0].width == 2
    assert narrow_term.cursor.row == 1
    assert narrow_term.cursor.col == 2

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

    # Moving cursor before sending a combining mark must NOT mutate the earlier cell.
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

    # ICH splitting wide cell at col=1 heals col=0 and clears wide head pushed to col=5.
    term.reset()
    term.feed("日本語".encode())
    term.feed(b"\x1b[1;2H\x1b[1@")
    row0 = term.screen._rows[0]
    for col in range(6):
        role = (
            WidthRole((row0.attrs[col] >> 60) & 0b11)
            if row0.attrs is not None
            else WidthRole.NARROW
        )
        if role == WidthRole.WIDE:
            assert col + 1 < 6
            assert row0.attrs is not None
            next_role = WidthRole((row0.attrs[col + 1] >> 60) & 0b11)
            assert next_role == WidthRole.SPACER_TAIL
        if role == WidthRole.SPACER_TAIL:
            assert col > 0
            assert row0.attrs is not None
            prev_role = WidthRole((row0.attrs[col - 1] >> 60) & 0b11)
            assert prev_role == WidthRole.WIDE

    # DCH splitting wide pairs at col=1 heals col=0.
    term.reset()
    term.feed("日本語".encode())
    term.feed(b"\x1b[1;2H\x1b[2P")
    row0 = term.screen._rows[0]
    for col in range(6):
        role = (
            WidthRole((row0.attrs[col] >> 60) & 0b11)
            if row0.attrs is not None
            else WidthRole.NARROW
        )
        if role == WidthRole.WIDE:
            assert row0.attrs is not None
            assert (
                WidthRole((row0.attrs[col + 1] >> 60) & 0b11) == WidthRole.SPACER_TAIL
            )
        if role == WidthRole.SPACER_TAIL:
            assert row0.attrs is not None
            assert WidthRole((row0.attrs[col - 1] >> 60) & 0b11) == WidthRole.WIDE

    # Scroll styled + wide lines into FrozenRow RLE history and inspect runs/cells.
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
    """Assert cursor bounds and wide-cell head/tail pairing across active rows."""
    scr = term.screen
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
