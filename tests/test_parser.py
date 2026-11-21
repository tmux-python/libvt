"""Streaming VT500 parser, SGR subparameter, UTF-8, and C1 isolation tests."""

from __future__ import annotations

import libvt
from libvt.config import Limits
from libvt.parser import (
    Action,
    ApcDispatch,
    CsiDispatch,
    DcsDispatch,
    OscDispatch,
    Parser,
    ParserState,
    Print,
)
from libvt.style import AnsiColor, IndexedColor, RgbColor, UnderlineStyle


def _coalesce_prints(actions: list[Action]) -> list[Action]:
    """Merge adjacent ``Print`` actions so arbitrary chunk cuts compare equal."""
    merged: list[Action] = []
    for act in actions:
        if isinstance(act, Print) and merged and isinstance(merged[-1], Print):
            merged[-1] = Print(merged[-1].text + act.text)
        else:
            merged.append(act)
    return merged


def test_chunk_cut_invariance_across_byte_steps() -> None:
    """Arbitrary chunk boundaries yield identical parsed actions and terminal state."""
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
    ref_snap = ref_term.snapshot()

    for step in (1, 2, 3, 5, 7, 13, 31):
        parser = Parser()
        term = libvt.Terminal(rows=6, cols=40)
        chunked_actions: list[Action] = []
        for idx in range(0, len(payload), step):
            chunk = payload[idx : idx + step]
            chunked_actions.extend(parser.feed(chunk))
            term.feed(chunk)
        assert _coalesce_prints(chunked_actions) == ref_actions
        assert term.snapshot().text == ref_snap.text
        assert term.cursor == ref_snap.cursor


def test_sgr_colon_subparameters_and_indexed_vs_ansi() -> None:
    """Colon SGR subparameters, 24-bit RGB, and palette color variants stay distinct."""
    term = libvt.Terminal(rows=4, cols=20)
    term.feed(
        b"\x1b[1;2;4:3;38:2::40:180:160;48;5;1;58:5:196mA\x1b[22;31;24;59mB\x1b[0mC",
    )
    cell_a = term[0, 0]
    assert cell_a.style.bold is True
    assert cell_a.style.dim is True
    assert cell_a.style.underline == UnderlineStyle.CURLY
    assert cell_a.style.fg == RgbColor(40, 180, 160)
    assert cell_a.style.bg == IndexedColor(1)
    assert cell_a.style.bg != AnsiColor.RED
    assert cell_a.style.underline_color == IndexedColor(196)

    cell_b = term[0, 1]
    assert cell_b.style.bold is False
    assert cell_b.style.dim is False
    assert cell_b.style.fg == AnsiColor.RED
    assert cell_b.style.underline == UnderlineStyle.NONE
    assert cell_b.style.underline_color is None

    cell_c = term[0, 2]
    assert cell_c.style == libvt.Style()


def test_utf8_0x9c_inside_osc_dcs_apc_does_not_break_out() -> None:
    """UTF-8 ``0x9C`` continuation bytes never terminate string control payloads."""
    title = "Build “日本” Ü 😜"
    assert 0x9C in title.encode("utf-8")

    term = libvt.Terminal(rows=4, cols=30)
    term.feed(f"\x1b]2;{title}\x07ok".encode())
    assert term.title == title
    assert term.text == "ok"

    parser = Parser()
    actions = parser.feed(
        b"\x1bPq" + title.encode("utf-8") + b"\x1b\\"
        b"\x1b_" + title.encode("utf-8") + b"\x1b\\",
    )
    assert len(actions) == 2
    assert isinstance(actions[0], DcsDispatch)
    assert actions[0].data == title.encode("utf-8")
    assert isinstance(actions[1], ApcDispatch)
    assert actions[1].data == title.encode("utf-8")

    # Explicit 8-bit C1 opt-in handles single-byte CSI (0x9B) and OSC/ST (0x9D/0x9C).
    c1_actions = Parser(c1_controls=True).feed(b"\x9b12;34H\x9d2;ascii\x9c")
    assert len(c1_actions) == 2
    assert isinstance(c1_actions[0], CsiDispatch)
    assert c1_actions[0].params.values == (12, 34)
    assert c1_actions[0].final == ord("H")
    assert c1_actions[1] == OscDispatch(2, b"ascii", b"\x9c")


def test_incomplete_utf8_flushed_before_c0_control() -> None:
    """Incomplete UTF-8 bytes emit ``U+FFFD`` before an intervening C0 control runs."""
    term = libvt.Terminal(rows=4, cols=10)
    term.feed(b"\xe2\x82\nX")
    # U+FFFD prints at (0, 0) advancing cursor to col 1; LF moves to (1, 1).
    assert term[0].text == "\ufffd"
    assert term[1].text == " X"


def test_double_esc_st_and_oversized_payload_recovery() -> None:
    """Double ``ESC`` aborts cleanly and oversized strings resynchronize."""
    term = libvt.Terminal(rows=4, cols=20, limits=Limits(max_osc_bytes=16))
    # First ESC enters OSC_ESC; second ESC cancels OSC and begins ESC \ ST no-op.
    term.feed(b"\x1b]2;short\x1b\x1b\\after")
    assert term.text == "after"

    term.reset()
    term.feed(b"\x1b]2;" + (b"x" * 64) + b"\x07clean")
    assert term.title == ""
    assert term.text == "clean"
    assert term.parser_state == ParserState.GROUND

    # Exercise SGR memo FIFO eviction past 256 unique entries.
    for idx in range(300):
        r = idx & 0xFF
        term.feed(f"\x1b[38;2;{r};10;20m.".encode("ascii"))
    assert term[0, 0].text != ""
