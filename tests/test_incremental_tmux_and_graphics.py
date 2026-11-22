"""Regression and feature tests for incremental deltas, tmux capture, and graphics."""

from __future__ import annotations

import asyncio
import base64
import copy
import pickle
import shutil
import struct
import subprocess
import time
import traceback
import zlib

import pytest

import libvt
from libvt import _ucd
from libvt.aio import AsyncTerminalStream
from libvt.config import DEFAULT_PROFILE, TMUX_PROFILE, Profile
from libvt.events import KittyGraphic
from libvt.exc import TerminalTimeoutError
from libvt.graphics import GraphicsProtocol, ZLayer
from libvt.modes import Mode
from libvt.parser import ParserState
from libvt.screen import RowView
from libvt.style import AnsiColor
from libvt.terminal import ResyncReason
from libvt.tmux import (
    TMUX_METADATA_FORMAT,
    TmuxLineFlags,
    TmuxPaneMetadata,
    decode_tmux_capture,
    unescape_tmux_control_output,
)


class _SlottedTimeoutError(TerminalTimeoutError):
    """Downstream subclass with ``__slots__`` and custom keyword-only ``__init__``."""

    __slots__ = ("attempt",)

    def __init__(
        self,
        message: str,
        *,
        attempt: int,
        parser_state: ParserState = ParserState.GROUND,
        screen_text: str = "",
        recent_bytes: bytes | bytearray | memoryview = b"",
    ) -> None:
        super().__init__(
            message,
            parser_state=parser_state,
            screen_text=screen_text,
            recent_bytes=recent_bytes,
            eof=False,
        )
        self.attempt = attempt


class _DynamicTimeoutError(TerminalTimeoutError):
    """Downstream subclass allowing arbitrary instance attributes."""

    extra_context: str = ""


async def test_timeout_error_notes_slots_subclasses_and_concise_str() -> None:
    """``TerminalTimeoutError`` preserves ``__notes__``, slots, and 1-line ``str``."""
    empty = TerminalTimeoutError()
    assert empty.args == ()
    assert str(empty) == ""
    assert empty.errno is None

    quiet = TerminalTimeoutError(
        "quiet timeout",
        screen_text="secret-viewport",
        attach_note=False,
    )
    assert not hasattr(quiet, "__notes__")
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        quiet_rt = pickle.loads(pickle.dumps(quiet, protocol=proto))
        assert not hasattr(quiet_rt, "__notes__")
        assert quiet_rt.screen_text == "secret-viewport"

    slotted = _SlottedTimeoutError(
        "slotted timeout",
        attempt=4,
        parser_state=ParserState.CSI_PARAM,
        screen_text="row0",
        recent_bytes=b"\x1b[1;",
    )
    slotted.add_note("slot-note")
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        slotted_rt = pickle.loads(pickle.dumps(slotted, protocol=proto))
        assert type(slotted_rt) is _SlottedTimeoutError
        assert slotted_rt.attempt == 4
        assert slotted_rt.errno is None
        assert slotted_rt.__notes__ == slotted.__notes__
    for cloned_slot in (copy.copy(slotted), copy.deepcopy(slotted)):
        assert type(cloned_slot) is _SlottedTimeoutError
        assert cloned_slot.attempt == 4
        assert cloned_slot.__notes__ == slotted.__notes__

    sub = _DynamicTimeoutError(
        "timed out waiting for prompt",
        parser_state=ParserState.OSC_STRING,
        screen_text="$ stuck",
        recent_bytes=memoryview(b"\x1b]0;unterminated"),
        eof=False,
    )
    sub.add_note("attempt=3")
    sub.extra_context = "pane-%1"

    assert str(sub) == "timed out waiting for prompt"
    assert "\n" not in str(sub)
    assert isinstance(sub.recent_bytes, bytes)
    assert any("OSC_STRING" in n for n in sub.__notes__)
    assert "attempt=3" in sub.__notes__

    shallow = copy.copy(sub)
    shallow.add_note("shallow-only")
    assert "shallow-only" not in sub.__notes__

    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        restored = pickle.loads(pickle.dumps(sub, protocol=proto))
        assert type(restored) is _DynamicTimeoutError
        assert restored.errno is None
        assert str(restored) == "timed out waiting for prompt"
        assert restored.parser_state == ParserState.OSC_STRING
        assert restored.screen_text == "$ stuck"
        assert restored.recent_bytes == b"\x1b]0;unterminated"
        assert restored.eof is False
        assert restored.__notes__ == sub.__notes__
        assert restored.extra_context == "pane-%1"

    caught: list[TerminalTimeoutError] = []
    group_msg = "batch"
    other_msg = "other"
    try:
        raise ExceptionGroup(group_msg, [sub, ValueError(other_msg)])
    except* TimeoutError as eg:
        caught.extend(e for e in eg.exceptions if isinstance(e, TerminalTimeoutError))
    except* ValueError:
        pass
    assert len(caught) == 1
    assert caught[0].__notes__ == sub.__notes__

    reader = asyncio.StreamReader()
    stream = AsyncTerminalStream(reader, rows=2, cols=12)
    reader.feed_data(b"hello\x1b]0;stuck")
    with pytest.raises(TerminalTimeoutError) as exc_info:
        await stream.wait_for("missing", timeout=0.02)
    err = exc_info.value
    assert "\n" not in str(err)
    assert "Flight Recorder:" not in str(err)
    assert "Flight Recorder:" in err.format_diagnostics()
    assert any("Flight Recorder:" in n for n in err.__notes__)
    assert "Flight Recorder:" in "".join(traceback.format_exception(err))

    with pytest.raises(TerminalTimeoutError) as no_note_exc:
        await stream.wait_for("missing", timeout=0.01, attach_note=False)
    assert not hasattr(no_note_exc.value, "__notes__")


def test_right_margin_widening_zwsp_and_safe_narrow_bmp_slicing() -> None:
    """Right-margin ``VS16`` keeps SGR/margins, ``ZWSP`` breaks, ``?7l`` is fast."""
    for chunk_mode in ("whole", "per_char", "per_byte"):
        term = libvt.Terminal(rows=3, cols=4)
        payload = "\x1b[31mabc1\x1b[34m\ufe0f\u20e3Z"
        if chunk_mode == "whole":
            term.feed_str(payload)
        elif chunk_mode == "per_char":
            for ch in payload:
                term.feed_str(ch)
        else:
            raw = payload.encode("utf-8")
            for idx in range(len(raw)):
                term.feed(raw[idx : idx + 1])

        assert term[0].text == "abc"
        assert term[0].wrapped is True
        assert term[1, 0].text == "1\ufe0f\u20e3"
        assert term[1, 0].width == 2
        assert term[1, 0].style.fg == AnsiColor.RED
        assert term[1, 2].text == "Z"
        assert term[1, 2].style.fg == AnsiColor.BLUE

    # DECSTBM bottom < rows - 1 keeps wrapped=True after right-margin widening scroll.
    bounded = libvt.Terminal(rows=4, cols=4)
    bounded.feed_str("\x1b[1;2rL0\r\nabc1\ufe0f\u20e3Z")
    assert bounded[0].text == "abc"
    assert bounded[0].wrapped is True
    assert bounded[1, 0].text == "1\ufe0f\u20e3"
    assert bounded[1, 2].text == "Z"

    # Mode.IRM shifts existing text on the destination row when widening wraps.
    irm = libvt.Terminal(rows=3, cols=4)
    irm.feed_str("\x1b[2;1HXY\x1b[1;1H\x1b[4habc1\ufe0f\u20e3")
    assert irm[1, 0].text == "1\ufe0f\u20e3"
    assert irm[1, 2].text == "X"
    assert irm[1, 3].text == "Y"

    # U+200B ZERO WIDTH SPACE is GCB=Control (GB4/GB5) and must break the cluster.
    zwsp_term = libvt.Terminal(rows=2, cols=6)
    zwsp_term.feed_str("1\u200b\ufe0fA")
    assert zwsp_term[0, 0].text == "1"
    assert zwsp_term[0, 0].width == 1
    assert zwsp_term[0, 1].text == "A"

    matched_cps = [
        cp
        for cp in range(0x10000)
        if _ucd.SAFE_NARROW_BMP_RE.match(chr(cp)) is not None
    ]
    assert len(matched_cps) >= 18_000
    for sample in ("┌", "─", "│", "█", "Ω", "д", "→", "⠿"):
        assert _ucd.SAFE_NARROW_BMP_RE.match(sample) is not None
    for unsafe in (
        "\u0301",
        "\ufe0f",
        "\u200d",
        "中",
        "\u202e",
        "\x00",
        "\x7f",
        "\x85",
    ):
        assert _ucd.SAFE_NARROW_BMP_RE.match(unsafe) is None

    mixed = "┌── Ωμέγα 🚀 Кириллица ──┐#1\ufe0f\u20e3*2\ufe0f"
    bulk = libvt.Terminal(rows=4, cols=16)
    bulk.feed_str(mixed)
    scalar = libvt.Terminal(rows=4, cols=16)
    for ch in mixed:
        scalar.feed_str(ch)
    assert bulk.lines == scalar.lines
    assert bulk.cursor == scalar.cursor

    # ?7l (DECAWM disabled) collapses right-margin slice overwrites in O(cols) time.
    no_wrap = libvt.Terminal(rows=2, cols=8)
    t0 = time.monotonic()
    no_wrap.feed_str("\x1b[?7l" + ("ABCDEFGH" * 12_500) + "!")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2
    assert no_wrap[0].text == "ABCDEFG!"


def test_tmux_profile_decstr_ed2_history_versions_and_linux_console() -> None:
    """``Profile.tmux`` handles ``next-3.9``/``rc``, ``DECSTR``, and ``ED 2``."""
    p_next = Profile.tmux("next-3.9")
    assert p_next.sixel_erase_policy == "cell_mask"
    p_rc = Profile.tmux("tmux 3.8-rc3")
    assert p_rc.sixel_erase_policy == "whole_image"
    p_prefixed = Profile.tmux("tmux 3.8a")
    assert p_prefixed.sixel_erase_policy == "whole_image"

    lt = libvt.Terminal(rows=2, cols=8, profile=Profile.linux_console())
    lt.feed(b"\x0elqqk\x0f")
    assert lt[0].text == "┌──┐"

    tm = libvt.Terminal(rows=4, cols=10, scrollback=20, profile=TMUX_PROFILE)
    tm.feed(b"\x1b[31m\x1b[?7l\x1b[!pA")
    assert tm.modes.is_set(Mode.DECAWM) is False
    assert tm[0, 0].style.fg == AnsiColor.RED

    vt = libvt.Terminal(rows=4, cols=10, profile=DEFAULT_PROFILE)
    vt.feed(b"\x1b[31m\x1b[?7l\x1b[!pA")
    assert vt.modes.is_set(Mode.DECAWM) is True
    assert vt[0, 0].style.fg is None

    tm2 = libvt.Terminal(rows=4, cols=10, scrollback=20, profile=TMUX_PROFILE)
    tm2.feed(b"ROW_ONE\r\nROW_TWO\x1b[4;5H\x1b[44m\x1b[2J")
    assert [r.text for r in tm2.history] == ["ROW_ONE", "ROW_TWO"]
    assert tm2.lines == ("", "", "", "")
    assert tm2[0, 0].style.bg == AnsiColor.BLUE
    assert (tm2.cursor.row, tm2.cursor.col) == (3, 4)

    # ED 0 at (0, 0) scrolls primary content (including BCE-colored rows) into history.
    tm2.feed(b"\x1b[0m\x1b[HHOME_ROW\x1b[H\x1b[J")
    assert [r.text for r in tm2.history] == [
        "ROW_ONE",
        "ROW_TWO",
        "HOME_ROW",
        "",
        "",
        "",
    ]
    assert tm2.history[3][0].style.bg == AnsiColor.BLUE

    tm2.feed(b"\x1b[?1049hALT_LINE\x1b[2J")
    assert len(tm2.history) == 6
    assert tm2.lines == ("", "", "", "")


def test_tmux_capture_decoder_synthetic_and_live() -> None:
    """``decode_tmux_capture`` decodes ``list[str]``, ``-J``, and live ``tmux``."""
    assert unescape_tmux_control_output(r"\033[31mhi\134there\015\012") == (
        b"\x1b[31mhi\\there\r\n"
    )
    raw_utf8_line = b"caf\xc3\xa9\\012".decode("utf-8", errors="surrogateescape")
    assert unescape_tmux_control_output(raw_utf8_line) == b"caf\xc3\xa9\n"

    # Format string uses real tmux format names and parses UINT_MAX saved coords.
    assert "#{bracket_paste_flag}" in TMUX_METADATA_FORMAT
    assert "#{mouse_all_flag}" in TMUX_METADATA_FORMAT
    assert "#{pane_private_modes}" in TMUX_METADATA_FORMAT
    raw_fmt = (
        "8\t3\t0\t2\t1\t0\t4294967295\t4294967295\t0\t2\t"
        "0\t0\t1\t0\t1\t0\t0\t1\t1\t1004,2004\t"
        "0,4\t0\t2000\t1\t7\t/tmp\ttitle"
    )
    parsed_meta = TmuxPaneMetadata.from_format_line(raw_fmt)
    assert parsed_meta.alternate_saved_x is None
    assert parsed_meta.alternate_saved_y is None
    assert parsed_meta.bracketed_paste_flag is True
    assert parsed_meta.mouse_any_flag is True
    assert parsed_meta.focus_events_flag is True
    assert parsed_meta.synchronized is True

    # Sequence[str] input (libtmux capture_pane() shape) + ?2026 synchronized hold.
    sync_term = libvt.Terminal.from_tmux_capture(
        ["HOLD_LIN", "", ""],
        metadata=raw_fmt,
    )
    assert sync_term.synchronized is True
    sync_term.feed(b"\x1b[1;1HDIRTY")
    assert sync_term.committed[0].text == "HOLD_LIN"
    sync_term.feed(b"\x1b[?2026l")
    assert sync_term.committed[0].text == "DIRTYLIN"

    # Unflagged capture + joined_capture infers 3-row soft wrap + CJK SPACER_HEAD wrap,
    # and flagged P/O lines keep PromptZone rows accurate when scrollback < hist_count.
    unflagged_lines = ["H0", "H1", "ABCD", "EFGH", "IJ", "abc ", "中文"]
    joined_lines = ["H0", "H1", "ABCDEFGHIJ", "abc中文"]
    cjk_meta = TmuxPaneMetadata(cols=4, rows=5, history_size=2)
    cjk_term = libvt.Terminal.from_tmux_capture(
        unflagged_lines,
        metadata=cjk_meta,
        scrollback=1,
        joined_capture=joined_lines,
    )
    assert [r.text for r in cjk_term.history] == ["H1"]
    assert cjk_term[0].wrapped is True
    assert cjk_term[1].wrapped is True
    assert cjk_term[2].wrapped is False
    assert cjk_term[3].wrapped is True
    cjk_term.resize(rows=5, cols=10)
    assert cjk_term.lines[:3] == ("H1", "ABCDEFGHIJ", "abc中文")

    pz_snap = libvt.Snapshot.from_tmux_capture(
        ["P H0", "P H1", "- V0", "O V1", "- V2", "- V3", "- V4"],
        metadata=cjk_meta,
        scrollback=1,
        flagged=True,
    )
    assert [r.text for r in pz_snap.history] == ["H1"]
    assert [(z.row, z.kind.value) for z in pz_snap.prompt_zones] == [
        (-1, "A"),
        (1, "C"),
    ]

    # Synthetic flagged (-F -e -p -N) capture with scrollback, bare SO/SI ACS,
    # SGR carry across rows, custom tabstops, soft wrap, and pending_wrap.
    meta = TmuxPaneMetadata(
        cols=8,
        rows=3,
        cursor_x=8,
        cursor_y=2,
        wrap_flag=True,
        pane_tabs=(4, 8),
        output_generation=42,
    )
    captured = (
        "- HIST_0  \n"
        "X \x1b[31m\x0elqqk\x0f    \n"
        "WX ABCDEFGH\n"
        "X \x1b[44m\t\x1b[49m1234\n"
    )
    res = decode_tmux_capture(
        captured,
        metadata=meta,
        post_metadata=meta,
        flagged=True,
    )
    assert res.completeness.exact_wraps is True
    assert res.completeness.exact_generation_bracket is True
    assert TmuxLineFlags.WRAPPED in res.line_flags[2]
    assert [r.text for r in res.snapshot.history] == ["HIST_0"]
    assert res.terminal[0].text == "┌──┐"
    assert res.terminal[0, 0].style.fg == AnsiColor.RED
    assert res.terminal[1].text == "ABCDEFGH"
    assert res.terminal[1].wrapped is True
    assert res.terminal[1, 0].style.fg == AnsiColor.RED
    assert res.terminal[2].text == "    1234"
    assert res.terminal[2, 0].style.bg == AnsiColor.BLUE
    assert res.terminal[2, 3].style.bg == AnsiColor.BLUE
    assert res.terminal[2, 4].style.bg is None
    assert res.terminal.cursor.row == 2
    assert res.terminal.cursor.col == 7
    assert res.terminal.cursor.pending_wrap is True
    res.terminal.feed(b"\x1b[0m!\x0el\x0f")
    assert res.terminal[2].text == "!l"

    if shutil.which("tmux") is not None:
        sock = f"libvt-test-{zlib.crc32(b'incremental-tmux')}"
        try:
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    sock,
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-x",
                    "10",
                    "-y",
                    "3",
                    "sh",
                    "-c",
                    (
                        "printf '\\033[?2004h\\033(0lqqk\\033(B\\r\\n"
                        "1234567890WRAP\\r\\n\\t\\033[42mX\\033[0m'; sleep 30"
                    ),
                ],
                check=True,
            )
            for _ in range(100):
                chk = subprocess.run(
                    ["tmux", "-L", sock, "capture-pane", "-p"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if "X" in chk.stdout:
                    break
                time.sleep(0.02)
            else:
                pytest.fail("timed out waiting for tmux pane output")
            meta_raw = subprocess.run(
                [
                    "tmux",
                    "-L",
                    sock,
                    "display-message",
                    "-p",
                    TMUX_METADATA_FORMAT,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.rstrip("\n")
            cap_raw = subprocess.run(
                [
                    "tmux",
                    "-L",
                    sock,
                    "capture-pane",
                    "-S",
                    "-",
                    "-E",
                    "-",
                    "-F",
                    "-e",
                    "-p",
                    "-N",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            live_res = decode_tmux_capture(cap_raw, metadata=meta_raw, flagged=True)
            assert live_res.terminal.modes.is_set(Mode.BRACKETED_PASTE) is True
            assert [r.text for r in live_res.snapshot.history] == ["┌──┐"]
            assert live_res.terminal[0].text == "1234567890"
            assert live_res.terminal[0].wrapped is True
            assert live_res.terminal[1].text == "WRAP"
            assert live_res.terminal[2].text == "        X"
            assert live_res.terminal[2, 0].style.bg is None
            assert live_res.terminal[2, 8].style.bg == AnsiColor.GREEN
        finally:
            subprocess.run(
                ["tmux", "-L", sock, "kill-server"],
                check=False,
                capture_output=True,
            )


def test_changes_since_watermarks_and_replica_equivalence() -> None:
    """``changes_since`` and ``apply_lines``/``apply_rows`` stay exact."""
    term = libvt.Terminal(
        rows=4,
        cols=6,
        scrollback=6,
        profile=TMUX_PROFILE,
    )
    rep_lines_hist: list[str] = []
    rep_lines_view: list[str] = []
    rep_rows_hist: list[RowView] = []
    rep_rows_view: list[RowView] = []

    def _step_and_assert(tok: libvt.SyncToken | None) -> libvt.ScreenDelta:
        delta = term.changes_since(tok)
        delta.apply_lines(rep_lines_hist, rep_lines_view)
        delta.apply_rows(rep_rows_hist, rep_rows_view)
        snap = delta.snapshot
        assert rep_lines_hist == [r.text for r in snap.history]
        assert rep_lines_view == list(snap.lines)
        assert [r.text for r in rep_rows_hist] == [r.text for r in snap.history]
        assert [(r.text, r.wrapped) for r in rep_rows_view] == [
            (snap[i].text, snap[i].wrapped) for i in range(snap.rows)
        ]
        return delta

    d = _step_and_assert(None)
    assert d.resync_reason == ResyncReason.INITIAL

    # Wide SPACER_HEAD wrap + cross-row heal + DCH at col 0 clearing preceding wrapped.
    term.feed_str("abcde中\r\nLINE_2")
    d = _step_and_assert(d.token)
    assert rep_rows_view[0].wrapped is True
    term.feed_str("\x1b[2;1H\x1b[P")
    d = _step_and_assert(d.token)
    assert rep_rows_view[0].wrapped is False

    # Full-screen scroll + large multi-row scroll exceeding viewport height (> 4 rows).
    term.feed_str("\x1b[4;1H\r\nS1\r\nS2\r\nS3\r\nS4\r\nS5")
    d = _step_and_assert(d.token)
    assert d.resync_reason == ResyncReason.NONE
    assert d.viewport_scroll_up == 5

    # Bounded DECSTBM top=0, bottom=2 (rows=4) pushes to history while pinning row 3.
    term.feed_str("\x1b[1;3r\x1b[3;1H\nTOP_SC\x1b[r")
    d = _step_and_assert(d.token)
    assert d.resync_reason == ResyncReason.NONE

    # ED 3 clears scrollback and subsequent scroll stays incremental.
    term.feed(b"\x1b[3J\x1b[4;1H\r\nAFTER_CLEAR")
    d = _step_and_assert(d.token)
    assert d.history_cleared is True

    # Auxiliary metadata mutations (OSC 7501, remote OSC 7 URI, ESC k) flag changes.
    term.feed(b"\x1b]7501;state=working:id=build\x07")
    d = _step_and_assert(d.token)
    assert d.metadata_changed is True

    term.feed(b"\x1b]7;file://remote-box.example/srv/app\x07")
    d = _step_and_assert(d.token)
    assert d.metadata_changed is True
    assert d.snapshot.cwd == ""
    assert d.snapshot.cwd_uri == "file://remote-box.example/srv/app"

    term.feed(b"\x1bkmy-window\x1b\\")
    d = _step_and_assert(d.token)
    assert d.metadata_changed is True

    # ?2026 synchronized output holds committed view while working view advances.
    c_tok = term.sync_token(view="committed")
    term.feed(b"\x1b[?2026h\x1b[1;1HTORN")
    assert term.changes_since(c_tok, view="committed").has_changes is False
    assert 0 in term.changes_since(c_tok, view="working").dirty_rows
    term.feed(b"\x1b[?2026l")
    d = _step_and_assert(c_tok)
    assert d.has_changes is True

    # ?1049h / ?1049l bumps epoch; scrolling > scrollback triggers HISTORY_EVICTED.
    stale_pre_alt = d.token
    term.feed(b"\x1b[?1049hALT\x1b[?1049l")
    d = _step_and_assert(stale_pre_alt)
    assert d.resync_reason == ResyncReason.EPOCH_CHANGED

    stale_same_epoch = d.token
    f_tok = d.token
    for idx in range(10):
        term.feed(f"\r\nEV{idx}".encode("ascii"))
        f_tok = term.changes_since(f_tok).token
    assert term.changes_since(stale_same_epoch).resync_reason == (
        ResyncReason.HISTORY_EVICTED
    )


def test_sixel_and_kitty_graphics_and_snapshot_immutability() -> None:
    """``SIXEL``/Kitty handle scroll-before-place, limits, and frozen ``Snapshot``."""
    ihdr_body = struct.pack(">IIBBBBB", 16, 32, 8, 6, 0, 0, 0)
    ihdr_crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_body) & 0xFFFFFFFF)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr_body + ihdr_crc
    )
    z_payload = base64.b64encode(zlib.compress(png_bytes))
    part1, part2 = z_payload[:12], z_payload[12:]

    term = libvt.Terminal(rows=6, cols=10, scrollback=10)
    term.feed(b"\x1b[2;3H")
    term.feed(b"\x1b_Ga=T,f=100,o=z,i=7,p=1,z=-5,m=1;" + part1 + b"\x1b\\")
    assert len(term.placements) == 0
    term.feed(b"\x1b_Gm=0;" + part2 + b"\x1b\\")
    assert b"\x1b_Gi=7,p=1;OK\x1b\\" in term.drain_replies()

    assert len(term.placements) == 1
    kp = term.placements[0]
    assert kp.protocol == GraphicsProtocol.KITTY
    assert (kp.row, kp.col, kp.rows, kp.cols) == (1, 2, 2, 2)
    assert kp.layer == ZLayer.BELOW_TEXT
    assert (term.cursor.row, term.cursor.col) == (2, 4)

    # Kitty U=0 overlay survives text overwrite; d=y with missing y=0 is a safe no-op.
    term.feed(b"\x1b[2;3HOVER\x1b[K\x1b_Ga=d,d=y\x1b\\")
    assert len(term.placements) == 1

    # Retransmitting i=7 replaces stale placements of image 7.
    raw_1cell = base64.b64encode(b"\xff" * (8 * 16 * 3))
    term.feed(b"\x1b[1;1H\x1b_Ga=t,f=24,s=8,v=16,i=7;" + raw_1cell + b"\x1b\\")
    assert len(term.placements) == 0
    assert b"\x1b_Gi=7;OK\x1b\\" in term.drain_replies()

    # Unnumbered Kitty a=T upload places image silently (zero PTY reply bytes).
    term.feed(b"\x1b[1;1H\x1b_Ga=T,f=24,s=8,v=16;" + raw_1cell + b"\x1b\\")
    assert term.drain_replies() == b""
    assert len(term.placements) == 1

    # Query a=q without i= stays silent; i + I together and truncated payloads fail.
    term.feed(b"\x1b_Ga=q,f=24,s=1,v=1;" + base64.b64encode(b"abc") + b"\x1b\\")
    assert term.drain_replies() == b""
    term.feed(b"\x1b_Ga=t,i=2,I=3,f=24,s=1,v=1;" + base64.b64encode(b"abc") + b"\x1b\\")
    assert b"EINVAL" in term.drain_replies()
    term.feed(b"\x1b_Ga=t,i=8,f=24,s=8,v=16;" + base64.b64encode(b"short") + b"\x1b\\")
    assert b"EINVAL" in term.drain_replies()
    assert 8 not in term.images
    comp_trunc = base64.b64encode(zlib.compress(png_bytes)[:-4])
    term.feed(b"\x1b_Ga=t,i=9,f=100,o=z;" + comp_trunc + b"\x1b\\")
    assert b"EINVAL" in term.drain_replies()
    assert 9 not in term.images

    # Reject local filesystem t=f safely in sans-I/O emulator.
    term.feed(b"\x1b_Ga=T,t=f,i=99;" + base64.b64encode(b"/etc/passwd") + b"\x1b\\")
    assert 99 not in term.images
    assert b"EINVAL" in term.drain_replies()

    # Virtual parent + relative child placement + cascade delete.
    term.feed(b"\x1b_Ga=T,U=1,i=20,p=1,c=2,r=2,f=24,s=8,v=16;" + raw_1cell + b"\x1b\\")
    # Virtual parent cannot anchor non-virtual relative child; real parent can:
    term.feed(b"\x1b_Ga=p,i=20,p=2,P=20,Q=1\x1b\\")
    assert b"ENOPARENT" in term.drain_replies()
    term.drain_replies()
    term.feed(b"\x1b[2;2H\x1b_Ga=p,i=20,p=3,c=2,r=2\x1b\\")
    term.feed(b"\x1b_Ga=p,i=20,p=4,P=20,Q=3,H=2,V=1\x1b\\")
    term.drain_replies()
    child_pls = [p for p in term.placements if p.image_id == 20 and p.placement_id == 4]
    assert len(child_pls) == 1
    assert (child_pls[0].row, child_pls[0].col) == (2, 3)
    term.feed(b"\x1b_Ga=d,d=i,i=20,p=3\x1b\\")
    assert not any(
        p.image_id == 20 and p.placement_id in (3, 4) for p in term.placements
    )

    # Bottom-of-alt-screen Kitty & SIXEL scroll-before-place keep full 2-row graphic.
    alt_t = libvt.Terminal(rows=4, cols=8)
    raw_2row = base64.b64encode(b"\xaa" * (16 * 32 * 3))
    alt_t.feed(b"\x1b[?1049h\x1b[4;2H\x1b_Ga=T,f=24,s=16,v=32;" + raw_2row + b"\x1b\\")
    assert len(alt_t.placements) == 1
    assert (alt_t.placements[0].row, alt_t.placements[0].rows) == (2, 2)

    sixel_seq = b'\x1bP0;1;0q"1;1;16;32#0!16~-#0!16~-#0!16~-#0!16~-#0!16~\x1b\\'
    vt_sixel = libvt.Terminal(rows=6, cols=10, profile=Profile.tmux("next-3.9"))
    vt_sixel.feed(sixel_seq)
    assert len(vt_sixel.placements) == 1
    sp = vt_sixel.placements[0]
    assert sp.protocol == GraphicsProtocol.SIXEL
    assert (sp.row, sp.col, sp.rows, sp.cols) == (0, 0, 2, 2)
    assert (vt_sixel.cursor.row, vt_sixel.cursor.col) == (2, 0)
    vt_sixel.feed(b"\x1b[1;1HX")
    assert vt_sixel.placements[0].occupies_cell(0, 0) is False
    assert vt_sixel.placements[0].occupies_cell(0, 1) is True

    tm38_sixel = libvt.Terminal(rows=6, cols=10, profile=Profile.tmux("3.8a"))
    tm38_sixel.feed(sixel_seq)
    tm38_sixel.feed(b"\x1b[1;1HX")
    assert len(tm38_sixel.placements) == 0

    # 10,000px SIXEL stream under DECLRMM finishes in O(rows) without per-row DoS loop.
    dos_t = libvt.Terminal(rows=24, cols=80, record_events=True)
    t0 = time.monotonic()
    for _ in range(20):
        dos_t.feed(b'\x1b[?69h\x1b[10;20s\x1b[1;1H\x1bPq"1;1;10;10000#0~\x1b\\')
    assert time.monotonic() - t0 < 0.2

    # Snapshot and KittyCommand.control enforce immutability at runtime.
    ev_t = libvt.Terminal(rows=4, cols=8, record_events=True)
    ev_t.feed(b"\x1b_Ga=T,f=24,s=8,v=16,i=5;" + raw_1cell + b"\x1b\\")
    evs = [e for e in ev_t.drain_events() if isinstance(e, KittyGraphic)]
    assert len(evs) == 1
    with pytest.raises(TypeError):
        evs[0].command.control["a"] = "d"  # type: ignore[index]

    snap = ev_t.snapshot()
    assert len(snap.to_dict()["placements"]) == 1
    with pytest.raises(AttributeError):
        snap.title = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        snap.program_statuses["x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        snap.images[999] = None  # type: ignore[index]
