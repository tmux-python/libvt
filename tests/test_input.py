"""Tests for ``libvt.input`` mode-aware key, mouse, focus, and paste encoding."""

from __future__ import annotations

import pytest

import libvt
from libvt.config import Policy
from libvt.exc import UnsafePasteError
from libvt.input import (
    FocusEvent,
    KeyEvent,
    KeyMod,
    KittyKeyboardFlags,
    MouseAction,
    MouseButton,
    MouseEvent,
    PasteEvent,
)


def test_kitty_keyboard_stack_mouse_sgr_and_focus_encoding() -> None:
    """``KittyKeyboardStack`` isolates ``CSI u`` from ``SCORC`` and encodes input."""
    term = libvt.Terminal(rows=4, cols=20)
    term.feed(b"\x1b[2;4H\x1b[s\x1b[>1u\x1b[1;1H\x1b[u\x1b[?u")
    assert (term.cursor.row, term.cursor.col) == (1, 3)
    assert term.kitty_keyboard_flags == KittyKeyboardFlags.DISAMBIGUATE
    assert term.drain_replies() == b"\x1b[?1u"

    assert term.encode(KeyEvent.parse("escape")) == b"\x1b[27u"
    assert term.encode(KeyEvent.parse("ctrl+a")) == b"\x1b[97;5u"

    term.feed(b"\x1b[<1u\x1b[?1h")
    assert term.encode(KeyEvent.parse("up")) == b"\x1bOA"

    term.feed(b"\x1b[?1000;1006;1004h")
    click = MouseEvent(
        row=2,
        col=4,
        button=MouseButton.LEFT,
        action=MouseAction.PRESS,
        modifiers=KeyMod.SHIFT,
    )
    assert term.encode(click) == b"\x1b[<4;5;3M"
    assert term.encode(FocusEvent(focused=True)) == b"\x1b[I"


def test_paste_event_fail_closed_and_sanitizer() -> None:
    r"""``PasteEvent`` blocks nested ``\x1b[201~`` bypasses and unbracketed newlines."""
    term = libvt.Terminal(rows=4, cols=20)
    with pytest.raises(UnsafePasteError):
        term.encode(PasteEvent("ls\nrm -rf /"))
    assert (
        term.encode(PasteEvent("line1\nline2", allow_unbracketed_newlines=True))
        == b"line1\nline2"
    )

    term.feed(b"\x1b[?2004h")
    assert term.encode(PasteEvent("hello\nworld")) == b"\x1b[200~hello\nworld\x1b[201~"

    with pytest.raises(UnsafePasteError):
        term.encode(PasteEvent("safe\x1b[20\x1b[201~1~;evil\n"))

    sanitized = term.encode(PasteEvent("hi\x1b[20\x1b[201~1~", sanitize=True))
    assert sanitized == b"\x1b[200~hi[20[201~1~\x1b[201~"
    assert Policy().allow_clipboard_read is False
    assert Policy().allow_passthrough is False
