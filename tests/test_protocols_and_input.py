"""Rex OSC 7501, OSC 133/7/52, queries, tmux passthrough, and input encoding tests."""

from __future__ import annotations

import base64

import pytest

import libvt
from libvt.config import Policy, Profile
from libvt.events import (
    ClipboardWrite,
    ColorSchemeQueried,
    ProgramStatusChanged,
    WorkingDirectoryChanged,
)
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
from libvt.status import BlockKind, StatusState


def test_osc_7501_rex_grammar_hierarchy_urgency_and_queries() -> None:
    """Rex ``OSC 7501`` enforces grammar, hierarchy, urgency, and pruning."""
    silent_term = libvt.Terminal(rows=4, cols=30)
    # Query is silent by default when neither policy nor event consumer is attached.
    silent_term.feed(b"\x1b]7501;?\x07")
    assert silent_term.drain_replies() == b""

    # Attaching an event consumer enables capability replies mirroring BEL vs ST.
    term = libvt.Terminal(rows=6, cols=40, record_events=True)
    term.feed(b"\x1b]7501;?\x07")
    assert term.drain_replies().endswith(b"\x07")
    term.feed(b"\x1b]7501;?\x1b\\")
    reply_st = term.drain_replies()
    assert reply_st.startswith(b"\x1b]7501;?:")
    assert reply_st.endswith(b"\x1b\\")

    # OSC 9;4 fallback populates root status until native 7501 arrives.
    term.feed(b"\x1b]9;4;1;35\x07")
    assert term.program_status is not None
    assert term.program_status.state == StatusState.WORKING
    assert term.program_status.progress == 35

    # Native 7501 hierarchical tasks with base64 title/msg and ancestor app inheritance.
    b64_title = base64.b64encode("Compiling 日本".encode()).decode("ascii")
    term.feed(
        f"\x1b]7501;state=working:id=build:app=cargo:progress=50:title={b64_title}\x1b\\".encode(),
    )
    term.feed(b"\x1b]7501;state=blocked:id=build/sign:kind=auth\x1b\\")
    # ConEmu 9;4 is ignored once native 7501 has been observed.
    term.feed(b"\x1b]9;4;0\x07")

    child = term.program_statuses["build/sign"]
    assert child.effective_app == "cargo"
    assert child.kind == BlockKind.AUTH
    assert term.program_statuses["build"].title == "Compiling 日本"
    # Priority: blocked outranks working!
    assert term.program_status == child

    # Base64 title containing BiDi or C0 spoofing is rejected.
    spoof_b64 = base64.b64encode("ok\u202eevil".encode()).decode("ascii")
    term.feed(f"\x1b]7501;state=error:id=spoof:title={spoof_b64}\x1b\\".encode())
    assert "spoof" not in term.program_statuses

    # Secondary prompt OSC 133;A;k=s keeps active status; primary OSC 133;A clears root.
    term.feed(b"\x1b]7501;state=working\x1b\\")
    assert "" in term.program_statuses
    term.feed(b"\x1b]133;A;k=s\x07")
    assert "" in term.program_statuses
    term.feed(b"\x1b]133;A\x07")
    assert "" not in term.program_statuses

    # Process exit prunes working/blocked tasks while preserving terminal done/error.
    term.feed(b"\x1b]7501;state=done:id=lint\x1b\\")
    term.notify_process_exit()
    assert "lint" in term.program_statuses
    assert "build" not in term.program_statuses
    assert "build/sign" not in term.program_statuses

    term.feed(b"\x1b]7501;state=working:id=tree\x1b\\")
    term.feed(b"\x1b]7501;state=working:id=tree/leaf\x1b\\")
    term.feed(b"\x1b]7501;state=clear:id=tree\x1b\\")
    assert "tree" not in term.program_statuses
    assert "tree/leaf" not in term.program_statuses
    assert any(isinstance(e, ProgramStatusChanged) for e in term.drain_events())


def test_osc_133_abs_row_osc_7_osc_52_and_tmux_passthrough() -> None:
    """``OSC 133`` ``abs_row``, ``OSC 7``/``52``, queries, and ``DCS tmux;`` unwrap."""
    term = libvt.Terminal(
        rows=3,
        cols=20,
        scrollback=20,
        profile=Profile(color_scheme=libvt.ColorScheme.LIGHT),
        record_events=True,
    )
    for idx in range(5):
        seq = (
            f"\x1b]133;A\x07$ \x1b]133;B\x07cmd{idx}\r\n"
            f"\x1b]133;C\x07out\r\n\x1b]133;D;0\x07"
        )
        term.feed(seq.encode())
    zones = term.prompt_zones
    assert len(zones) >= 4
    assert [z.abs_row for z in zones] == sorted(z.abs_row for z in zones)
    assert zones[-1].exit_code == 0

    # OSC 7 file URI decoding (local host accepted, remote host ignored by default).
    term.feed(b"\x1b]7;file://localhost/tmp/my%20dir\x07")
    assert term.cwd == "/tmp/my dir"
    term.feed(b"\x1b]7;file://remote-box.example/srv\x07")
    assert term.cwd == "/tmp/my dir"

    # OSC 52 write emits ClipboardWrite; '?' query is silent unless enabled.
    clip_b64 = base64.b64encode(b"copied").decode("ascii")
    term.feed(f"\x1b]52;c;{clip_b64}\x07\x1b]52;c;?\x07".encode("ascii"))
    assert term.drain_replies() == b""
    events = term.drain_events()
    assert any(isinstance(e, WorkingDirectoryChanged) for e in events)
    assert ClipboardWrite(selection="c", text="copied") in events

    # XTVERSION, DECRQSS, XTGETTCAP, OSC 10/11, ?2031 / ?996n, and DCS tmux; unwrap.
    term.feed(
        b"\x1b[>q\x1bP$qm\x1b\\\x1bP+q544e\x1b\\\x1b]10;?\x07\x1b[?2031h\x1b[?996n",
    )
    replies = term.drain_replies()
    assert b"\x1bP>|libvt" in replies
    assert b"\x1bP1$r" in replies
    assert b"\x1bP1+r544e=" in replies
    assert b"\x1b]10;rgb:" in replies
    assert b"\x1b[?997;2n" in replies
    assert ColorSchemeQueried(scheme=libvt.ColorScheme.LIGHT) in term.drain_events()

    # Unwrap 1-level (ST and BEL) and 2-level nested DCS tmux; passthrough.
    term.feed(b"\x1bPtmux;\x1b\x1b]2;bel-title\x07\x1b\\")
    assert term.title == "bel-title"
    term.feed(b"\x1bPtmux;\x1b\x1b]7501;state=done:id=inner\x1b\x1b\\\x1b\\")
    assert "inner" in term.program_statuses
    term.feed(
        b"\x1bPtmux;\x1b\x1bPtmux;"
        b"\x1b\x1b\x1b\x1b]7501;state=done:id=nested2\x1b\x1b\x1b\x1b\\"
        b"\x1b\x1b\\\x1b\\",
    )
    assert "nested2" in term.program_statuses


def test_kitty_keyboard_stack_and_input_encoding() -> None:
    """``KittyKeyboardStack`` isolates ``CSI u`` from ``SCORC`` and encodes input."""
    term = libvt.Terminal(rows=4, cols=20)
    # Save cursor at (1, 3), push Kitty DISAMBIGUATE, then restore cursor with CSI u.
    term.feed(b"\x1b[2;4H\x1b[s\x1b[>1u\x1b[1;1H\x1b[u\x1b[?u")
    assert term.cursor.row == 1
    assert term.cursor.col == 3
    assert term.kitty_keyboard_flags == KittyKeyboardFlags.DISAMBIGUATE
    assert term.drain_replies() == b"\x1b[?1u"

    # Kitty CSI u key encoding vs DECCKM legacy encoding.
    assert term.encode(KeyEvent.parse("escape")) == b"\x1b[27u"
    assert term.encode(KeyEvent.parse("ctrl+a")) == b"\x1b[97;5u"

    # Popping returns to legacy mode; DECCKM switches Up arrow from CSI A to SS3 A.
    term.feed(b"\x1b[<1u\x1b[?1h")
    assert term.encode(KeyEvent.parse("up")) == b"\x1bOA"

    # Mouse SGR (?1006 + ?1000) and Focus (?1004).
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


def test_paste_event_fail_closed_and_character_sanitize() -> None:
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

    # Classic .replace("\x1b[201~", "") smuggling vector fails closed!
    with pytest.raises(UnsafePasteError):
        term.encode(PasteEvent("safe\x1b[20\x1b[201~1~;evil\n"))

    # Character-level sanitize strips ESC cleanly so no ESC [ 201 ~ can ever reassemble.
    sanitized = term.encode(PasteEvent("hi\x1b[20\x1b[201~1~", sanitize=True))
    assert sanitized == b"\x1b[200~hi[20[201~1~\x1b[201~"

    # Policy allow_clipboard_read remains default False.
    assert Policy().allow_clipboard_read is False
