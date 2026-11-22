"""Tests for ``libvt.status`` (``OSC 7501`` / ``9;4``) and ``OSC 133/7/52``."""

from __future__ import annotations

import base64

import libvt
from libvt.config import Policy, Profile
from libvt.events import (
    ClipboardWrite,
    ColorSchemeQueried,
    Passthrough,
    ProgramStatusChanged,
    WorkingDirectoryChanged,
)
from libvt.status import BlockKind, StatusState


def test_osc_7501_hierarchy_urgency_spoof_rejection_and_pruning() -> None:
    """Rex ``OSC 7501`` enforces grammar, hierarchy, urgency, and lifecycle pruning."""
    pull_term = libvt.Terminal(rows=4, cols=30, record_events=False)
    pull_term.feed(b"\x1b]7501;?\x07")
    pull_reply = pull_term.drain_replies()
    assert pull_reply.startswith(b"\x1b]7501;?:")
    assert pull_reply.endswith(b"\x07")

    suppressed = libvt.Terminal(
        rows=4,
        cols=30,
        policy=Policy(allow_program_status_query=False),
    )
    suppressed.feed(b"\x1b]7501;?\x07")
    assert suppressed.drain_replies() == b""

    term = libvt.Terminal(rows=6, cols=40, record_events=True)
    term.feed(b"\x1b]7501;?\x1b\\")
    assert term.drain_replies().endswith(b"\x1b\\")

    term.feed(b"\x1b]9;4;1;35\x07")
    assert term.program_status is not None
    assert (term.program_status.state, term.program_status.progress) == (
        StatusState.WORKING,
        35,
    )

    b64_title = base64.b64encode("Compiling 日本".encode()).decode("ascii")
    term.feed(
        f"\x1b]7501;state=working:id=build:app=cargo:progress=50:title={b64_title}\x1b\\".encode(),
    )
    term.feed(b"\x1b]7501;state=blocked:id=build/sign:kind=auth\x1b\\")
    term.feed(b"\x1b]9;4;0\x07")

    child = term.program_statuses["build/sign"]
    assert (child.effective_app, child.kind) == ("cargo", BlockKind.AUTH)
    assert term.program_statuses["build"].title == "Compiling 日本"
    assert term.program_status == child

    spoof_b64 = base64.b64encode("ok\u202eevil".encode()).decode("ascii")
    term.feed(f"\x1b]7501;state=error:id=spoof:title={spoof_b64}\x1b\\".encode())
    assert "spoof" not in term.program_statuses

    term.feed(b"\x1b]7501;state=working\x1b\\")
    term.feed(b"\x1b]133;A;k=s\x07")
    assert "" in term.program_statuses
    term.feed(b"\x1b]133;A\x07")
    assert "" not in term.program_statuses

    term.feed(b"\x1b]7501;state=done:id=lint\x1b\\")
    term.notify_process_exit()
    assert "lint" in term.program_statuses
    assert "build" not in term.program_statuses

    term.feed(b"\x1b]7501;state=working:id=tree\x1b\\")
    term.feed(b"\x1b]7501;state=working:id=tree/leaf\x1b\\")
    term.feed(b"\x1b]7501;state=clear:id=tree\x1b\\")
    assert "tree" not in term.program_statuses
    assert "tree/leaf" not in term.program_statuses
    assert any(isinstance(e, ProgramStatusChanged) for e in term.drain_events())


def test_osc_133_osc_7_osc_52_queries_and_tmux_passthrough() -> None:
    """``OSC 133/7/52``, terminal queries, and ``DCS tmux;`` passthrough obey policy."""
    term = libvt.Terminal(
        rows=3,
        cols=20,
        scrollback=20,
        profile=Profile(color_scheme=libvt.ColorScheme.LIGHT),
        record_events=True,
    )
    for idx in range(4):
        term.feed(
            f"\x1b]133;A\x07$ \x1b]133;B\x07c{idx}\r\n"
            f"\x1b]133;C\x07o\r\n\x1b]133;D;0\x07".encode(),
        )
    zones = term.prompt_zones
    assert [z.abs_row for z in zones] == sorted(z.abs_row for z in zones)
    assert zones[-1].exit_code == 0

    term.feed(b"\x1b]7;file://localhost/tmp/my%20dir\x07")
    assert (term.cwd, term.cwd_uri) == ("/tmp/my dir", "file://localhost/tmp/my%20dir")
    term.feed(b"\x1b]7;file://remote-box.example/srv\x07")
    assert (term.cwd, term.cwd_uri) == ("", "file://remote-box.example/srv")
    term.feed(b"\x1b]7;file://localhost/bad%00path\x07")
    assert term.cwd == ""
    term.feed(b"\x1b]7;\x07")
    assert (term.cwd, term.cwd_uri) == ("", "")

    clip_b64 = base64.b64encode(b"copied").decode("ascii")
    term.feed(f"\x1b]52;c;{clip_b64}\x07\x1b]52;c;?\x07".encode("ascii"))
    assert term.drain_replies() == b""
    events = term.drain_events()
    assert (
        WorkingDirectoryChanged(uri="file://remote-box.example/srv", path="/srv")
        in events
    )
    assert ClipboardWrite(selection="c", text="copied") in events

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

    term.feed(b"\x1bPtmux;\x1b\x1b]2;spoofed-title\x07\x1b\\")
    assert term.title == ""
    assert term.drain_events() == (Passthrough(data=b"\x1b]2;spoofed-title\x07"),)

    allow_term = libvt.Terminal(
        rows=3,
        cols=20,
        policy=Policy(allow_passthrough=True),
        record_events=True,
    )
    allow_term.feed(b"\x1bPtmux;\x1b\x1b]2;hel\x1b\\" + b"\x1bPtmux;lo\x07\x1b\\")
    assert allow_term.title == "hello"
    allow_term.feed(
        b"\x1bPtmux;\x1b\x1bPtmux;"
        b"\x1b\x1b\x1b\x1b]7501;state=done:id=nested2\x1b\x1b\x1b\x1b\\"
        b"\x1b\x1b\\\x1b\\",
    )
    assert "nested2" in allow_term.program_statuses

    tmux_term = libvt.Terminal(
        rows=3,
        cols=20,
        profile=libvt.TMUX_PROFILE,
        policy=Policy(strict=True),
    )
    tmux_term.feed(b"\x1bkbuild-win\x1b\\OK\x1b]10;rgb:11/22/33\x1b\\\x1b]110\x1b\\")
    assert (tmux_term.text, tmux_term.window_name) == ("OK", "build-win")
