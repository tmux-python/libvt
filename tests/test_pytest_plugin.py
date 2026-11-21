"""Tests for ``libvt.pytest_plugin`` fixtures and ``libvt.testing`` assertions."""

from __future__ import annotations

import typing as t

import pytest

from libvt import Parser, ScreenAssertionError, Terminal
from libvt.testing import assert_screen, format_screen_box

if t.TYPE_CHECKING:
    import collections.abc


def test_pytest_fixtures_and_assert_screen(
    vt_terminal: Terminal,
    vt_factory: collections.abc.Callable[..., Terminal],
    vt_parser: Parser,
) -> None:
    """``vt_*`` fixtures create isolated state and ``assert_screen`` checks boxes."""
    assert vt_terminal.rows == 24
    assert vt_terminal.cols == 80
    assert len(vt_parser.feed(b"hi")) == 1

    small = vt_factory(rows=3, cols=6)
    small.feed_str("abcdefX")
    assert small[0].wrapped is True
    boxed = format_screen_box(small)
    assert "│abcdef│W" in boxed
    assert_screen(small, "abcdef\nX", cursor=(1, 1))

    with pytest.raises(ScreenAssertionError, match="┌"):
        assert_screen(small, "wrong-line")
