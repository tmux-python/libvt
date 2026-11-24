"""Tests for ``libvt.terminal``, ``Profile`` quirks, ``?2026``, and deltas."""

from __future__ import annotations

import copy
import dataclasses
import pickle
import random
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
    assert (tuple(ed3_t.history), ed3_t.lines, ed3_t.cursor.pending_wrap) == (
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

    # SyncToken is opaque per Terminal; mutating another Terminal never bumps it.
    other_term = libvt.Terminal(rows=2, cols=6)
    tok_before_other = term.sync_token()
    assert not dataclasses.is_dataclass(libvt.SyncToken)
    assert not hasattr(tok_before_other, "terminal_id")
    assert not hasattr(tok_before_other, "_terminal_id")
    assert not hasattr(tok_before_other, "modes")
    assert len(repr(tok_before_other)) < 80
    assert hash(tok_before_other) == hash(term.sync_token())
    assert copy.copy(tok_before_other) == tok_before_other
    assert copy.deepcopy(tok_before_other) == tok_before_other
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        roundtrip = pickle.loads(pickle.dumps(tok_before_other, protocol=proto))
        assert roundtrip == tok_before_other
        assert (
            other_term.changes_since(roundtrip).resync_reason
            == ResyncReason.TERMINAL_MISMATCH
        )
    with pytest.raises(TypeError):
        libvt.SyncToken()
    with pytest.raises(AttributeError):
        t.cast("t.Any", tok_before_other).extra = 1
    with pytest.raises(TypeError):
        term.changes_since(t.cast("t.Any", "bad"))
    other_term.feed_str("hello world")
    assert term.sync_token() == tok_before_other

    # Direct Screen / ModeState mutators preserve ?2026 holds and replica sync.
    direct_t = libvt.Terminal(rows=3, cols=6)
    direct_t.feed(b"HELLO")
    d0 = direct_t.changes_since(None)
    dh: list[RowView] = []
    dv: list[RowView] = []
    d0.apply_rows(dh, dv)
    direct_t.screen.reset()
    d1 = direct_t.changes_since(d0.token)
    d1.apply_rows(dh, dv)
    direct_t.feed(b"\x1b[2;1HWORLD")
    d2 = direct_t.changes_since(d1.token)
    d2.apply_rows(dh, dv)
    assert [r.text for r in dv] == list(d2.snapshot.lines)

    hold_t = libvt.Terminal(rows=2, cols=6)
    hold_t.feed(b"SAFE\x1b[?2026h")
    hold_t.screen.reset()
    assert hold_t.committed.text == "SAFE"

    # DCH / ICH / SD / IL under DECLRMM & non-reflow resize preserve row invariants.
    dch_lr = libvt.Terminal(rows=3, cols=6)
    dch_lr.feed(b"ABCDEFG\x1b[?69h\x1b[3;6s\x1b[1;1H")
    lr_tok = dch_lr.sync_token(view="working")
    dch_lr.feed(b"\x1b[P\x1b[@")
    lr_delta = dch_lr.changes_since(lr_tok, view="working")
    assert dch_lr.snapshot()[0].wrapped is True
    assert lr_delta.dirty_rows == ()

    sd_lr = libvt.Terminal(rows=4, cols=6)
    sd_lr.feed("ABCDE日12".encode() + b"\x1b[?69h\x1b[3;6s\x1b[1;1H\x1b[T\x1b[?69l")
    assert sd_lr[1].wrapped is False
    assert sd_lr[1, 5].width_role != libvt.WidthRole.SPACER_HEAD

    il_lr = libvt.Terminal(rows=3, cols=6)
    il_lr.feed("ABCDE日1234".encode() + b"\x1b[?69h\x1b[2;5s\x1b[2;2H\x1b[L")
    assert not (il_lr[0].wrapped and il_lr[0, 5].text == " ")

    ich_w = libvt.Terminal(rows=2, cols=6)
    ich_w.feed("ABC日".encode() + b"\x1b[?69h\x1b[2;4s\x1b[1;2H\x1b[@")
    ref_w = libvt.Terminal(rows=2, cols=6)
    ref_w.feed(b"A BC")
    assert ich_w.snapshot()[0].text == "A BC"
    assert ich_w.snapshot()[0] == ref_w.snapshot()[0]

    alt_rz = libvt.Terminal(rows=3, cols=4)
    alt_rz.feed(b"\x1b[?1049h" + "ABC日".encode())
    alt_rz.resize(rows=3, cols=6)
    alt_rz.feed(b"\x1b[1;5HXY")
    assert alt_rz[0, 3].width_role != libvt.WidthRole.SPACER_HEAD
    assert alt_rz[0].text == "ABC XY"

    # Seeded replica & _Row write-barrier fingerprint fuzzer.
    rng = random.Random(20261123)
    fuzz_tokens: tuple[bytes, ...] = (
        b"ABCD",
        "日本".encode(),
        b"a\xcc\x81",
        b"\r",
        b"\n",
        b"\x08",
        b"\x1b[1;1H",
        b"\x1b[2;3H",
        b"\x1b[A",
        b"\x1b[B",
        b"\x1b[C",
        b"\x1b[D",
        b"\x1b[@",
        b"\x1b[2@",
        b"\x1b[P",
        b"\x1b[2P",
        b"\x1b[2X",
        b"\x1b[K",
        b"\x1b[1K",
        b"\x1b[2K",
        b"\x1b[J",
        b"\x1b[2J",
        b"\x1b[3J",
        b"\x1b[L",
        b"\x1b[M",
        b"\x1b[S",
        b"\x1b[T",
        b"\x1b[1;3r",
        b"\x1b[2;4r",
        b"\x1b[r",
        b"\x1b[?69h\x1b[3;5s",
        b"\x1b[?69l",
        b"\x1b[31;44m",
        b"\x1b[0m",
        b"\x1b[?1049h",
        b"\x1b[?1049l",
        b"\x1b[?2026h",
        b"\x1b[?2026l",
    )
    for trial in range(50):
        ft = libvt.Terminal(rows=4, cols=6, scrollback=8)
        w_tok = ft.sync_token(view="working")
        c_tok2 = ft.sync_token(view="committed")
        w_hist: list[RowView] = []
        w_view: list[RowView] = [ft.snapshot()[i] for i in range(4)]
        c_hist: list[RowView] = []
        c_view: list[RowView] = [ft.committed[i] for i in range(4)]
        prev_fps = [r.debug_fingerprint() for r in ft.screen._rows]

        for step_i in range(24):
            if step_i == 12 and trial % 5 == 0:
                ft.resize(rows=rng.choice((3, 5)), cols=rng.choice((5, 8)))
            else:
                ft.feed(rng.choice(fuzz_tokens))

            cur_fps = [r.debug_fingerprint() for r in ft.screen._rows]
            dw = ft.changes_since(w_tok, view="working")
            if dw.resync_reason is ResyncReason.NONE and dw.viewport_scroll_up == 0:
                changed_idx = tuple(
                    i for i in range(ft.rows) if cur_fps[i] != prev_fps[i]
                )
                for ci in changed_idx:
                    assert ci in dw.dirty_rows
            dw.apply_rows(w_hist, w_view)
            ws = dw.snapshot
            assert w_hist == list(ws.history)
            assert w_view == [ws[i] for i in range(ws.rows)]
            w_tok = dw.token
            prev_fps = cur_fps

            dc = ft.changes_since(c_tok2, view="committed")
            dc.apply_rows(c_hist, c_view)
            cs = dc.snapshot
            assert c_hist == list(cs.history)
            assert c_view == [cs[i] for i in range(cs.rows)]
            c_tok2 = dc.token


def test_bce_strips_hyperlink_su_history_and_vt_formatter_roundtrip() -> None:
    """``BCE`` drops ``OSC 8``, ``SU`` scrolls to history, and ``VT`` round-trips."""
    bce_t = libvt.Terminal(rows=3, cols=8)
    bce_t.feed(
        b"\x1b]8;id=1;https://example.com\x07\x1b[42m"
        b"AB\x1b[K\r\n"
        b"CD\x1b[2X\r\n"
        b"EF\x1b[1;1H\x1b[1;2r\x1b[1L\x1b[r\x1b]8;;\x07"
    )
    assert bce_t[1, 0].hyperlink == libvt.Hyperlink(uri="https://example.com", id="1")
    for r_i, c_i in ((0, 0), (0, 5), (1, 2), (1, 3)):
        assert bce_t[r_i, c_i].style.bg == AnsiColor.GREEN
        assert bce_t[r_i, c_i].hyperlink is None

    su_t = libvt.Terminal(rows=3, cols=8, scrollback=10)
    su_t.feed(b"\x1b]133;A\x07rowA\r\nrowB\r\n\x1b]133;A\x07rowC\x1b[1;2r\x1b[1S")
    su_snap = su_t.snapshot()
    assert [r.text for r in su_snap.history] == ["rowA"]
    assert su_snap.lines == ("rowB", "", "rowC")
    assert [su_snap.at_abs(z.abs_row).text for z in su_snap.prompt_zones] == [
        "rowA",
        "rowC",
    ]
    # Huge Ps in SU clamps to region height rather than flooding scrollback.
    su_t.feed(b"\x1b[999S")
    assert len(su_t.history) == 3

    # Alt-screen SU and narrow-margin SU do not push to primary scrollback.
    su_t.feed(b"\x1b[r\x1b[?1049hALT1\r\nALT2\x1b[1S\x1b[?1049l")
    assert len(su_t.history) == 3

    # Formatter(OutputFormat.VT) round-trips IRM / DECOM / DECLRMM / ?7l / ?25l /
    # ?2027l / DECSCUSR and short wrapped=True rows from tmux -T.
    src_t = libvt.Terminal.from_tmux_capture(
        ["W ab", "- GH", "- "],
        rows=3,
        cols=6,
        flagged=True,
        trim_trailing=True,
        profile=DEFAULT_PROFILE,
    )
    assert src_t.screen._rows[0].used == 2
    src_t.feed(
        b"\x1b[2;3H\x1b[42m\x1b[2X\x1b[0m"
        b"\x1b[4h\x1b[?69h\x1b[2;5s\x1b[?6h\x1b[?7l\x1b[?25l\x1b[5 q\x1b[2;2H"
    )
    snap = src_t.snapshot()
    vt_out = snap.format(OutputFormat.VT)
    dst_t = libvt.Terminal(rows=3, cols=6)
    dst_t.feed_str(vt_out)
    dst_snap = dst_t.snapshot()
    assert dst_snap[0].wrapped is True
    assert dst_snap[0, 0].text == "a"
    assert dst_snap[0, 1].text == "b"
    assert dst_snap[1].text == "GH"
    assert dst_snap[1, 2].style.bg == AnsiColor.GREEN
    assert dst_snap.modes == snap.modes
    assert dst_snap.cursor == snap.cursor

    # ?2027l and ?25l across ?1049h round-trip through Formatter(OutputFormat.VT).
    flag_t = libvt.Terminal(rows=2, cols=8)
    flag_t.feed(b"\x1b[?25l\x1b[?1049h\x1b[?2027l" + "🇺🇸X".encode())
    flag_dst = libvt.Terminal(rows=2, cols=8)
    flag_dst.feed_str(flag_t.snapshot().format(OutputFormat.VT))
    assert flag_dst.cursor.visible is False
    assert (flag_dst[0, 1].text, flag_dst[0, 2].text) == ("🇸", "X")

    # Bottom-row wrapped=True from tmux capture preserves penultimate wrap & BCE.
    s_bot1 = libvt.Snapshot.from_tmux_capture(
        ["W abcdef", "W "], rows=2, cols=6, flagged=True, trim_trailing=True
    )
    d_bot1 = libvt.Terminal(rows=2, cols=6)
    d_bot1.feed_str(s_bot1.format(OutputFormat.VT))
    assert d_bot1[0].wrapped is True

    t_bot2 = libvt.Terminal.from_tmux_capture(
        ["- abcdef", "W ab"], rows=2, cols=6, flagged=True, trim_trailing=True
    )
    t_bot2.feed(b"\x1b[2;3H\x1b[42m\x1b[3X\x1b[0m")
    d_bot2 = libvt.Terminal(rows=2, cols=6)
    d_bot2.feed_str(t_bot2.snapshot().format(OutputFormat.VT))
    assert d_bot2[1, 2].style.bg == AnsiColor.GREEN


def test_profile_decstr_decsc_declrmm_and_top_margin_history() -> None:
    """``DEFAULT`` vs ``TMUX_PROFILE`` split ``DECSTR``, ``DECSC``, and ``?69``."""
    # 1. DECSTR on DEFAULT / XTERM resets only VT510 soft-reset state.
    for prof in (DEFAULT_PROFILE, libvt.XTERM_PROFILE):
        dt = libvt.Terminal(rows=4, cols=8, profile=prof)
        dt.feed(
            b"\x1b[?1000h\x1b[?1006h\x1b[?2004h\x1b[>1u"
            b"\x1b[4h\x1b[?7l\x1b[?25l\x1b[?69h\x1b[2;4r\x1b[2;6s\x1b[?6h"
            b"\x1b[31m\x1b]8;;https://example.com\x07\x1b[5 q\x1b)0\x0e"
            b"\x1b[2;2H\x1b7\x1b[!p"
        )
        assert dt.modes.is_set(libvt.Mode.MOUSE_VT200) is True
        assert dt.modes.is_set(libvt.Mode.MOUSE_SGR) is True
        assert dt.modes.is_set(libvt.Mode.BRACKETED_PASTE) is True
        assert int(dt.kitty_keyboard_flags) == 1
        assert dt.modes.is_set(libvt.Mode.IRM) is False
        assert dt.modes.is_set(libvt.Mode.DECOM) is False
        assert dt.modes.is_set(libvt.Mode.DECAWM) is True
        assert dt.modes.is_set(libvt.Mode.DECTCEM) is True
        assert dt.modes.is_set(libvt.Mode.DECLRMM) is False
        assert dt.cursor.visible is True
        assert (dt.cursor.shape, dt.cursor.blinking) == (libvt.CursorShape.BLOCK, True)
        assert dt.screen.scroll_region == libvt.ScrollRegion(0, 3, 0, 7)
        dt.feed(b"l\x1b8")
        assert dt[dt.cursor.row, 0].style.fg is None
        assert (dt.cursor.row, dt.cursor.col) == (0, 0)

    # 2. _single_shift saved/restored by DECSC; DEFAULT shares primary slot with ?1049h
    #    while alt screen keeps its own DECSC slot; CSI 5;10 s with ?69 off is ignored.
    ss_t = libvt.Terminal(rows=4, cols=8, profile=DEFAULT_PROFILE)
    ss_t.feed(b"\x1b*0\x1bN\x1b7\x1b[2;2Hll\x1b8ll")
    assert (ss_t[1, 1].text, ss_t[1, 2].text) == ("┌", "l")
    assert (ss_t[0, 0].text, ss_t[0, 1].text) == ("┌", "l")

    def_sc = libvt.Terminal(rows=4, cols=8, profile=DEFAULT_PROFILE)
    def_sc.feed(b"\x1b[3;1H\x1b7\x1b[3;3H\x1b[5;10s\x1b8")
    assert (def_sc.cursor.row, def_sc.cursor.col) == (2, 0)
    def_sc.feed(b"\x1b[3;3H\x1b[?1049h\x1b[1;5H\x1b7\x1b[2;6H\x1b8\x1b[?1049l\x1b8")
    assert (def_sc.cursor.row, def_sc.cursor.col) == (2, 2)

    # 3. TMUX_PROFILE: separate DECSC vs ?1049h slots, ?69/?1048 off, CSI 5;10 s saves.
    tm_sc = libvt.Terminal(rows=4, cols=8, profile=TMUX_PROFILE)
    tm_sc.feed(
        b"\x1b[3;1H\x1b7\x1b[1;5H\x1b[?1048h\x1b[?1048$p\x1b[3;3H\x1b[?1049h\x1b[?1049l\x1b8"
    )
    assert tm_sc.drain_replies() == b"\x1b[?1048;0$y"
    assert (tm_sc.cursor.row, tm_sc.cursor.col) == (2, 0)
    tm_sc.feed(b"\x1b[?69h\x1b[?69$p\x1b[2;5H\x1b[5;10s\x1b[4;7H\x1b[u")
    assert tm_sc.modes.is_set(libvt.Mode.DECLRMM) is False
    assert tm_sc.drain_replies() == b"\x1b[?69;0$y"
    assert (tm_sc.cursor.row, tm_sc.cursor.col) == (1, 4)

    # Pending SS2 does not leak onto fresh alternate screen on ?1049h.
    ss_alt = libvt.Terminal(rows=2, cols=8, profile=DEFAULT_PROFILE)
    ss_alt.feed(b"\x1b*0\x1bN\x1b[?1049hl")
    assert ss_alt[0, 0].text == "l"

    # 4. TMUX_PROFILE top > 0 DECSTBM scrollback + ScreenDelta + PromptZone + gfx.
    tm_top = libvt.Terminal(rows=4, cols=8, scrollback=10, profile=TMUX_PROFILE)
    tok = tm_top.sync_token()
    rep_h: list[RowView] = []
    rep_v: list[RowView] = [tm_top.snapshot()[i] for i in range(4)]
    tm_top.feed(
        b"\x1b]133;A\x07HDR\r\n"
        b"\x1b]133;A\x07R1\r\n"
        b"R2\r\n"
        b"\x1b]133;A\x07FTR"
        b"\x1b[2;3r\x1b[3;1H\nNEW2"
    )
    delta = tm_top.changes_since(tok)
    delta.apply_rows(rep_h, rep_v)
    snap_top = delta.snapshot
    assert [r.text for r in snap_top.history] == ["R1"]
    assert snap_top.lines == ("HDR", "R2", "NEW2", "FTR")
    assert [r.text for r in rep_h] == ["R1"]
    assert [r.text for r in rep_v] == ["HDR", "R2", "NEW2", "FTR"]
    assert [snap_top.at_abs(z.abs_row).text for z in snap_top.prompt_zones] == [
        "HDR",
        "R1",
        "FTR",
    ]
    # DL inside top > 0 does not push to history.
    tm_top.feed(b"\x1b[2;1H\x1b[M")
    assert [r.text for r in tm_top.history] == ["R1"]
    assert tm_top.lines == ("HDR", "NEW2", "", "FTR")

    # Wrapped row evicted from top > 0 DECSTBM or ED 2 does not glue onto viewport[0],
    # and SIXEL inside top > 0 DECSTBM clips at top without covering HDR.
    tm_wrap = libvt.Terminal(rows=4, cols=8, scrollback=10, profile=TMUX_PROFILE)
    tm_wrap.feed(b"HEADER\r\n1234567890\r\nFOOTER\x1b[2;3r\x1b[3;1H\n")
    assert tm_wrap.history[-1].wrapped is False
    tm_wrap.resize(rows=4, cols=16, reflow=True)
    assert tm_wrap.lines[0] == "HEADER"

    tm_sx = libvt.Terminal(rows=4, cols=8, scrollback=10, profile=TMUX_PROFILE)
    tm_sx.feed(
        b'HEADER\r\nR1\r\nR2\r\nFOOTER\x1b[2;3r\x1b[3;1H\x1bP0;1;0q"1;1;16;48#0!16~\x1b\\'
    )
    sx_snap = tm_sx.snapshot()
    assert sx_snap.placements[0].occupies_abs(sx_snap.scrolled_total, 0) is False

    # DEFAULT_PROFILE keeps top > 0 DECSTBM scrolls out of history.
    def_top = libvt.Terminal(rows=4, cols=8, scrollback=10, profile=DEFAULT_PROFILE)
    def_top.feed(b"HDR\r\nR1\r\nR2\r\nFTR\x1b[2;3r\x1b[3;1H\nNEW2")
    assert list(def_top.history) == []
    assert def_top.lines == ("HDR", "R2", "NEW2", "FTR")
