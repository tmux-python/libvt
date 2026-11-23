"""Tests for ``libvt.testing`` assertions and ``libvt.pytest_plugin`` fixtures."""

from __future__ import annotations

import pytest

import libvt
from libvt.pytest_plugin import TerminalFactory
from libvt.terminal import FlightRecorder, Terminal
from libvt.testing import assert_screen, assert_screen_box


def test_pytest_fixtures_and_assert_screen_literal_borders(
    vt_terminal: Terminal,
    vt_factory: TerminalFactory,
    vt_parser: libvt.Parser,
    vt_recorder: FlightRecorder,
) -> None:
    """``vt_*`` fixtures wire recorders; ``assert_screen`` keeps ACS borders."""
    assert (vt_terminal.rows, vt_terminal.cols) == (24, 80)
    assert vt_terminal.recorder is vt_recorder
    assert isinstance(vt_parser, libvt.Parser)

    bordered = vt_factory(rows=3, cols=5)
    bordered.feed(b"\x1b(0lqqqk\r\nx X x\r\nmqqqj\x1b(B")
    assert bordered.text == "┌───┐\n│ X │\n└───┘"
    assert_screen(bordered, bordered.text)
    assert_screen(
        bordered,
        """
        ┌───┐
        │ X │
        └───┘
        """,
    )


def test_assert_screen_box_wide_soft_wrap_and_bidi_diagnostics(
    vt_factory: TerminalFactory,
    vt_recorder: FlightRecorder,
) -> None:
    """``assert_screen_box`` validates widths, ``W`` markers, and BiDi escapes."""
    small = vt_factory(rows=2, cols=8)
    small.feed("日本ok\r\nline2  W".encode())
    assert_screen_box(
        small,
        """
        ┌────────┐
        │日本ok  │
        │line2  W│
        └────────┘
        """,
    )

    wrapped = vt_factory(rows=2, cols=4)
    wrapped.feed(b"abcdef")
    assert_screen_box(
        wrapped,
        """
        ┌────┐
        │abcd│W
        │ef  │
        └────┘
        """,
        cursor=(1, 2),
    )

    with pytest.raises(ValueError, match="display width"):
        assert_screen_box(small, "┌────┐\n│short│\n└────┘")

    spoof_term = vt_factory(rows=1, cols=10, recorder=vt_recorder)
    spoof_term.feed(b"\x1b[31mhi")
    spoof_term.screen._rows[0].write_narrow(2, "\u202e", 0, None)
    with pytest.raises(AssertionError) as exc_info:
        assert_screen_box(spoof_term, "┌──────────┐\n│mismatch  │\n└──────────┘")
    msg = str(exc_info.value)
    assert "\\u202e" in msg
    assert "Flight Recorder:" in msg
