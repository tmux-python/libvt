"""Tests for ``libvt._grid`` and ``libvt.screen`` rows, healing, reflow, and fuzz."""

from __future__ import annotations

import collections.abc
import random
import time
import typing as t

import pytest

import libvt
from libvt._ucd import WidthRole
from libvt.config import DEFAULT_PROFILE, XTERM_PROFILE, Profile
from libvt.events import InBandResize
from libvt.modes import Mode
from libvt.style import AnsiColor


def _assert_grid_invariants(term: libvt.Terminal) -> None:
    """Assert cursor bounds and wide-cell head/tail/spacer_head invariants."""
    scr = term.screen
    declrmm = term.modes.is_set(Mode.DECLRMM)
    assert 0 <= scr.cursor.row < scr.rows
    assert 0 <= scr.cursor.col < scr.cols
    for r_idx, raw_row in enumerate(scr._rows):
        assert len(raw_row.chars) == scr.cols
        assert 0 <= raw_row.used <= scr.cols
        if raw_row.attrs is None:
            continue
        for c_idx in range(scr.cols):
            role = WidthRole((raw_row.attrs[c_idx] >> 60) & 0b11)
            if role == WidthRole.WIDE:
                assert c_idx + 1 < scr.cols, f"orphan WIDE at ({r_idx}, {c_idx})"
                tail_role = WidthRole((raw_row.attrs[c_idx + 1] >> 60) & 0b11)
                assert tail_role == WidthRole.SPACER_TAIL
            elif role == WidthRole.SPACER_TAIL:
                assert c_idx > 0, f"orphan SPACER_TAIL at ({r_idx}, 0)"
                head_role = WidthRole((raw_row.attrs[c_idx - 1] >> 60) & 0b11)
                assert head_role == WidthRole.WIDE
            elif (
                role == WidthRole.SPACER_HEAD
                and not declrmm
                and c_idx == scr.cols - 1
                and r_idx + 1 < scr.rows
                and raw_row.wrapped
            ):
                next_row = scr._rows[r_idx + 1]
                next_role = (
                    WidthRole((next_row.attrs[0] >> 60) & 0b11)
                    if next_row.attrs is not None
                    else WidthRole.NARROW
                )
                assert next_role == WidthRole.WIDE


class ResizeCase(t.NamedTuple):
    """Typed scenario for ``Terminal.resize()`` reflow and ``pending_wrap`` rules."""

    test_id: str
    init_rows: int
    init_cols: int
    profile: Profile
    pre_feed: bytes
    new_rows: int
    new_cols: int
    post_feed: bytes
    expected_lines: tuple[str, ...]
    expected_cursor: tuple[int, int, bool]


RESIZE_CASES: list[ResizeCase] = [
    ResizeCase(
        test_id="same-size-preserves-pending-wrap",
        init_rows=2,
        init_cols=4,
        profile=DEFAULT_PROFILE,
        pre_feed=b"\x1b[1;2rABCD",
        new_rows=2,
        new_cols=4,
        post_feed=b"E",
        expected_lines=("ABCD", "E"),
        expected_cursor=(1, 1, False),
    ),
    ResizeCase(
        test_id="reflow-widen-unwraps-pending-wrap",
        init_rows=2,
        init_cols=4,
        profile=DEFAULT_PROFILE,
        pre_feed=b"ABCD",
        new_rows=2,
        new_cols=8,
        post_feed=b"E",
        expected_lines=("ABCDE", ""),
        expected_cursor=(0, 5, False),
    ),
    ResizeCase(
        test_id="xterm-no-reflow-widen-retains-pending-wrap",
        init_rows=2,
        init_cols=4,
        profile=XTERM_PROFILE,
        pre_feed=b"ABCD",
        new_rows=2,
        new_cols=8,
        post_feed=b"E",
        expected_lines=("ABCD", "E"),
        expected_cursor=(1, 1, False),
    ),
    ResizeCase(
        test_id="reflow-widen-wide-glyph-at-right-margin",
        init_rows=2,
        init_cols=4,
        profile=DEFAULT_PROFILE,
        pre_feed="AB界".encode(),
        new_rows=2,
        new_cols=8,
        post_feed=b"E",
        expected_lines=("AB界E", ""),
        expected_cursor=(0, 5, False),
    ),
    ResizeCase(
        test_id="exact-fit-shrink-no-phantom-blank-row",
        init_rows=2,
        init_cols=8,
        profile=DEFAULT_PROFILE,
        pre_feed=b"ABCD",
        new_rows=2,
        new_cols=4,
        post_feed=b"E",
        expected_lines=("ABCD", "E"),
        expected_cursor=(1, 1, False),
    ),
    ResizeCase(
        test_id="alt-screen-resize-syncs-saved-primary-cursor",
        init_rows=2,
        init_cols=4,
        profile=DEFAULT_PROFILE,
        pre_feed=b"ABCDabc\x1b[?1049h",
        new_rows=2,
        new_cols=8,
        post_feed=b"\x1b[?1049lX",
        expected_lines=("ABCDabcX", ""),
        expected_cursor=(0, 8 - 1, True),
    ),
]


@pytest.mark.parametrize(
    "case",
    RESIZE_CASES,
    ids=[c.test_id for c in RESIZE_CASES],
)
def test_resize_pending_wrap_and_reflow_cases(case: ResizeCase) -> None:
    """``resize()`` preserves ``pending_wrap``, wide cells, and profile reflow."""
    term = libvt.Terminal(
        rows=case.init_rows,
        cols=case.init_cols,
        scrollback=10,
        profile=case.profile,
    )
    term.feed(case.pre_feed)
    term.resize(rows=case.new_rows, cols=case.new_cols)
    term.feed(case.post_feed)
    assert term.lines == case.expected_lines
    assert (
        term.cursor.row,
        term.cursor.col,
        term.cursor.pending_wrap,
    ) == case.expected_cursor


def test_resize_soft_wrap_join_alt_crop_and_inband_2048() -> None:
    """Primary soft wraps join on widen, alt crops, and ``?2048`` emits reply."""
    term = libvt.Terminal(rows=4, cols=10, scrollback=20, record_events=True)
    term.feed(b"\x1b[?2048h0123456789abcdefgh\r\nhard")
    assert (term[0].text, term[0].wrapped) == ("0123456789", True)

    term.resize(rows=4, cols=20)
    assert (term[0].text, term[0].wrapped, term[1].text) == (
        "0123456789abcdefgh",
        False,
        "hard",
    )
    assert term.drain_replies() == b"\x1b[48;4;20;0;0t"
    assert InBandResize(rows=4, cols=20) in term.drain_events()

    term.feed(b"\x1b[?1049h\x1b[H0123456789abcdefgh")
    term.resize(rows=4, cols=10)
    assert (term[0].text, term[0].wrapped) == ("0123456789", False)


def test_row_wide_healing_decslrm_scroll_and_rle_history() -> None:
    """``SPACER_HEAD`` heals on edit, ``DECSLRM`` scrolls, and ``FrozenRow`` packs."""
    narrow_term = libvt.Terminal(rows=3, cols=4)
    narrow_term.feed("abc❤️".encode())
    assert narrow_term[0, 3].width_role == WidthRole.SPACER_HEAD
    narrow_term.feed(b"\x1b[2;1HZ")
    assert (narrow_term[0, 3].width_role, narrow_term[0].wrapped) == (
        WidthRole.NARROW,
        False,
    )
    assert (narrow_term[1, 0].text, narrow_term[1, 1].width_role) == (
        "Z",
        WidthRole.NARROW,
    )

    w2w = libvt.Terminal(rows=3, cols=4)
    w2w.feed_str("abc❤️\x1b[2;1H日")
    assert (w2w[0, 3].width_role, w2w[0].wrapped, w2w[1, 0].text) == (
        WidthRole.NARROW,
        False,
        "日",
    )

    # DECSLRM horizontal margin scroll_up / scroll_down + partial ECH text_used trim.
    scr_up = libvt.Terminal(rows=2, cols=6)
    scr_up.feed(b"\r\nCD\x1b[?69h\x1b[1;2s\x1b[1;1H\x1b[S")
    assert scr_up[0].text == "CD"

    scr_dn = libvt.Terminal(rows=2, cols=6)
    scr_dn.feed(b"AB\x1b[?69h\x1b[1;2s\x1b[1;1H\x1b[T")
    assert scr_dn[1].text == "AB"

    ech_t = libvt.Terminal(rows=2, cols=10)
    ech_t.feed(b"ABCD\x1b[3G\x1b[2X")
    assert ech_t[0].text == "AB"

    # Explicit spaces survive EL 0; RowView stays anchored; FrozenRow.used stays tight.
    term = libvt.Terminal(rows=3, cols=8, scrollback=20)
    term.feed(b"   \x1b[K")
    assert term.screen._rows[0].used == 3

    term.reset()
    term.feed("日本語\x1b[1;2H\x1b[1@".encode())
    _assert_grid_invariants(term)
    term.reset()
    term.feed("日本語\x1b[1;2H\x1b[2P".encode())
    _assert_grid_invariants(term)

    fr_t = libvt.Terminal(rows=2, cols=80, scrollback=8)
    top_view = fr_t[0]
    fr_t.feed(b"\x1b[31mA\x1b[0m\r\nB\r\nC")
    assert fr_t._screens.primary._history[0].used == 1
    assert top_view.text == "B"
    assert len(fr_t.history[0].runs) >= 1


def test_frozen_row_combining_wide_bce_and_256_block_cow_history() -> None:
    """``_Row.freeze()`` retains wide+combining+BCE cells and ``O(1)`` CoW history."""
    term = libvt.Terminal(rows=2, cols=8, scrollback=10)
    term.feed("e\u0301中\x1b[44m\x1b[K\r\n❤\ufe0f🇯🇵👋🏻Z\r\nv0\r\nv1".encode())
    snap = term.snapshot()
    assert isinstance(snap.history, collections.abc.Sequence)
    assert isinstance(term.history, collections.abc.Sequence)
    assert not isinstance(snap.history, tuple)

    h0 = snap.history[0]
    assert h0.text == "e\u0301中"
    assert (h0[0].text, h0[1].text, h0[1].width_role, h0[2].width_role) == (
        "e\u0301",
        "中",
        WidthRole.WIDE,
        WidthRole.SPACER_TAIL,
    )
    assert [r.text for r in h0.runs] == ["e\u0301中", "     "]
    assert h0.runs[1].style.bg == AnsiColor.BLUE
    assert (h0.runs[1].start_col, h0.runs[1].end_col) == (3, 8)

    h1 = snap.history[1]
    assert (h1[0].text, h1[2].text, h1[4].text, h1[6].text) == (
        "❤\ufe0f",
        "🇯🇵",
        "👋🏻",
        "Z",
    )

    bce_term = libvt.Terminal(rows=2, cols=8, scrollback=10)
    bce_term.feed("中\x1b[44m\x1b[K\r\n\n\n".encode())
    bce_runs = bce_term.snapshot().history[0].runs
    assert [(r.start_col, r.end_col, r.text, r.style.bg) for r in bce_runs] == [
        (0, 2, "中", None),
        (2, 8, "      ", AnsiColor.BLUE),
    ]

    ech_el = libvt.Terminal(rows=2, cols=10, scrollback=4)
    ech_el.feed(b"\x1b[41mAB\x1b[K\x1b[0m\x1b[3G\x1b[5X\x1b[8G\x1b[K\r\n\n\n")
    assert ech_el.snapshot().history[0]._row.used == 2
    assert [r.text for r in ech_el.snapshot().history[0].runs] == ["AB"]

    declrmm_sp = libvt.Terminal(rows=3, cols=6, scrollback=10)
    declrmm_sp.feed(
        b"\r\nA  \x1b[?69h\x1b[1;4s\x1b[1;1H\x1b[S\x1b[?69l\x1b[3;1H\r\n\r\n\r\n"
    )
    assert declrmm_sp.history[0].text == "A  "
    assert [r.text for r in declrmm_sp.history[0].runs] == ["A  "]
    declrmm_sp.resize(rows=3, cols=8)
    assert declrmm_sp.history[0].text == "A  "

    deep = libvt.Terminal(rows=2, cols=16, scrollback=50_000)
    deep.feed(b"".join(f"line-{i:05d}\r\n".encode() for i in range(50_002)))
    assert len(deep.history) == 50_000
    assert (deep.history[0].text, deep.history[-1].text) == (
        "line-00001",
        "line-50000",
    )
    assert deep.history[0] == deep.history[0]
    assert deep.history[255] in deep.history
    assert deep.history.index(deep.history[257]) == 257
    assert deep.history.count(deep.history[257]) == 1
    assert [r.text for r in deep.history[254:258]] == [
        "line-00255",
        "line-00256",
        "line-00257",
        "line-00258",
    ]
    assert [r.text for r in deep.history[-3:][::-1]] == [
        "line-50000",
        "line-49999",
        "line-49998",
    ]
    assert next(reversed(deep.history)).text == "line-50000"

    snap_a = deep.snapshot()
    tok = deep.sync_token()
    t0 = time.perf_counter()
    for step in range(50):
        deep.feed(f"tail-{step:02d}\r\n".encode())
        s_deep = deep.snapshot()
        delta = deep.changes_since(tok)
        tok = delta.token
        assert delta.full_resync is False
        assert len(delta.new_history) == 1
        assert s_deep.history[-1].text == (
            "line-50001" if step == 0 else f"tail-{step - 1:02d}"
        )
    assert time.perf_counter() - t0 < 0.05
    snap_b = deep.snapshot()
    assert snap_a._history.blocks[1] is snap_b._history.blocks[1]


def test_seeded_grid_and_reflow_invariants() -> None:
    """Seeded property fuzzing preserves wide-spacer and cursor bounds."""
    rng = random.Random(20261121)
    tokens: tuple[bytes, ...] = (
        b"abc",
        "日本".encode(),
        "🧑‍🌾".encode(),
        "🇯🇵".encode(),
        "❤\ufe0f".encode(),
        "a\u0301".encode(),
        b"\r",
        b"\n",
        b"\x08",
        b"\t",
        b"\x1b[A",
        b"\x1b[B",
        b"\x1b[C",
        b"\x1b[D",
        b"\x1b[2@",
        b"\x1b[2P",
        b"\x1b[2X",
        b"\x1b[K",
        b"\x1b[1K",
        b"\x1b[J",
        b"\x1b[L",
        b"\x1b[M",
        b"\x1b[2;5r",
        b"\x1b[r",
        b"\x1b[?69h\x1b[2;7s",
        b"\x1b[?69l",
        b"\x1b[?1049h",
        b"\x1b[?1049l",
    )

    for trial in range(80):
        term = libvt.Terminal(
            rows=rng.choice((4, 6, 8)),
            cols=rng.choice((6, 8, 12)),
            scrollback=30,
        )
        stream = b"".join(rng.choice(tokens) for _ in range(28))
        idx = 0
        while idx < len(stream):
            step = rng.randint(1, 11)
            term.feed(stream[idx : idx + step])
            idx += step
        _assert_grid_invariants(term)
        if trial % 4 == 0:
            term.resize(rows=rng.choice((3, 6, 9)), cols=rng.choice((5, 8, 14)))
            _assert_grid_invariants(term)
