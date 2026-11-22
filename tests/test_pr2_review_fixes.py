"""Regression tests for PR #2 architectural and terminal-profile review fixes."""

from __future__ import annotations

import libvt
from libvt.config import XTERM_PROFILE


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
