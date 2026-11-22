"""Regression tests for PR #2 architectural and terminal-profile review fixes."""

from __future__ import annotations

import asyncio
import pickle

import pytest

import libvt
from libvt.aio import AsyncTerminalStream
from libvt.config import DEFAULT_PROFILE, TMUX_PROFILE, XTERM_PROFILE, Policy
from libvt.events import ScreenErased
from libvt.exc import TerminalTimeoutError
from libvt.modes import Mode
from libvt.parser import ParserState
from libvt.style import AnsiColor
from libvt.terminal import OutputFormat


def test_resize_preserves_pending_wrap_and_wide_glyphs() -> None:
    """``resize()`` preserves ``pending_wrap``, wide cells, and profile reflow rules."""
    # Case 1a: same-size 4x2 -> 4x2 preserves pending_wrap and scroll_region.
    same = libvt.Terminal(rows=2, cols=4)
    same.feed(b"\x1b[1;2rABCD")
    assert same.cursor.pending_wrap is True
    same.resize(rows=2, cols=4)
    assert same.cursor.pending_wrap is True
    same.feed(b"E")
    assert same.lines == ("ABCD", "E")
    assert (same.cursor.row, same.cursor.col) == (1, 1)
    assert same.cursor.pending_wrap is False
    assert same[0].wrapped is True

    # Case 1b (DEFAULT_PROFILE reflow=True): widen 4x2 -> 8x2 unwraps pending_wrap.
    widened = libvt.Terminal(rows=2, cols=4)
    widened.feed(b"ABCD")
    widened.resize(rows=2, cols=8)
    assert widened.cursor.col == 4
    assert widened.cursor.pending_wrap is False
    widened.feed(b"E")
    assert widened.lines == ("ABCDE", "")
    assert (widened.cursor.row, widened.cursor.col) == (0, 5)
    assert widened[0].wrapped is False

    # Case 1b (XTERM_PROFILE reflow_on_resize=False): widen keeps col 3 + pending_wrap.
    xt = libvt.Terminal(rows=2, cols=4, profile=XTERM_PROFILE)
    xt.feed(b"ABCD")
    xt.resize(rows=2, cols=8)
    assert xt.cursor.col == 3
    assert xt.cursor.pending_wrap is True
    xt.feed(b"E")
    assert xt.lines == ("ABCD", "E")
    assert (xt.cursor.row, xt.cursor.col) == (1, 1)
    assert xt[0].wrapped is False

    # Wide glyph ending at right margin ('AB界' on 4 cols) reflows cleanly on widen.
    wide = libvt.Terminal(rows=2, cols=4)
    wide.feed("AB界".encode())
    assert wide.cursor.pending_wrap is True
    wide.resize(rows=2, cols=8)
    wide.feed(b"E")
    assert wide.lines == ("AB界E", "")
    assert (wide.cursor.row, wide.cursor.col) == (0, 5)

    # Exact-fit shrink 8x2 -> 4x2 after 'ABCD' produces no phantom blank row.
    shrink = libvt.Terminal(rows=2, cols=8, scrollback=10)
    shrink.feed(b"ABCD")
    assert (shrink.cursor.row, shrink.cursor.col) == (0, 4)
    shrink.resize(rows=2, cols=4)
    assert shrink.history == ()
    shrink.feed(b"E")
    assert shrink.lines == ("ABCD", "E")
    assert (shrink.cursor.row, shrink.cursor.col) == (1, 1)

    # Resizing while inside ?1049h keeps primary saved cursor synchronized on ?1049l.
    alt_resize = libvt.Terminal(rows=2, cols=4)
    alt_resize.feed(b"ABCDabc\x1b[?1049h")
    alt_resize.resize(rows=2, cols=8)
    alt_resize.feed(b"\x1b[?1049lX")
    assert alt_resize[0].text == "ABCDabcX"


def test_sync_2026_chunk_and_observer_invariance_and_cursor_hold() -> None:
    """``?2026`` ``committed`` freezes pre-``BSU`` screen/cursor independent of cuts."""
    payload = b"B\x1b[?2026hC"
    splits: list[list[bytes]] = [
        [payload],
        [bytes([b]) for b in payload],
        *[[payload[:i], payload[i:]] for i in range(1, len(payload))],
    ]
    for observe_before in (False, True):
        for chunks in splits:
            term = libvt.Terminal(rows=2, cols=8)
            term.feed(b"A")
            if observe_before:
                assert term.committed[0].text == "A"
            for chunk in chunks:
                term.feed(chunk)
            assert term.synchronized is True
            held = term.committed
            assert held[0].text == "AB"
            assert (held.cursor.row, held.cursor.col) == (0, 2)
            assert Mode.SYNCHRONIZED_OUTPUT not in held.modes

            live = term.snapshot()
            assert live[0].text == "ABC"
            assert (live.cursor.row, live.cursor.col) == (0, 3)
            assert Mode.SYNCHRONIZED_OUTPUT in live.modes

            term.feed(b"\x1b[?2026l")
            after = term.committed
            assert after[0].text == "ABC"
            assert (after.cursor.row, after.cursor.col) == (0, 3)

    # Scalar cursor/visibility/title hold at BSU while CPR & DECRQM report live state.
    term2 = libvt.Terminal(rows=3, cols=10)
    term2.feed(b"A")
    term2.feed(b"\x1b[?2026h\x1b[2;3H\x1b[?25l\x1b]2;Next\x07\x1b[6n\x1b[?2026$p")
    held2 = term2.committed
    assert (held2.cursor.row, held2.cursor.col) == (0, 1)
    assert held2.cursor.visible is True
    assert held2.title == ""
    assert term2.drain_replies() == b"\x1b[2;3R\x1b[?2026;1$y"

    term2.feed(b"\x1b[?2026l")
    done2 = term2.committed
    assert (done2.cursor.row, done2.cursor.col) == (1, 2)
    assert done2.cursor.visible is False
    assert done2.title == "Next"


def test_ed3_preserves_viewport_and_pending_wrap_and_profile_alt() -> None:
    """``CSI 3 J`` clears history without wiping viewport cells or ``pending_wrap``."""
    term = libvt.Terminal(rows=2, cols=4, scrollback=10, record_events=True)
    term.feed(b"old\r\nnew\r\nABCD")
    assert [r.text for r in term.history] == ["old"]
    assert term.lines == ("new", "ABCD")
    assert term.cursor.pending_wrap is True
    term.drain_events()

    term.feed(b"\x1b[3J")
    assert term.history == ()
    assert term.lines == ("new", "ABCD")
    assert term.cursor.pending_wrap is True
    assert [e for e in term.drain_events() if isinstance(e, ScreenErased)] == []

    # DECSED 3 and out-of-range ED 99 are complete no-ops.
    term.feed(b"\x1b[?3J\x1b[99J")
    assert term.lines == ("new", "ABCD")
    assert term.cursor.pending_wrap is True

    term.feed(b"E")
    assert term.lines == ("ABCD", "E")
    assert [r.text for r in term.history] == ["new"]

    # Alternate-screen ED 3 respects profile.ed3_clears_scrollback_on_alt.
    def_term = libvt.Terminal(rows=2, cols=6, scrollback=10, profile=DEFAULT_PROFILE)
    def_term.feed(b"hist1\r\nhist2\r\nvis\x1b[?1049hALT\x1b[3J")
    assert def_term[0].text == "ALT"
    assert [r.text for r in def_term.history] == ["hist1"]

    for prof in (XTERM_PROFILE, TMUX_PROFILE):
        p_term = libvt.Terminal(rows=2, cols=6, scrollback=10, profile=prof)
        p_term.feed(b"hist1\r\nhist2\r\nvis\x1b[?1049hALT\x1b[3J")
        assert p_term[0].text == "ALT"
        assert p_term.history == ()


def test_hpr_vpr_margins_decom_and_tmux_ignore() -> None:
    """``HPR`` and ``VPR`` honor ``DECOM`` margins and ``TMUX_PROFILE`` ignore."""
    term = libvt.Terminal(rows=4, cols=8)
    term.feed(b"A\x1b[2aB\x1b[1eC")
    assert term[0].text == "A  B"
    assert term[1].text == "    C"

    # HPR clears pending_wrap.
    wrap_t = libvt.Terminal(rows=2, cols=4)
    wrap_t.feed(b"ABCD\x1b[1aE")
    assert wrap_t.lines == ("ABCE", "")

    # DECOM off (?6l): HPR/VPR cross DECLRMM and DECSTBM margins to screen edges.
    margin_t = libvt.Terminal(rows=4, cols=8)
    margin_t.feed(b"\x1b[?69h\x1b[2;3r\x1b[3;6s\x1b[2;3H\x1b[10a\x1b[10e")
    assert (margin_t.cursor.row, margin_t.cursor.col) == (3, 7)

    # DECOM on (?6h): HPR/VPR clamp to active margins without double-adding origin.
    margin_t.feed(b"\x1b[?6h\x1b[1a\x1b[1e")
    assert (margin_t.cursor.row, margin_t.cursor.col) == (2, 3)
    margin_t.feed(b"\x1b[99a\x1b[99e")
    assert (margin_t.cursor.row, margin_t.cursor.col) == (2, 5)

    # TMUX_PROFILE ignores HPR/VPR quietly even under Policy(strict=True).
    tm = libvt.Terminal(
        rows=2, cols=8, profile=TMUX_PROFILE, policy=Policy(strict=True)
    )
    tm.feed(b"A\x1b[3aB\x1b[2eC")
    assert tm[0].text == "ABC"

    spans = libvt.explain(b"\x1b[3a\x1b[2e")
    assert [s.mnemonic for s in spans] == ["HPR", "VPR"]


def test_decslrm_scroll_clear_range_rep_decaln_and_vs16_clamp() -> None:
    """``DECSLRM`` scroll, ``ECH``, ``REP``, ``DECALN``, and ``?7l`` ``VS16`` hold."""
    # DECSLRM scroll_up updates text_used bidirectionally.
    scr_t = libvt.Terminal(rows=2, cols=6)
    scr_t.feed(b"AB\x1b[?69h\x1b[1;2s\x1b[1;1H\x1b[S")
    assert scr_t[0].text == ""

    scr_t2 = libvt.Terminal(rows=2, cols=6)
    scr_t2.feed(b"\r\nCD\x1b[?69h\x1b[1;2s\x1b[1;1H\x1b[S")
    assert scr_t2[0].text == "CD"

    scr_t3 = libvt.Terminal(rows=2, cols=6)
    scr_t3.feed(b"AB\x1b[?69h\x1b[1;2s\x1b[1;1H\x1b[T")
    assert scr_t3[1].text == "AB"

    # Partial ECH trims text_used when erasing to the end of written text.
    ech_t = libvt.Terminal(rows=2, cols=10)
    ech_t.feed(b"ABCD\x1b[3G\x1b[2X")
    assert ech_t[0].text == "AB"

    # _Row.freeze() does not inflate FrozenRow.used to full screen width.
    fr_t = libvt.Terminal(rows=2, cols=80, scrollback=5)
    fr_t.feed(b"\x1b[31mA\x1b[0m\r\nB\r\nC")
    assert fr_t._screens.primary._history[0].used == 1

    # Non-print sequences clear REP _last_printed state.
    rep_t = libvt.Terminal(rows=2, cols=10)
    rep_t.feed(b"A\x1b[C\x1b[3b")
    assert rep_t[0].text == "A"
    rep_t.reset()
    rep_t.feed(b"A\r\x1b[3b")
    assert rep_t[0].text == "A"

    # DECALN resets margins and wrapped flags before filling 'E'.
    aln_t = libvt.Terminal(rows=2, cols=4)
    aln_t.feed(b"ABCDEF\x1b[?69h\x1b[2;3s\x1b#8")
    assert aln_t.lines == ("EEEE", "EEEE")
    assert aln_t[0].wrapped is False
    assert aln_t.screen.scroll_region == libvt.ScrollRegion(0, 1, 0, 3)

    # ?7l right-margin VS16 keeps display_width <= cols without 2-col overflow.
    vs_t = libvt.Terminal(rows=2, cols=4)
    vs_t.feed("\x1b[?7lABC\u26a0\ufe0f".encode())
    assert libvt.display_width(vs_t[0].text) <= 4
    assert vs_t[0, 3].width == 1


async def test_formatter_vt_bce_roundtrip_html_palette_and_timeout_diagnostics() -> (
    None
):
    """``Formatter`` preserves BCE/HTML styles and timeout diagnostics."""
    # VT formatter preserves both right-trimmed text AND trailing BCE background!
    bce_t = libvt.Terminal(rows=2, cols=10)
    bce_t.feed(b"\x1b[42mHI\x1b[K")
    snap = bce_t.snapshot()
    assert snap[0].text == "HI"
    assert snap[0, 9].style.bg == AnsiColor.GREEN

    vt_str = snap.format(OutputFormat.VT)
    rt = libvt.Terminal(rows=2, cols=10)
    rt.feed_str(vt_str)
    assert rt[0].text == "HI"
    assert rt[0, 9].style.bg == AnsiColor.GREEN

    # HTML formatter renders ANSI/indexed colors, reverse video, and wavy lines.
    html_t = libvt.Terminal(rows=2, cols=16)
    html_t.feed(b"\x1b[31;48;5;208;4:3;9;53mWARN\x1b[0;7mREV\x1b[0m")
    rendered_html = html_t.snapshot().format(OutputFormat.HTML)
    assert "color:rgb(" in rendered_html
    assert "background-color:rgb(255,135,0)" in rendered_html
    assert "text-decoration-style:wavy" in rendered_html
    assert "underline" in rendered_html and "overline" in rendered_html

    # TerminalTimeoutError preserves single-arg OSError str/errno and pickles cleanly.
    manual = TerminalTimeoutError(
        "timed out",
        parser_state=ParserState.OSC_STRING,
        screen_text="hello",
        recent_bytes=b"\x1b]0;stuck",
        eof=False,
    )
    assert isinstance(manual, TimeoutError)
    assert manual.errno is None
    assert str(manual) == "timed out"
    unpickled = pickle.loads(pickle.dumps(manual))
    assert str(unpickled) == "timed out"
    assert unpickled.parser_state == ParserState.OSC_STRING
    assert unpickled.recent_bytes == b"\x1b]0;stuck"

    # AsyncTerminalStream attaches FlightRecorder and populates timeout fields.
    reader = asyncio.StreamReader()
    stream = AsyncTerminalStream(reader, rows=2, cols=10)
    reader.feed_data(b"hello\x1b]0;stuck")
    with pytest.raises(TerminalTimeoutError) as exc_info:
        await stream.wait_for("missing", timeout=0.05)
    err = exc_info.value
    assert err.parser_state == ParserState.OSC_STRING
    assert b"\x1b]0;stuck" in err.recent_bytes
    assert "Flight Recorder:" in str(err)
