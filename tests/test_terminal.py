"""Terminal emulation, charsets, modes, reflow, sync stepping, and format tests."""

from __future__ import annotations

import pytest

import libvt
from libvt.config import TMUX_PROFILE, XTERM_PROFILE, Policy
from libvt.events import InBandResize, RenderHoldChanged
from libvt.exc import ProtocolError
from libvt.terminal import OutputFormat, StopOn


def test_dec_special_graphics_g0_g3_so_si_ss2_ss3_and_decsc() -> None:
    """``G0..G3`` DEC Special Graphics translate box drawing and ``DECSC``/``DECRC``."""
    term = libvt.Terminal(rows=4, cols=16)
    # DECSC at col 5 saves G0='0'; DECRC restores col 5 and G0='0' ('x' -> '│').
    term.feed(b"\x1b(0lqqqk\x1b7\x1b(B plain\x1b8x")
    assert term[0].text == "┌───┐│plain"

    # G1 via SO/SI (0x0E / 0x0F), G2 via SS2 (ESC N), G3 via SS3 (ESC O).
    term.reset()
    term.feed(b"\x1b)0\x0emqj\x0fa\x1b*0\x1bNl\x1b+0\x1bOkz")
    assert term[0].text == "└─┘a┌┐z"


def test_cbt_cht_decsca_decsed_decsel_and_csi_22_j() -> None:
    """``CBT``/``CHT``, ``DECSCA`` selective erase, and ``CSI 22 J`` scroll-push."""
    term = libvt.Terminal(rows=4, cols=24, scrollback=20)
    # CHT 2 jumps 0 -> 16; 'a' prints at 16 (cursor -> 17); CBT 2 steps 17 -> 16 -> 8.
    term.feed(b"\x1b[2Ia\x1b[2Zb")
    assert term[0, 16].text == "a"
    assert term[0, 8].text == "b"

    # DECSCA: CSI 1 " q protects cells from DECSEL (CSI ? 2 K) & DECSED (CSI ? 2 J).
    term.reset()
    term.feed(b'\x1b[1"qSAFE\x1b[0"qWIPE\x1b[?2K')
    assert term[0].text == "SAFE"
    term.feed(b"\x1b[?2J")
    assert term[0].text == "SAFE"
    # Unconditional ED 2 erases protected cells too.
    term.feed(b"\x1b[2J")
    assert term[0].text == ""

    # CSI 22 J (xterm / Ghostty E3 scroll-clear) pushes viewport lines into scrollback.
    term.reset()
    term.feed(b"first\r\nsecond")
    term.feed(b"\x1b[22J")
    assert term.text == ""
    assert [r.text for r in term.history] == ["first", "second"]


def test_alt_screen_single_truth_decrqm_and_mode_aliases() -> None:
    """``?47``, ``?1047``, and ``?1049`` report real buffer state via ``DECRQM``."""
    term = libvt.Terminal(rows=4, cols=12)
    term.feed(b"primary\x1b[?47h\x1b[?1049l\x1b[?47$p")
    assert term[0].text == "primary"
    assert term.screens.active_is_alt is False
    # Status 2 means recognized & reset (NOT stale 1 = set).
    assert term.drain_replies() == b"\x1b[?47;2$y"


def test_tmux_vs_xterm_pending_wrap_and_reverse_wrap() -> None:
    """``TMUX_PROFILE`` models phantom-column ``pending_wrap`` and soft-wrap ``BS``."""
    xt = libvt.Terminal(rows=3, cols=5, profile=XTERM_PROFILE)
    xt.feed(b"12345\x1b[1DX")
    assert xt[0].text == "123X5"

    tm = libvt.Terminal(rows=3, cols=5, profile=TMUX_PROFILE)
    tm.feed(b"12345\x1b[1DX")
    assert tm[0].text == "1234X"

    # Soft-wrap BS across row boundary in TMUX_PROFILE vs XTERM_PROFILE with ?45h.
    tm.reset()
    tm.feed(b"abcdef\x08\x08Z")
    assert tm[0].text == "abcdZ"

    xt.reset()
    xt.feed(b"\x1b[?45habcdef\x08\x08Z")
    assert xt[0].text == "abcdZ"


def test_resize_reflow_primary_vs_alt_and_inband_2048() -> None:
    """Primary soft-wrapped lines reflow on resize; alt crops; ``?2048`` notifies."""
    term = libvt.Terminal(rows=4, cols=10, scrollback=20, record_events=True)
    term.feed(b"\x1b[?2048h0123456789abcdefgh\r\nhard")
    assert term[0].text == "0123456789"
    assert term[0].wrapped is True
    assert term[1].text == "abcdefgh"
    assert term[2].text == "hard"

    term.resize(rows=4, cols=20)
    assert term[0].text == "0123456789abcdefgh"
    assert term[0].wrapped is False
    assert term[1].text == "hard"
    assert term.drain_replies() == b"\x1b[48;4;20;0;0t"
    events = term.drain_events()
    assert InBandResize(rows=4, cols=20) in events

    # Alt screen never reflows logical lines.
    term.feed(b"\x1b[?1049h\x1b[H0123456789abcdefgh")
    term.resize(rows=4, cols=10)
    assert term[0].text == "0123456789"
    assert term[0].wrapped is False


def test_synchronized_output_cow_feed_until_and_generation_expiry() -> None:
    """``?2026`` CoW holds ``committed``, ``feed_until`` steps, and expiry checks."""
    term = libvt.Terminal(rows=4, cols=16, record_events=True)
    term.feed(b"frame-0")
    assert term.committed[0].text == "frame-0"

    batch = b"\x1b[?2026h\x1b[Hpartial\x1b[?2026l\x1b[?2026h\x1b[Hframe-2"
    res1 = term.feed_until(batch, stop=StopOn.SYNC_END)
    assert res1.stopped_on == StopOn.SYNC_END
    assert term.synchronized is False
    assert term.committed[0].text == "partial"

    term.feed(batch[res1.offset :])
    assert term.synchronized is True
    assert term.committed[0].text == "partial"
    assert term.snapshot()[0].text == "frame-2"

    cur_gen = term.sync_generation
    assert term.expire_synchronized_output(cur_gen - 1) is False
    assert term.synchronized is True
    assert term.expire_synchronized_output(cur_gen) is True
    assert term.synchronized is False
    assert term.committed[0].text == "frame-2"
    hold_events = [e for e in term.drain_events() if isinstance(e, RenderHoldChanged)]
    assert hold_events[-1] == RenderHoldChanged(
        active=False,
        sync_generation=cur_gen,
        expired=True,
    )

    # StopOn.GROUND must not fire in the middle of a multi-byte UTF-8 sequence.
    utf8_term = libvt.Terminal(rows=2, cols=10)
    part1 = utf8_term.feed_until("日".encode()[:2], stop=StopOn.GROUND)
    assert part1.stopped_on == StopOn.EOF
    part2 = utf8_term.feed_until("日A".encode()[2:], stop=StopOn.GROUND)
    assert part2.stopped_on == StopOn.GROUND
    assert part2.offset == 1
    assert utf8_term[0, 0].text == "日"


def test_formatter_plain_vt_html_xss_and_snapshot_to_dict() -> None:
    """Formatters roundtrip VT, coalesce HTML spans, block XSS URIs, and serialize."""
    term = libvt.Terminal(rows=3, cols=20)
    term.feed(
        b"\x1b[1;38;2;200;40;40mhi\x1b[0m "
        b"\x1b]8;;https://example.com\x07ok\x1b]8;;\x07 "
        b"\x1b]8;;javascript:alert(1)\x07bad\x1b]8;;\x07",
    )
    snap = term.snapshot()
    assert snap.format(OutputFormat.PLAIN) == "hi ok bad"

    html_out = snap.format(OutputFormat.HTML)
    assert 'href="https://example.com"' in html_out
    assert "javascript:" not in html_out

    vt_text = snap.format(OutputFormat.VT)
    roundtrip = libvt.Terminal(rows=3, cols=20)
    roundtrip.feed_str(vt_text)
    assert roundtrip.text == snap.text
    assert roundtrip[0, 0].style == snap[0, 0].style

    payload_dict = snap.to_dict()
    assert payload_dict["rows"] == 3
    assert payload_dict["cols"] == 20

    strict_term = libvt.Terminal(rows=2, cols=10, policy=Policy(strict=True))
    with pytest.raises(ProtocolError):
        strict_term.feed(b"\x1b[999z")
