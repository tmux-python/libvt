"""Tests for ``libvt.parser`` VT500 state transitions, SGR, and wire encodings."""

from __future__ import annotations

import base64
import typing as t

import pytest

import libvt
from libvt.config import Limits, Policy
from libvt.parser import (
    Action,
    ApcDispatch,
    CsiDispatch,
    DcsDispatch,
    EscDispatch,
    OscDispatch,
    Parser,
    ParserState,
    Print,
)
from libvt.style import AnsiColor, IndexedColor, RgbColor, UnderlineStyle
from libvt.terminal import StopOn


def _coalesce_prints(actions: list[Action]) -> list[Action]:
    """Merge adjacent ``Print`` actions so chunk cuts compare equal."""
    merged: list[Action] = []
    for act in actions:
        if isinstance(act, Print) and merged and isinstance(merged[-1], Print):
            merged[-1] = Print(merged[-1].text + act.text)
        else:
            merged.append(act)
    return merged


@pytest.mark.parametrize("step", [1, 3, 13], ids=["step-1", "step-3", "step-13"])
def test_chunk_cut_invariance(step: int) -> None:
    """Arbitrary byte-chunk cuts yield identical actions and terminal state."""
    payload = (
        b"\x1b[?2026h\x1b[1;4:3;38:2::40:180:160;58:5:196m"
        + "Hello 日本 🧑‍🌾 🇯🇵!".encode()
        + b"\x1b]8;id=1;https://example.com\x1b\\link\x1b]8;;\x07"
        + b"\x1b]7501;state=working:progress=42\x1b\\"
        + b"\x1bP1;2+q544e\x1b\\\x1b_Gi=1;AAAA\x1b\\\x1b[?2026l"
    )
    ref_actions = _coalesce_prints(Parser().feed(payload))
    ref_term = libvt.Terminal(rows=6, cols=40)
    ref_term.feed(payload)

    parser = Parser()
    term = libvt.Terminal(rows=6, cols=40)
    chunked_actions: list[Action] = []
    for idx in range(0, len(payload), step):
        chunk = payload[idx : idx + step]
        chunked_actions.extend(parser.feed(chunk))
        term.feed(chunk)
    assert _coalesce_prints(chunked_actions) == ref_actions
    assert term.text == ref_term.text
    assert term.cursor == ref_term.cursor


def test_sgr_colon_subparameters_and_palette_variants() -> None:
    """Colon SGR subparameters, 24-bit RGB, and indexed vs ANSI colors stay distinct."""
    term = libvt.Terminal(rows=4, cols=20)
    term.feed(
        b"\x1b[1;2;4:3;38:2::40:180:160;48;5;1;58:5:196mA\x1b[22;31;24;59mB\x1b[0mC",
    )
    cell_a = term[0, 0]
    assert (cell_a.style.bold, cell_a.style.dim) == (True, True)
    assert cell_a.style.underline == UnderlineStyle.CURLY
    assert cell_a.style.fg == RgbColor(40, 180, 160)
    assert cell_a.style.bg == IndexedColor(1)
    assert cell_a.style.bg != AnsiColor.RED
    assert cell_a.style.underline_color == IndexedColor(196)

    cell_b = term[0, 1]
    assert (cell_b.style.bold, cell_b.style.dim) == (False, False)
    assert cell_b.style.fg == AnsiColor.RED
    assert cell_b.style.underline == UnderlineStyle.NONE
    assert cell_b.style.underline_color is None
    assert term[0, 2].style == libvt.Style()


def test_utf8_c1_isolation_latin1_wire_mode_and_incomplete_utf8() -> None:
    """UTF-8 isolates ``0x80..0x9F``; ``latin-1`` parses ``C1``; C0 flushes UTF-8."""
    title = "Build “日本” Ü 😜"
    assert 0x9C in title.encode("utf-8")

    term = libvt.Terminal(rows=4, cols=30)
    term.feed(f"\x1b]2;{title}\x07ok".encode())
    assert (term.title, term.text) == (title, "ok")

    smuggled = Parser(encoding="utf-8").feed(b"\xc2\x9b31mSAFE")
    assert _coalesce_prints(smuggled) == [Print("31mSAFE")]
    assert libvt.strip(b"\xc2\x9b31mSAFE") == "31mSAFE"

    actions = Parser().feed(
        b"\x1bPq" + title.encode("utf-8") + b"\x1b\\"
        b"\x1b_" + title.encode("utf-8") + b"\x1b\\",
    )
    assert len(actions) == 2
    assert isinstance(actions[0], DcsDispatch)
    assert isinstance(actions[1], ApcDispatch)

    lat_actions = Parser(encoding="latin-1").feed(
        b"\x1b[12\x9b3;4H\x9d2;caf\xe9\x9c\x8d\xe9",
    )
    assert lat_actions == [
        CsiDispatch(0, libvt.CsiParams((3, 4), 0, 0), b"", ord("H")),
        OscDispatch(2, b"caf\xe9", b"\x9c"),
        EscDispatch(b"", ord("M")),
        Print("é"),
    ]

    lat_term = libvt.Terminal(rows=2, cols=10, encoding="latin-1")
    step = lat_term.feed_until(
        b"\x9d2;unterminated\x9b?2026hHI\x9b?2026ltrailing",
        stop=StopOn.SYNC_END,
    )
    assert (step.stopped_on, lat_term[0].text) == (StopOn.SYNC_END, "HI")

    with pytest.raises(ValueError, match="unsupported parser encoding"):
        Parser(encoding=t.cast("t.Any", "ebcdic"))

    # Incomplete UTF-8 flushes U+FFFD before an intervening C0 LF executes.
    c0_term = libvt.Terminal(rows=4, cols=10)
    c0_term.feed(b"\xe2\x82\nX")
    assert (c0_term[0].text, c0_term[1].text) == ("\ufffd", " X")


def test_string_esc_abort_dcs_del_bel_and_payload_limits() -> None:
    r"""``STRING_ESC`` aborts non-``\`` escapes, ``DCS`` ignores ``DEL``, limits cap."""
    p = Parser()
    p.feed(b"\x1b]2;pending\x1b")
    assert p.state == ParserState.STRING_ESC
    assert p.feed(b"c") == [EscDispatch(b"", ord("c"))]
    assert p.state == ParserState.GROUND

    # DCS ignores 0x7F (DEL) in header while keeping 0x07 (BEL) inside DCS tmux;.
    pt_term = libvt.Terminal(rows=2, cols=16, policy=Policy(allow_passthrough=True))
    pt_term.feed(b"\x1bPt\x7fmux;\x1b\x1b]2;ok-title\x07\x1b\\")
    assert pt_term.title == "ok-title"

    term = libvt.Terminal(
        rows=4,
        cols=20,
        limits=Limits(max_osc_bytes=16),
        record_events=True,
    )
    term.feed(b"\x1b]2;cut\x1b[32mok")
    assert (term.title, term.text) == ("", "ok")
    assert term[0, 0].style.fg == AnsiColor.GREEN

    opt_term = libvt.Terminal(rows=4, cols=20, policy=Policy(abort_dispatch_osc=True))
    opt_term.feed(b"\x1b]2;committed\x1b[31mR\x1b]2;cancelled\x18X")
    assert (opt_term.title, opt_term.text) == ("committed", "RX")

    term.reset()
    term.feed(b"\x1b]2;short\x1b\x1b\\after")
    assert (term.title, term.text) == ("", "after")

    strict_dcs = Parser(tmux_passthrough=False).feed(b"\x1bPtmux;\x1b\x1b\\hi")
    assert _coalesce_prints([a for a in strict_dcs if isinstance(a, Print)]) == [
        Print("hi"),
    ]
    assert not any(isinstance(a, DcsDispatch) for a in strict_dcs)

    term.reset()
    term.drain_events()
    term.feed(b"\x1b]2;" + (b"x" * 64) + b"\x07clean")
    assert (term.title, term.text) == ("", "clean")
    assert any(isinstance(ev, libvt.UnhandledSequence) for ev in term.drain_events())


def test_bounded_ascii_ints_and_utf8_event_byte_cost() -> None:
    """Bounded ASCII int parsers never raise on >4300 digits or malformed URIs."""
    vt = libvt.Terminal(
        rows=3,
        cols=12,
        limits=Limits(max_osc_bytes=16_384, max_event_bytes=512, max_events=4096),
        record_events=True,
    )
    vt.feed(b"\x1b]" + b"9" * 4_500 + b";x\x07")
    assert Parser().feed(b"\x1b]65536;x\x07") == [OscDispatch(-1, b"x", b"\x07")]
    vt.feed(
        "\x1b]9;4;²;²\x07\x1b]133;D;--5\x07\x1b]133;D;²\x07".encode()
        + b"\x1b]7501;state=working:progress=+50\x07"
        + b"\x1b]7501;state=working:progress=101\x07"
        + b"\x1b]7;file://[\x07\x1b]7;file://[bad]/tmp\x07"
        + "\x1b]7;file://\uff03/tmp\x07".encode()
        + b"\x1b]9;9;file://[\x07"
        + b"\x1b]10;#-f-f-f\x07\x1b]10;rgb:-fff/-fff/-fff\x07\x1b]10;?\x07"
        + b"\x1b]133;D;-42\x07"
    )
    assert [z.exit_code for z in vt.prompt_zones] == [None, None, -42]
    assert vt.program_status is None
    assert b"-" not in vt.drain_replies()

    raw_px = base64.b64encode(b"\xff" * (8 * 16 * 3))
    vt.feed(b"\x1b_Ga=t,f=24,s=8,v=16,i=4294967295;" + raw_px + b"\x1b\\")
    assert 4_294_967_295 in vt.images
    vt.drain_replies()
    vt.feed(
        b"\x1b_Ga=t,f=24,s=8,v=16,i=4294967296;"
        + raw_px
        + b"\x1b\\"
        + b"\x1b_Ga=p,i="
        + b"9" * 5_000
        + b"\x1b\\"
    )
    assert 4_294_967_296 not in vt.images
    vt.drain_replies()

    raw_2x2 = base64.b64encode(b"\xff" * 12)
    vt.feed(b"\x1b_Ga=t,f=24,s=2,v=2,i=5,q=2,m=1;" + raw_2x2[:8] + b"\x1b\\")
    vt.feed(b"\x1b_Gq=999999999999999,m=0;" + raw_2x2[8:] + b"\x1b\\")
    assert vt.drain_replies() == b""

    vt.feed(b"\x1b_Ga=t,f=24,s=2,v=2,i=6,m=1;" + raw_2x2[:8] + b"\x1b\\")
    vt.feed(b"\x1b_GI=7,m=0;" + raw_2x2[8:] + b"\x1b\\")
    assert 6 not in vt.images
    assert b"EINVAL:cannot specify both i and I" in vt.drain_replies()

    vt.feed(b"\x1b_Ga=T,f=24,s=2,v=2,i=8,m=1;" + raw_2x2[:8] + b"\x1b\\")
    vt.feed(b"\x1b_Gp=4294967296,m=0;" + raw_2x2[8:] + b"\x1b\\")
    assert vt.drain_replies() == b"\x1b_Gi=8;EINVAL:invalid uint32 p\x1b\\"

    vt.feed("\x1b_Gi=9,t=é;\x1b\\".encode())
    assert b"EINVAL:unsupported transmission medium" in vt.drain_replies()

    vt.drain_events()
    vt.feed(b"\x07\x07\x07")
    vt.feed(b"\x1b]2;" + (b"x" * 600) + b"\x07")
    assert len(vt.drain_events()) == 3

    for i in range(20):
        vt.feed_str(f"\x1b]2;{'🔥' * 30}-{i}\x07")
    evs = vt.drain_events()
    assert 1 <= len(evs) <= 3


def test_parse_sgr_ignores_colon_5_3_outside_tmux_capture() -> None:
    """Global ``parse_sgr`` and live ``Terminal`` ignore ``CSI 5:3 m``."""
    vt = libvt.Terminal(rows=2, cols=8)
    vt.feed(b"\x1b[5:3mA\x1b[53mB\x1b[55;5mC")
    assert (vt[0, 0].style.overline, vt[0, 0].style.blink) == (False, False)
    assert (vt[0, 1].style.overline, vt[0, 1].style.blink) == (True, False)
    assert (vt[0, 2].style.overline, vt[0, 2].style.blink) == (False, True)
