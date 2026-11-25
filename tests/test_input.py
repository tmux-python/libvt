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


def test_xtmodkeys_alternate_scroll_1007_and_sgr_pixel_1016() -> None:
    """``XTMODKEYS``, ``?1007`` wheel-to-arrow, and ``?1016`` pixel clamping."""
    term = libvt.Terminal(rows=4, cols=20)
    term.feed(b"\x1b[?4m\x1b[>4;2m\x1b[?4m")
    assert term.modify_other_keys == 2
    assert term.drain_replies() == b"\x1b[>4;0m\x1b[>4;2m"

    assert term.encode(KeyEvent.parse("a")) == b"a"
    assert term.encode(KeyEvent.parse("A")) == b"A"
    assert term.encode(KeyEvent.parse("shift+a")) == b"\x1b[27;2;65~"
    assert term.encode(KeyEvent.parse("alt+a")) == b"\x1b[27;3;97~"
    assert term.encode(KeyEvent.parse("alt+shift+a")) == b"\x1b[27;4;65~"
    assert term.encode(KeyEvent.parse("ctrl+a")) == b"\x1b[27;5;97~"
    assert term.encode(KeyEvent.parse("ctrl+shift+a")) == b"\x1b[27;6;65~"
    assert term.encode(KeyEvent.parse("ctrl+;")) == b"\x1b[27;5;59~"
    assert term.encode(KeyEvent.parse("shift+ ")) == b"\x1b[27;2;32~"
    assert term.encode(KeyEvent.parse("shift+tab")) == b"\x1b[27;2;9~"
    assert term.encode(KeyEvent.parse("shift+enter")) == b"\x1b[27;2;13~"
    assert term.encode(KeyEvent.parse("ctrl+enter")) == b"\x1b[27;5;13~"
    assert term.encode(KeyEvent.parse("ctrl+é")) == b"\x1b[27;5;233~"
    assert term.encode(KeyEvent.parse("alt+é")) == b"\x1b[27;3;233~"
    assert term.encode(KeyEvent.parse("ctrl+\u0130")) == b"\x1b[27;5;304~"
    assert term.encode(KeyEvent.parse("ctrl+\u212a")) == b"\x1b[27;5;8490~"

    # Kitty keyboard protocol takes precedence over modifyOtherKeys while active.
    term.feed(b"\x1b[>1u")
    assert term.encode(KeyEvent.parse("ctrl+a")) == b"\x1b[97;5u"
    assert term.encode(KeyEvent.parse("shift+a")) == b"A"
    term.feed(b"\x1b[<1u")

    # Level 1 keeps C0/ESC for Ctrl+a / Alt+a / Shift+Tab while encoding ambiguous keys.
    term.feed(b"\x1b[>4;1m")
    assert term.modify_other_keys == 1
    assert term.encode(KeyEvent.parse("shift+a")) == b"A"
    assert term.encode(KeyEvent.parse("alt+a")) == b"\x1ba"
    assert term.encode(KeyEvent.parse("alt+shift+a")) == b"\x1bA"
    assert term.encode(KeyEvent.parse("ctrl+a")) == b"\x01"
    assert term.encode(KeyEvent.parse("shift+tab")) == b"\x1b[Z"
    assert term.encode(KeyEvent.parse("ctrl+;")) == b"\x1b[27;5;59~"
    assert term.encode(KeyEvent.parse("shift+enter")) == b"\x1b[27;2;13~"
    assert term.encode(KeyEvent.parse("ctrl+é")) == b"\x1b[27;5;233~"
    assert term.encode(KeyEvent.parse("alt+é")) == b"\x1b" + "é".encode()
    assert term.encode(KeyEvent.parse("ctrl+\u0130")) == b"\x1b[27;5;304~"
    assert term.encode(KeyEvent.parse("ctrl+\u212a")) == b"\x1b[27;5;8490~"

    # CSI > 4 m, CSI > m, CSI > 4 n, DECSTR, and RIS reset modify_other_keys to 0.
    term.feed(b"\x1b[>4m")
    assert term.modify_other_keys == 0
    assert term.encode(KeyEvent.parse("shift+a")) == b"A"
    assert term.encode(KeyEvent.parse("alt+shift+a")) == b"\x1bA"
    assert term.encode(KeyEvent.parse("ctrl+\u0130")) == "\u0130".encode()
    assert term.encode(KeyEvent.parse("ctrl+\u212a")) == "\u212a".encode()
    term.feed(b"\x1b[>4;2m\x1b[>m")
    assert term.modify_other_keys == 0
    term.feed(b"\x1b[>4;2m\x1bc")
    assert term.modify_other_keys == 0
    term.feed(b"\x1b[>4;2m\x1b[>4n\x1b[?4m\x1b[>4;2m\x1b[!p\x1b[?4m")
    assert term.modify_other_keys == 0
    assert term.drain_replies() == b"\x1b[>4;0m\x1b[>4;0m"

    # ?1007 alternate scroll: default enabled on alt screen when mouse tracking is off.
    wheel_up = MouseEvent(
        button=MouseButton.WHEEL_UP,
        action=MouseAction.PRESS,
        row=1,
        col=2,
    )
    wheel_dn = MouseEvent(
        button=MouseButton.WHEEL_DOWN,
        action=MouseAction.PRESS,
        row=1,
        col=2,
    )
    assert term.encode(wheel_up) == b""
    term.feed(b"\x1b[?1007$p\x1b[?1049h")
    assert term.drain_replies() == b"\x1b[?1007;1$y"
    assert term.encode(wheel_up) == b"\x1b[A"
    assert term.encode(wheel_dn) == b"\x1b[B"
    term.feed(b"\x1b[?1h")
    assert term.encode(wheel_up) == b"\x1bOA"
    term.feed(b"\x1b[?1007l")
    assert term.encode(wheel_up) == b""
    term.feed(b"\x1b[?1007h\x1b[?1l\x1b[?1049l")

    # ?1016 SGR-Pixel mouse: derives default cell-center pixels & clamps negatives.
    term.feed(b"\x1b[?1000;1016h")
    auto_px = MouseEvent(
        button=MouseButton.LEFT,
        action=MouseAction.PRESS,
        row=2,
        col=3,
    )
    assert term.encode(auto_px) == b"\x1b[<0;28;40M"
    neg_px = MouseEvent(
        button=MouseButton.LEFT,
        action=MouseAction.PRESS,
        row=0,
        col=0,
        pixel_x=-5,
        pixel_y=-12,
    )
    assert term.encode(neg_px) == b"\x1b[<0;0;0M"
    exp_rel = MouseEvent(
        button=MouseButton.RIGHT,
        action=MouseAction.RELEASE,
        row=0,
        col=0,
        pixel_x=10,
        pixel_y=20,
    )
    assert term.encode(exp_rel) == b"\x1b[<2;10;20m"
    partial_x = MouseEvent(
        button=MouseButton.LEFT,
        action=MouseAction.PRESS,
        row=2,
        col=3,
        pixel_x=10,
    )
    partial_y = MouseEvent(
        button=MouseButton.LEFT,
        action=MouseAction.PRESS,
        row=2,
        col=3,
        pixel_y=20,
    )
    assert term.encode(partial_x) == b"\x1b[<0;10;40M"
    assert term.encode(partial_y) == b"\x1b[<0;28;20M"

    # Standalone encode_input(..., active_is_alt=None) infers ?1049 from ModeState.
    alt_modes = libvt.ModeState()
    alt_modes.set_mode(libvt.Mode.ALT_SCREEN, enabled=True)
    assert libvt.encode_input(wheel_up, modes=alt_modes) == b"\x1b[A"

    # Per-screen XTMODKEYS isolation across ?1049h and zero-pixel ?1016 gating.
    strict_t = libvt.Terminal(
        rows=4,
        cols=10,
        profile=libvt.Profile(cell_width_px=0, cell_height_px=0),
        policy=Policy(strict=True),
    )
    strict_t.feed(b"\x1b[>1;2m\x1b[>4;2m\x1b[?1049h")
    assert strict_t.modify_other_keys == 0
    strict_t.feed(b"\x1b[>2n\x1b[?1049l")
    assert strict_t.modify_other_keys == 2
    strict_t.feed(b"\x1b[?1016$p\x1b[?1000;1006;1016h")
    assert strict_t.drain_replies() == b"\x1b[?1016;0$y"
    assert strict_t.encode(auto_px) == b"\x1b[<0;4;3M"
