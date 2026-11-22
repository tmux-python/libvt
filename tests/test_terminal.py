"""Tests for ``libvt.terminal``, ``Profile`` quirks, ``?2026``, and deltas."""

from __future__ import annotations

import time
import typing as t

import pytest

import libvt
from libvt.config import (
    DEFAULT_PROFILE,
    TMUX_PROFILE,
    XTERM_PROFILE,
    Policy,
    Profile,
    SixelErasePolicy,
)
from libvt.events import RenderHoldChanged, ScreenErased
from libvt.exc import ProtocolError
from libvt.modes import Mode
from libvt.screen import RowView
from libvt.style import AnsiColor
from libvt.terminal import OutputFormat, ResyncReason, StopOn


class TmuxVersionCase(t.NamedTuple):
    """Typed version-parsing scenario for :meth:`Profile.tmux`."""

    test_id: str
    version: str
    rep_wraps: bool
    sixel_erase_policy: SixelErasePolicy


TMUX_VERSION_CASES: list[TmuxVersionCase] = [
    TmuxVersionCase("tmux-3.8a", "tmux 3.8a", False, "whole_image"),
    TmuxVersionCase("tmux-3.8-rc3", "tmux 3.8-rc3", False, "whole_image"),
    TmuxVersionCase("next-3.9", "next-3.9", True, "cell_mask"),
    TmuxVersionCase("release-3.9", "3.9", True, "cell_mask"),
]


@pytest.mark.parametrize(
    "case",
    TMUX_VERSION_CASES,
    ids=[c.test_id for c in TMUX_VERSION_CASES],
)
def test_tmux_profile_version_matrix(case: TmuxVersionCase) -> None:
    """``Profile.tmux`` gates ``REP`` wrap and ``SIXEL`` erase policy by version."""
    prof = Profile.tmux(case.version)
    assert prof.rep_wraps is case.rep_wraps
    assert prof.sixel_erase_policy == case.sixel_erase_policy
    term = libvt.Terminal(rows=2, cols=5, profile=prof)
    term.feed(b"1234X\x1b[2b")
    assert term.lines == (("1234X", "XX") if case.rep_wraps else ("1234X", ""))


def test_charsets_g0_g3_decsc_and_linux_console_profile() -> None:
    """``G0..G3`` DEC Special Graphics, ``DECSC``/``DECRC``, and ``linux_console``."""
    term = libvt.Terminal(rows=3, cols=16)
    term.feed(b"\x1b(0lqqqk\x1b7\x1b(B plain\x1b8x")
    assert term[0].text == "┌───┐│plain"

    term.reset()
    term.feed(b"\x1b)0\x0emqj\x0fa\x1b*0\x1bNl\x1b+0\x1bOkz")
    assert term[0].text == "└─┘a┌┐z"

    lt = libvt.Terminal(rows=2, cols=8, profile=Profile.linux_console())
    lt.feed(b"\x0elqqk\x0f")
    assert lt[0].text == "┌──┐"


def test_csi_motions_selective_erase_ed3_ed2_and_decstr() -> None:
    """``CBT/CHT``, ``HPR/VPR``, ``DECSCA``, ``ED 2/3/22``, ``DECALN``, ``DECSTR``."""
    term = libvt.Terminal(rows=4, cols=24, scrollback=20)
    term.feed(b"\x1b[2Ia\x1b[2Zb")
    assert (term[0, 16].text, term[0, 8].text) == ("a", "b")

    term.reset()
    term.feed(b'\x1b[1"qSAFE\x1b[0"qWIPE\x1b[?2K\x1b[?2J')
    assert term[0].text == "SAFE"
    term.feed(b"\x1b[2J")
    assert term[0].text == ""

    term.reset()
    term.feed(b"first\r\nsecond\x1b[22J")
    assert (term.text, [r.text for r in term.history]) == ("", ["first", "second"])

    # HPR / VPR obey DECOM and are ignored quietly under TMUX_PROFILE.
    margin_t = libvt.Terminal(rows=4, cols=8)
    margin_t.feed(b"\x1b[?69h\x1b[2;3r\x1b[3;6s\x1b[2;3H\x1b[10a\x1b[10e")
    assert (margin_t.cursor.row, margin_t.cursor.col) == (3, 7)
    margin_t.feed(b"\x1b[?6h\x1b[1a\x1b[1e\x1b[99a\x1b[99e")
    assert (margin_t.cursor.row, margin_t.cursor.col) == (2, 5)

    tm_strict = libvt.Terminal(
        rows=2,
        cols=8,
        profile=TMUX_PROFILE,
        policy=Policy(strict=True),
    )
    tm_strict.feed(b"A\x1b[3aB\x1b[2eC\x1b[!p")
    assert tm_strict[0].text == "ABC"

    # DECALN resets margins/wraps; non-print clears REP; ?47/?1049 share DECRQM truth.
    aln_t = libvt.Terminal(rows=2, cols=4)
    aln_t.feed(b"ABCDEF\x1b[?69h\x1b[2;3s\x1b#8")
    assert (aln_t.lines, aln_t[0].wrapped) == (("EEEE", "EEEE"), False)
    aln_t.feed(b"\x1b[1;1HA\x1b[C\x1b[3b\x1b[?47h\x1b[?1049l\x1b[?47$p")
    assert aln_t.drain_replies() == b"\x1b[?47;2$y"

    # ED 3 preserves viewport & pending_wrap; alt ED 3 obeys profile flag.
    ed3_t = libvt.Terminal(rows=2, cols=4, scrollback=10, record_events=True)
    ed3_t.feed(b"old\r\nnew\r\nABCD")
    ed3_t.drain_events()
    ed3_t.feed(b"\x1b[3J\x1b[?3J\x1b[99J")
    assert (ed3_t.history, ed3_t.lines, ed3_t.cursor.pending_wrap) == (
        (),
        ("new", "ABCD"),
        True,
    )
    assert [e for e in ed3_t.drain_events() if isinstance(e, ScreenErased)] == []

    def_alt = libvt.Terminal(rows=2, cols=6, scrollback=10, profile=DEFAULT_PROFILE)
    def_alt.feed(b"h1\r\nh2\r\nv\x1b[?1049hALT\x1b[3J")
    assert [r.text for r in def_alt.history] == ["h1"]

    # TMUX_PROFILE ignores DECSTR and scrolls primary ED 2 / ED 0 at (0,0) to history.
    tm2 = libvt.Terminal(rows=4, cols=10, scrollback=20, profile=TMUX_PROFILE)
    tm2.feed(b"ROW_ONE\r\nROW_TWO\x1b[4;5H\x1b[44m\x1b[2J")
    assert [r.text for r in tm2.history] == ["ROW_ONE", "ROW_TWO"]
    assert (tm2[0, 0].style.bg, tm2.cursor.row, tm2.cursor.col) == (
        AnsiColor.BLUE,
        3,
        4,
    )
    tm2.feed(b"\x1b[0m\x1b[HHOME_ROW\x1b[H\x1b[J\x1b[?1049hALT_LINE\x1b[2J")
    assert [r.text for r in tm2.history] == [
        "ROW_ONE",
        "ROW_TWO",
        "HOME_ROW",
        "",
        "",
        "",
    ]
    assert tm2.history[3][0].style.bg == AnsiColor.BLUE


def test_right_margin_vs16_zwsp_bmp_slicing_and_no_autowrap_fast_path() -> None:
    """Right-margin ``VS16`` preserves SGR/margins/``IRM``, ``ZWSP`` breaks, ``?7l``."""
    payload = "\x1b[31mabc1\x1b[34m\ufe0f\u20e3Z"
    for per_byte in (False, True):
        term = libvt.Terminal(rows=3, cols=4)
        if per_byte:
            raw = payload.encode("utf-8")
            for idx in range(len(raw)):
                term.feed(raw[idx : idx + 1])
        else:
            term.feed_str(payload)
        assert (term[0].text, term[0].wrapped) == ("abc", True)
        assert (term[1, 0].text, term[1, 0].width, term[1, 0].style.fg) == (
            "1\ufe0f\u20e3",
            2,
            AnsiColor.RED,
        )
        assert (term[1, 2].text, term[1, 2].style.fg) == ("Z", AnsiColor.BLUE)

    bounded = libvt.Terminal(rows=4, cols=4)
    bounded.feed_str("\x1b[1;2rL0\r\nabc1\ufe0f\u20e3Z")
    assert (bounded[0].text, bounded[0].wrapped, bounded[1, 0].text) == (
        "abc",
        True,
        "1\ufe0f\u20e3",
    )

    irm = libvt.Terminal(rows=3, cols=4)
    irm.feed_str("\x1b[2;1HXY\x1b[1;1H\x1b[4habc1\ufe0f\u20e3")
    assert (irm[1, 0].text, irm[1, 2].text, irm[1, 3].text) == (
        "1\ufe0f\u20e3",
        "X",
        "Y",
    )

    zwsp = libvt.Terminal(rows=2, cols=6)
    zwsp.feed_str("1\u200b\ufe0fA")
    assert (zwsp[0, 0].width, zwsp[0, 1].text) == (1, "A")

    # Narrow-BMP slice fast path + split GCB_PREPEND + ?7l clamp & O(cols) collapse.
    bmp = libvt.Terminal(rows=3, cols=8)
    bmp.feed("\x1b[32mcafé\x1b[2b ─│┌┐".encode())
    assert (bmp[0].text, bmp[0].wrapped, bmp[1].text) == ("cafééé ─", True, "│┌┐")

    split = libvt.Terminal(rows=2, cols=8)
    split.feed("\u0600".encode())
    split.feed(b"1")
    assert split[0, 0].text == "\u06001"

    no_wrap = libvt.Terminal(rows=2, cols=4)
    no_wrap.feed("\x1b[?7lABC\u26a0\ufe0f".encode())
    assert (libvt.display_width(no_wrap[0].text) <= 4, no_wrap[0, 3].width) == (True, 1)

    t0 = time.monotonic()
    no_wrap.feed_str("\x1b[1;1H" + ("ABCD" * 2_500) + "!")
    assert time.monotonic() - t0 < 0.1
    assert no_wrap[0].text == "ABC!"


def test_tmux_phantom_column_reverse_wrap_and_il_dl() -> None:
    """``TMUX_PROFILE`` models phantom col ``cols``, reverse ``BS``, and ``IL/DL``."""
    xt = libvt.Terminal(rows=3, cols=5, profile=XTERM_PROFILE)
    xt.feed(b"12345\x1b[1DX")
    assert xt[0].text == "123X5"
    xt.reset()
    xt.feed(b"\x1b[?45habcdef\x08\x08Z")
    assert xt[0].text == "abcdZ"

    tm = libvt.Terminal(rows=3, cols=5, profile=TMUX_PROFILE)
    tm.feed(b"12345\x1b[1DX")
    assert tm[0].text == "1234X"
    tm.reset()
    tm.feed(b"abcdef\x08\x08Z")
    assert tm[0].text == "abcdZ"

    tm.reset()
    tm.feed(b"\x1b[7mHEAD!\x1b[K\x1b[0m")
    assert (tm[0].text, tm[0, 4].style.reverse, tm.cursor.pending_wrap) == (
        "HEAD!",
        True,
        True,
    )

    tm.reset()
    tm.feed(b"12345\r\nrow-2\x1b[1;1H12345\x1b[J\x1b[X\x1b[@\x1b[P\x1b[6n")
    assert (tm.lines, tm.cursor.pending_wrap, tm.drain_replies()) == (
        ("12345", "", ""),
        True,
        b"\x1b[1;6R",
    )

    tm.reset()
    tm.feed(b"12345\nX")
    assert tm.lines == ("12345", "", "X")
    tm.reset()
    tm.feed(b"\x1b[2;1H12345\x1bMX")
    assert tm.lines == ("", "X2345", "")
    tm.reset()
    tm.feed(b"12345\x1b7\x1b8X")
    assert tm[0].text == "1234X"

    tm.reset()
    tm.feed(b"abcde\r\n12345\x1b[2;4H\x1b[L\x1b[M")
    assert tm.cursor.col == 3


def test_sync_2026_cow_feed_until_and_expiry() -> None:
    """``?2026`` freezes pre-``BSU`` state across cuts, steps, and expires safely."""
    payload = b"B\x1b[?2026hC"
    for chunks in (
        [payload],
        [bytes([b]) for b in payload],
        [payload[:3], payload[3:]],
    ):
        term = libvt.Terminal(rows=2, cols=8)
        term.feed(b"A")
        assert term.committed[0].text == "A"
        for chunk in chunks:
            term.feed(chunk)
        held = term.committed
        assert (held[0].text, held.cursor.col) == ("AB", 2)
        assert Mode.SYNCHRONIZED_OUTPUT not in held.modes
        term.feed(b"\x1b[?2026l")
        assert term.committed[0].text == "ABC"

    term2 = libvt.Terminal(rows=3, cols=16, record_events=True)
    term2.feed(b"A\x1b[?2026h\x1b[2;3H\x1b[?25l\x1b]2;Next\x07\x1b[6n\x1b[?2026$p")
    held2 = term2.committed
    assert (held2.cursor.row, held2.cursor.col, held2.cursor.visible, held2.title) == (
        0,
        1,
        True,
        "",
    )
    assert term2.drain_replies() == b"\x1b[2;3R\x1b[?2026;1$y"

    cur_gen = term2.sync_generation
    assert term2.expire_synchronized_output(cur_gen - 1) is False
    assert term2.expire_synchronized_output(cur_gen) is True
    assert term2.committed.title == "Next"
    hold_events = [e for e in term2.drain_events() if isinstance(e, RenderHoldChanged)]
    assert hold_events[-1].expired is True

    # StopOn.GROUND waits for complete multi-byte UTF-8 scalar.
    utf8_term = libvt.Terminal(rows=2, cols=10)
    assert utf8_term.feed_until("日".encode()[:2], stop=StopOn.GROUND).stopped_on == (
        StopOn.EOF
    )
    part2 = utf8_term.feed_until("日A".encode()[2:], stop=StopOn.GROUND)
    assert (part2.stopped_on, part2.offset, utf8_term[0, 0].text) == (
        StopOn.GROUND,
        1,
        "日",
    )


def test_formatters_bce_xss_and_snapshot_immutability() -> None:
    """``Formatter`` preserves VT BCE, sanitizes HTML URIs, and freezes ``Snapshot``."""
    term = libvt.Terminal(rows=2, cols=20)
    term.feed(
        b"\x1b[42;31;4:3;9;53mHI\x1b[K\r\n"
        b"\x1b[0m\x1b]8;;https://example.com\x07ok\x1b]8;;\x07 "
        b"\x1b]8;;javascript:alert(1)\x07bad\x1b]8;;\x07",
    )
    snap = term.snapshot()
    assert snap.format(OutputFormat.PLAIN) == "HI\nok bad"
    assert snap[0, 19].style.bg == AnsiColor.GREEN

    rt = libvt.Terminal(rows=2, cols=20)
    rt.feed_str(snap.format(OutputFormat.VT))
    assert (rt[0].text, rt[0, 19].style.bg) == ("HI", AnsiColor.GREEN)

    html_out = snap.format(OutputFormat.HTML)
    assert 'href="https://example.com"' in html_out
    assert "javascript:" not in html_out
    assert "text-decoration-style:wavy" in html_out
    assert snap.to_dict()["rows"] == 2

    with pytest.raises(AttributeError):
        snap.title = "mutated"
    with pytest.raises(TypeError):
        t.cast("dict[str, object]", snap.program_statuses)["x"] = None

    strict_term = libvt.Terminal(rows=2, cols=10, policy=Policy(strict=True))
    with pytest.raises(ProtocolError):
        strict_term.feed(b"\x1b[999z")


def test_changes_since_watermarks_and_replica_equivalence() -> None:
    """``changes_since`` and ``apply_lines``/``apply_rows`` maintain exact replicas."""
    term = libvt.Terminal(rows=4, cols=6, scrollback=6, profile=TMUX_PROFILE)
    rep_lines_hist: list[str] = []
    rep_lines_view: list[str] = []
    rep_rows_hist: list[RowView] = []
    rep_rows_view: list[RowView] = []

    def _step(tok: libvt.SyncToken | None) -> libvt.ScreenDelta:
        delta = term.changes_since(tok)
        delta.apply_lines(rep_lines_hist, rep_lines_view)
        delta.apply_rows(rep_rows_hist, rep_rows_view)
        snap = delta.snapshot
        assert rep_lines_hist == [r.text for r in snap.history]
        assert rep_lines_view == list(snap.lines)
        assert [(r.text, r.wrapped) for r in rep_rows_view] == [
            (snap[i].text, snap[i].wrapped) for i in range(snap.rows)
        ]
        return delta

    d = _step(None)
    assert d.resync_reason == ResyncReason.INITIAL

    term.feed_str("abcde中\r\nLINE_2")
    d = _step(d.token)
    assert rep_rows_view[0].wrapped is True
    term.feed_str("\x1b[2;1H\x1b[P")
    d = _step(d.token)
    assert rep_rows_view[0].wrapped is False

    term.feed_str("\x1b[4;1H\r\nS1\r\nS2\r\nS3\r\nS4\r\nS5")
    d = _step(d.token)
    assert (d.resync_reason, d.viewport_scroll_up) == (ResyncReason.NONE, 5)

    term.feed_str("\x1b[1;3r\x1b[3;1H\nTOP_SC\x1b[r\x1b[3J\x1b[4;1H\r\nPOST_CLR")
    d = _step(d.token)
    assert d.history_cleared is True

    term.feed(b"\x1b]7501;state=working:id=build\x07")
    d = _step(d.token)
    assert d.metadata_changed is True
    term.feed(b"\x1b]7;file://remote-box.example/srv/app\x07")
    d = _step(d.token)
    assert (d.metadata_changed, d.snapshot.cwd_uri) == (
        True,
        "file://remote-box.example/srv/app",
    )

    c_tok = term.sync_token(view="committed")
    term.feed(b"\x1b[?2026h\x1b[1;1HTORN")
    assert term.changes_since(c_tok, view="committed").has_changes is False
    assert 0 in term.changes_since(c_tok, view="working").dirty_rows
    term.feed(b"\x1b[?2026l")
    d = _step(c_tok)

    stale_pre_alt = d.token
    term.feed(b"\x1b[?1049hALT\x1b[?1049l")
    d = _step(stale_pre_alt)
    assert d.resync_reason == ResyncReason.EPOCH_CHANGED

    stale_same_epoch = d.token
    f_tok = d.token
    for idx in range(10):
        term.feed(f"\r\nEV{idx}".encode("ascii"))
        f_tok = term.changes_since(f_tok).token
    assert term.changes_since(stale_same_epoch).resync_reason == (
        ResyncReason.HISTORY_EVICTED
    )
