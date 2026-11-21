"""Tests for ``Terminal``, ``Snapshot``, ``Formatter``, and ``Effects``."""

from __future__ import annotations

from libvt import (
    AnsiColor,
    Effects,
    EraseCause,
    Formatter,
    KittyCommand,
    OutputFormat,
    ProgramStatus,
    PromptZone,
    Rect,
    SixelData,
    StopOn,
    Terminal,
)


def test_chunk_cut_invariance_across_protocols() -> None:
    """Arbitrary chunk boundaries yield identical snapshots, links, and prompts."""
    stream = (
        b"\x1b]0;build-pane\x07"
        b"\x1b]133;A\x07$ \x1b]133;B\x07make\r\n"
        b"\x1b]133;C\x07"
        b"\x1b[?2026h"
        b"\x1b]8;id=doc;https://libvt.git-pull.com\x1b\\"
        b"\x1b[1;32mDocs\x1b[0m-Link"
        b"\x1b]8;;\x1b\\"
        b" \xe6\x97\xa5\xe6\x9c\xac\r\n"
        b"\x1b]7501;compiling;75\x07"
        b"\x1b]133;D;0\x07"
        b"\x1b[?2026l"
    )
    whole = Terminal(rows=8, cols=40)
    whole.feed(stream)
    expected = whole.snapshot()

    for step in (1, 3, 7, 19):
        chunked = Terminal(rows=8, cols=40)
        for pos in range(0, len(stream), step):
            chunked.feed(stream[pos : pos + step])
        snap = chunked.snapshot()
        assert snap.text == expected.text
        assert snap.cursor == expected.cursor
        assert snap.title == "build-pane"
        assert snap.prompt_zones == expected.prompt_zones
        assert snap.program_status == ProgramStatus("compiling", 75)
        assert snap[1, 0].hyperlink == snap[1, 5].hyperlink


def test_osc8_hyperlink_survives_sgr_reset() -> None:
    """``SGR 0`` resets bold and foreground without closing an active ``OSC 8`` link."""
    term = Terminal(rows=4, cols=30)
    term.feed(
        b"\x1b]8;id=k1;https://example.com\x1b\\"
        b"\x1b[1;31mbold\x1b[0mplain"
        b"\x1b]8;;\x1b\\after",
    )
    cell0 = term[0, 0]
    assert cell0.style.bold is True
    assert cell0.style.fg == AnsiColor.RED
    assert cell0.hyperlink is not None
    assert cell0.hyperlink.uri == "https://example.com"
    assert term[0, 4].style.bold is False
    assert term[0, 4].style.fg is None
    assert term[0, 4].hyperlink == cell0.hyperlink
    assert term[0, 9].hyperlink is None


def test_synchronized_output_hold_and_vt_export() -> None:
    """``?2026`` holds ``committed`` and stays absent from static VT exports."""
    holds: list[bool] = []
    term = Terminal(
        rows=4,
        cols=20,
        effects=Effects(on_render_hold=holds.append),
    )
    term.feed(b"v1")
    assert term.committed.text == "v1"

    batch = b"\x1b[?2026h\x1b[2J\x1b[Hv2-frame\x1b[?2026lafter"
    step_start = term.feed_until(batch, stop=StopOn.SYNC_START)
    assert step_start.stopped_on == StopOn.SYNC_START
    assert term.synchronized is True
    assert term.committed.text == "v1"

    # Export mid-sync must never re-assert ?2026h.
    exported = Formatter(OutputFormat.VT).format_snapshot(term.snapshot())
    assert "?2026h" not in exported

    step_end = term.feed_until(
        batch,
        offset=step_start.offset,
        stop=StopOn.SYNC_END,
    )
    assert step_end.stopped_on == StopOn.SYNC_END
    assert term.synchronized is False
    assert term.committed.text == "v2-frame"
    assert holds == [True, False]


def test_sans_io_queries_and_readonly_effects_policy() -> None:
    """``DA1``, ``DA2``, ``DSR``, and ``DECRQM`` reply; title report respects opt-in."""
    term = Terminal(rows=10, cols=20)
    term.feed(b"\x1b]0;secret-title\x07\x1b[3;5H")
    term.feed(b"\x1b[c\x1b[>c\x1b[5n\x1b[6n\x1b[?2026$p\x1b[?9999$p\x1b[21t")
    replies = term.drain_replies()
    assert replies == (
        b"\x1b[?62;22c\x1b[>1;10;0c\x1b[0n\x1b[3;5R\x1b[?2026;2$y\x1b[?9999;0$y"
    )
    assert term.drain_replies() == b""

    opt_in = Terminal(rows=4, cols=20, effects=Effects(allow_title_report=True))
    opt_in.feed(b"\x1b]0;shown\x07\x1b[21t")
    assert opt_in.drain_replies() == b"\x1b]lshown\x1b\\"


def test_graphics_and_erase_callbacks() -> None:
    """SIXEL, Kitty graphics, OSC 133, and erase hooks fire with zero-based rects."""
    sixels: list[SixelData] = []
    kitties: list[KittyCommand] = []
    erases: list[tuple[Rect, EraseCause, bool]] = []
    zones: list[PromptZone] = []

    term = Terminal(
        rows=6,
        cols=20,
        effects=Effects(
            on_sixel=sixels.append,
            on_kitty_graphics=kitties.append,
            on_erase=lambda r, c, alt: erases.append((r, c, alt)),
            on_osc_133=lambda _k, z: zones.append(z),
        ),
    )
    term.feed(
        b'\x1b[2;4H\x1bPq"1;1;8;8#0~\x1b\\'
        b"\x1b_Ga=T,i=7;QUJD\x1b\\"
        b"\x1b]133;D;2\x07"
        b"\x1b[2K",
    )
    assert len(sixels) == 1
    assert (sixels[0].row, sixels[0].col) == (1, 3)
    assert len(kitties) == 1
    assert kitties[0].control == {"a": "T", "i": "7"}
    assert kitties[0].payload == b"QUJD"
    assert zones == [PromptZone(kind="D", row=1, col=3, exit_code=2)]
    assert (Rect(1, 0, 1, 20), EraseCause.EL, False) in erases
