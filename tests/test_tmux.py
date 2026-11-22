"""Tests for ``libvt.tmux`` ``capture-pane`` reconstruction and ``-CC`` unescape."""

from __future__ import annotations

import shutil
import subprocess
import time
import zlib

import pytest

import libvt
from libvt.modes import Mode
from libvt.style import AnsiColor
from libvt.tmux import (
    TMUX_METADATA_FORMAT,
    TmuxLineFlags,
    TmuxPaneMetadata,
    decode_tmux_capture,
    unescape_tmux_control_output,
)


def test_control_mode_unescape_and_metadata_format_parsing() -> None:
    """``unescape_tmux_control_output`` and ``TmuxPaneMetadata`` handle tmux quirks."""
    assert unescape_tmux_control_output(r"\033[31mhi\134there\015\012") == (
        b"\x1b[31mhi\\there\r\n"
    )
    raw_utf8_line = b"caf\xc3\xa9\\012".decode("utf-8", errors="surrogateescape")
    assert unescape_tmux_control_output(raw_utf8_line) == b"caf\xc3\xa9\n"

    assert "#{bracket_paste_flag}" in TMUX_METADATA_FORMAT
    assert "#{mouse_all_flag}" in TMUX_METADATA_FORMAT
    assert "#{pane_private_modes}" in TMUX_METADATA_FORMAT
    assert "#{synchronized_output_flag}" in TMUX_METADATA_FORMAT
    raw_fmt = (
        "8\t3\t0\t2\t1\t0\t4294967295\t4294967295\t0\t2\t"
        "0\t0\t1\t0\t1\t0\t0\t1\t1\t1004,2004\t"
        "0,4\t0\t2000\t1\t7\t/tmp\ttitle"
    )
    parsed_meta = TmuxPaneMetadata.from_format_line(raw_fmt)
    assert (parsed_meta.alternate_saved_x, parsed_meta.alternate_saved_y) == (
        None,
        None,
    )
    assert (
        parsed_meta.bracketed_paste_flag,
        parsed_meta.mouse_any_flag,
        parsed_meta.focus_events_flag,
        parsed_meta.synchronized,
    ) == (True, True, True, True)

    sync_term = libvt.Terminal.from_tmux_capture(["HOLD_LIN", "", ""], metadata=raw_fmt)
    sync_term.feed(b"\x1b[1;1HDIRTY")
    assert sync_term.committed[0].text == "HOLD_LIN"
    sync_term.feed(b"\x1b[?2026l")
    assert sync_term.committed[0].text == "DIRTYLIN"


def test_capture_decoder_acs_tabs_joined_wraps_and_prompt_zones() -> None:
    """``decode_tmux_capture`` reconstructs ACS, tabs, ``-J`` wraps, and zones."""
    cjk_meta = TmuxPaneMetadata(cols=4, rows=5, history_size=2)
    cjk_term = libvt.Terminal.from_tmux_capture(
        ["H0", "H1", "ABCD", "EFGH", "IJ", "abc ", "中文"],
        metadata=cjk_meta,
        scrollback=1,
        joined_capture=["H0", "H1", "ABCDEFGHIJ", "abc中文"],
    )
    assert [r.text for r in cjk_term.history] == ["H1"]
    assert (cjk_term[0].wrapped, cjk_term[1].wrapped, cjk_term[3].wrapped) == (
        True,
        True,
        True,
    )
    cjk_term.resize(rows=5, cols=10)
    assert cjk_term.lines[:3] == ("H1", "ABCDEFGHIJ", "abc中文")

    pz_snap = libvt.Snapshot.from_tmux_capture(
        ["P H0", "P H1", "- V0", "O V1", "- V2", "- V3", "- V4"],
        metadata=cjk_meta,
        scrollback=1,
        flagged=True,
    )
    assert [(z.row, z.kind.value) for z in pz_snap.prompt_zones] == [
        (-1, "A"),
        (1, "C"),
    ]

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
    assert (
        res.completeness.exact_wraps,
        res.completeness.exact_generation_bracket,
    ) == (
        True,
        True,
    )
    assert TmuxLineFlags.WRAPPED in res.line_flags[2]
    assert [r.text for r in res.snapshot.history] == ["HIST_0"]
    assert (res.terminal[0].text, res.terminal[0, 0].style.fg) == (
        "┌──┐",
        AnsiColor.RED,
    )
    assert (res.terminal[1].wrapped, res.terminal[1, 0].style.fg) == (
        True,
        AnsiColor.RED,
    )
    assert (
        res.terminal[2].text,
        res.terminal[2, 0].style.bg,
        res.terminal[2, 4].style.bg,
    ) == ("    1234", AnsiColor.BLUE, None)
    assert (
        res.terminal.cursor.row,
        res.terminal.cursor.col,
        res.terminal.cursor.pending_wrap,
    ) == (2, 7, True)
    res.terminal.feed(b"\x1b[0m!\x0el\x0f")
    assert res.terminal[2].text == "!l"


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux binary not installed")
def test_live_tmux_capture_pane_roundtrip() -> None:
    """Live ``tmux capture-pane`` reconstructs ACS, wraps, tabs, and modes."""
    sock = f"libvt-test-{zlib.crc32(b'tmux-capture')}"
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
                    "printf '\\033[?1000h\\033(0lqqk\\033(B\\r\\n"
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
            time.sleep(0.01)
        else:
            pytest.fail("timed out waiting for tmux pane output")
        meta_raw = subprocess.run(
            ["tmux", "-L", sock, "display-message", "-p", TMUX_METADATA_FORMAT],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.rstrip("\n")
        flagged_proc = subprocess.run(
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
            check=False,
            capture_output=True,
            text=True,
        )
        if flagged_proc.returncode == 0:
            live_res = decode_tmux_capture(
                flagged_proc.stdout,
                metadata=meta_raw,
                flagged=True,
            )
        else:
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
                    "-e",
                    "-p",
                    "-N",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            joined_raw = subprocess.run(
                ["tmux", "-L", sock, "capture-pane", "-S", "-", "-E", "-", "-p", "-J"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            live_res = decode_tmux_capture(
                cap_raw,
                metadata=meta_raw,
                flagged=False,
                joined_capture=joined_raw,
            )
        assert live_res.terminal.modes.is_set(Mode.MOUSE_VT200) is True
        assert [r.text for r in live_res.snapshot.history] == ["┌──┐"]
        assert (live_res.terminal[0].text, live_res.terminal[0].wrapped) == (
            "1234567890",
            True,
        )
        assert (live_res.terminal[1].text, live_res.terminal[2].text) == (
            "WRAP",
            "        X",
        )
        assert live_res.terminal[2, 8].style.bg == AnsiColor.GREEN
    finally:
        subprocess.run(
            ["tmux", "-L", sock, "kill-server"],
            check=False,
            capture_output=True,
        )
