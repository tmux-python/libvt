"""Regression tests for PR #2 architectural and terminal-profile review fixes."""

from __future__ import annotations

import libvt
from libvt.config import XTERM_PROFILE
from libvt.modes import Mode


def test_resize_preserves_pending_wrap_and_wide_glyphs() -> None:
    """``resize()`` preserves ``pending_wrap``, wide cells, and profile reflow rules."""
    # Case 1a: same-size 4x2 -> 4x2 preserves pending_wrap and scroll_region.
    same = libvt.Terminal(rows=2, cols=4)
    same.feed(b"\x1b[1;2rABCD")
    assert same.cursor.pending_wrap is True
    same.resize(rows=2, cols=4)
    assert same.cursor.pending_wrap is True
    same.feed(b"E")
    assert same.lines == ("ABCD", "E")
    assert (same.cursor.row, same.cursor.col) == (1, 1)
    assert same.cursor.pending_wrap is False
    assert same[0].wrapped is True

    # Case 1b (DEFAULT_PROFILE reflow=True): widen 4x2 -> 8x2 unwraps pending_wrap.
    widened = libvt.Terminal(rows=2, cols=4)
    widened.feed(b"ABCD")
    widened.resize(rows=2, cols=8)
    assert widened.cursor.col == 4
    assert widened.cursor.pending_wrap is False
    widened.feed(b"E")
    assert widened.lines == ("ABCDE", "")
    assert (widened.cursor.row, widened.cursor.col) == (0, 5)
    assert widened[0].wrapped is False

    # Case 1b (XTERM_PROFILE reflow_on_resize=False): widen keeps col 3 + pending_wrap.
    xt = libvt.Terminal(rows=2, cols=4, profile=XTERM_PROFILE)
    xt.feed(b"ABCD")
    xt.resize(rows=2, cols=8)
    assert xt.cursor.col == 3
    assert xt.cursor.pending_wrap is True
    xt.feed(b"E")
    assert xt.lines == ("ABCD", "E")
    assert (xt.cursor.row, xt.cursor.col) == (1, 1)
    assert xt[0].wrapped is False

    # Wide glyph ending at right margin ('AB界' on 4 cols) reflows cleanly on widen.
    wide = libvt.Terminal(rows=2, cols=4)
    wide.feed("AB界".encode())
    assert wide.cursor.pending_wrap is True
    wide.resize(rows=2, cols=8)
    wide.feed(b"E")
    assert wide.lines == ("AB界E", "")
    assert (wide.cursor.row, wide.cursor.col) == (0, 5)

    # Exact-fit shrink 8x2 -> 4x2 after 'ABCD' produces no phantom blank row.
    shrink = libvt.Terminal(rows=2, cols=8, scrollback=10)
    shrink.feed(b"ABCD")
    assert (shrink.cursor.row, shrink.cursor.col) == (0, 4)
    shrink.resize(rows=2, cols=4)
    assert shrink.history == ()
    shrink.feed(b"E")
    assert shrink.lines == ("ABCD", "E")
    assert (shrink.cursor.row, shrink.cursor.col) == (1, 1)

    # Resizing while inside ?1049h keeps primary saved cursor synchronized on ?1049l.
    alt_resize = libvt.Terminal(rows=2, cols=4)
    alt_resize.feed(b"ABCDabc\x1b[?1049h")
    alt_resize.resize(rows=2, cols=8)
    alt_resize.feed(b"\x1b[?1049lX")
    assert alt_resize[0].text == "ABCDabcX"


def test_sync_2026_chunk_and_observer_invariance_and_cursor_hold() -> None:
    """``?2026`` ``committed`` freezes pre-``BSU`` screen/cursor independent of cuts."""
    payload = b"B\x1b[?2026hC"
    splits: list[list[bytes]] = [
        [payload],
        [bytes([b]) for b in payload],
        *[[payload[:i], payload[i:]] for i in range(1, len(payload))],
    ]
    for observe_before in (False, True):
        for chunks in splits:
            term = libvt.Terminal(rows=2, cols=8)
            term.feed(b"A")
            if observe_before:
                assert term.committed[0].text == "A"
            for chunk in chunks:
                term.feed(chunk)
            assert term.synchronized is True
            held = term.committed
            assert held[0].text == "AB"
            assert (held.cursor.row, held.cursor.col) == (0, 2)
            assert Mode.SYNCHRONIZED_OUTPUT not in held.modes

            live = term.snapshot()
            assert live[0].text == "ABC"
            assert (live.cursor.row, live.cursor.col) == (0, 3)
            assert Mode.SYNCHRONIZED_OUTPUT in live.modes

            term.feed(b"\x1b[?2026l")
            after = term.committed
            assert after[0].text == "ABC"
            assert (after.cursor.row, after.cursor.col) == (0, 3)

    # Scalar cursor/visibility/title hold at BSU while CPR & DECRQM report live state.
    term2 = libvt.Terminal(rows=3, cols=10)
    term2.feed(b"A")
    term2.feed(b"\x1b[?2026h\x1b[2;3H\x1b[?25l\x1b]2;Next\x07\x1b[6n\x1b[?2026$p")
    held2 = term2.committed
    assert (held2.cursor.row, held2.cursor.col) == (0, 1)
    assert held2.cursor.visible is True
    assert held2.title == ""
    assert term2.drain_replies() == b"\x1b[2;3R\x1b[?2026;1$y"

    term2.feed(b"\x1b[?2026l")
    done2 = term2.committed
    assert (done2.cursor.row, done2.cursor.col) == (1, 2)
    assert done2.cursor.visible is False
    assert done2.title == "Next"
