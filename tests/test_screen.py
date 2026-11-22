"""Unit tests for packed ``_Row`` storage, wide cells, and 2D screen slicing."""

from __future__ import annotations

from libvt.screen import BLANK_CELL, Screen, WidthRole
from libvt.terminal import Terminal


def test_blank_cell_singleton_and_2d_slicing() -> None:
    """Unwritten cells return ``BLANK_CELL`` and support 1D/2D/negative slices."""
    term = Terminal(rows=5, cols=12)
    assert term[0, 0] is BLANK_CELL
    term.feed_str("top-left\r\n  [popup] \r\n\x1b[5;1Hstatus-bar")
    assert term[0].text == "top-left"
    assert term[-1].text == "status-bar"
    region = term[1:2, 2:9]
    assert region.rows == 1
    assert region.cols == 7
    assert region.text == "[popup]"
    assert region[0, 0].text == "["


def test_wide_character_spacer_healing_on_overwrite() -> None:
    """Overwriting either half of a 2-column glyph clears its partner cell."""
    term = Terminal(rows=2, cols=8)
    term.feed_str("日本")
    assert term[0, 0].text == "日"
    assert term[0, 0].width_role == WidthRole.WIDE
    assert term[0, 1].text == ""
    assert term[0, 1].width_role == WidthRole.SPACER_TAIL

    # Overwrite the spacer tail (col 1) with ASCII 'X'; col 0 must heal to ' '.
    term.feed(b"\x1b[1;2HX")
    assert term[0, 0].text == " "
    assert term[0, 0].width_role == WidthRole.NARROW
    assert term[0, 1].text == "X"
    assert term[0, 2].text == "本"

    # Overwrite wide head at col 2 with ASCII 'Y'; col 3 spacer must heal to ' '.
    term.feed(b"\x1b[1;3HY")
    assert term[0, 2].text == "Y"
    assert term[0, 3].text == " "
    assert term[0, 3].width_role == WidthRole.NARROW


def test_vs16_grapheme_widening_and_combining_marks() -> None:
    """Combining marks attach to the prior cell and ``VS16`` widens under ``?2027``."""
    term = Terminal(rows=2, cols=10)
    term.feed_str("e\u0301 \u2764\ufe0f!")
    assert term[0, 0].text == "e\u0301"
    assert term[0, 2].text == "\u2764\ufe0f"
    assert term[0, 2].width_role == WidthRole.WIDE
    assert term[0, 3].width_role == WidthRole.SPACER_TAIL
    assert term[0, 4].text == "!"


def test_declrmm_rectangular_scroll_and_primary_history() -> None:
    """Primary scroll pushes history while ``?69`` narrows vertical scroll columns."""
    scr = Screen(rows=3, cols=6, scrollback=10)
    term = Terminal(rows=3, cols=6, scrollback=10)
    term.feed_str("line1\r\nline2\r\nline3\r\nline4")
    assert term.screens.primary.history_lines == ("line1",)
    assert term.lines == ("line2", "line3", "line4")
    assert scr.rows == 3

    # Enable DECLRMM (?69) and set left/right margins to cols 2..5 (1-based).
    box = Terminal(rows=3, cols=6)
    box.feed_str("ABCDEF\r\n123456\r\nuvwxyz")
    box.feed(b"\x1b[?69h\x1b[2;5s\x1b[1;2H\x1b[1S")
    assert box.lines == ("A2345F", "1vwxy6", "u    z")
