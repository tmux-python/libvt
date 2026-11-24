"""Tests for ``libvt.tmux`` ``capture-pane`` reconstruction and ``-CC`` unescape."""

from __future__ import annotations

import shutil
import subprocess
import time
import zlib

import pytest

import libvt
from libvt.modes import Mode
from libvt.style import AnsiColor, IndexedColor
from libvt.tmux import (
    TMUX_METADATA_FORMAT,
    TmuxLineFlags,
    TmuxPaneMetadata,
    build_tmux_capture_command,
    decode_tmux_capture,
    parse_flagged_line,
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
    cjk_meta = TmuxPaneMetadata(cols=4, rows=6, history_size=2)
    cjk_term = libvt.Terminal.from_tmux_capture(
        [
            "\x0elqqk\x0f",
            "\x1b[5:3mH1\x1b[0m",
            "ABCD",
            "EFGH",
            "ab  ",
            "中文",
            "abc ",
            "中文",
        ],
        metadata=cjk_meta,
        scrollback=2,
        joined_capture=[
            "\x0elqqk\x0f",
            "\x1b[5:3mH1\x1b[0m",
            "ABCDEFGH",
            "ab 中文",
            "abc 中文",
        ],
    )
    assert [r.text for r in cjk_term.history] == ["┌──┐", "H1"]
    assert cjk_term.history[1][0].style.overline is True
    assert cjk_term.history[1][0].style.blink is False
    assert (cjk_term[0].wrapped, cjk_term[2].wrapped, cjk_term[4].wrapped) == (
        True,
        True,
        True,
    )
    cjk_term.resize(rows=6, cols=10)
    assert cjk_term.lines == ("┌──┐", "H1", "ABCDEFGH", "ab 中文", "abc 中文", "")

    stripped_res = decode_tmux_capture(
        ["H0", "\tX"],
        metadata=TmuxPaneMetadata(cols=4, rows=3, history_size=1),
        joined_capture=["H0", "\tX"],
    )
    assert stripped_res.completeness.exact_wraps is True
    assert [r.text for r in stripped_res.snapshot.history] == ["H0"]
    assert stripped_res.snapshot.lines == ("   X", "", "")

    pz_snap = libvt.Snapshot.from_tmux_capture(
        ["P H0", "P H1", "- V0", "O V1", "- V2", "- V3", "- V4"],
        metadata=TmuxPaneMetadata(cols=4, rows=5, history_size=2),
        scrollback=1,
        flagged=True,
    )
    assert [
        (z.abs_row, pz_snap.viewport_row(z.abs_row), z.kind.value)
        for z in pz_snap.prompt_zones
    ] == [
        (1, None, "A"),
        (3, 1, "C"),
    ]
    assert pz_snap.at_abs(pz_snap.prompt_zones[0].abs_row).text == "H1"
    assert pz_snap.at_abs(pz_snap.prompt_zones[1].abs_row).text == "V1"
    with pytest.raises(IndexError):
        pz_snap.at_abs(0)

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
                "-J",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        candidates = [
            decode_tmux_capture(
                cap_raw,
                metadata=meta_raw,
                flagged=False,
                joined_capture=joined_raw,
            ),
        ]
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
            candidates.append(
                decode_tmux_capture(
                    flagged_proc.stdout,
                    metadata=meta_raw,
                    flagged=True,
                ),
            )
        for live_res in candidates:
            assert live_res.completeness.exact_wraps is True
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


def test_capture_overline_scoping_ntf_wraps_and_command_bundle() -> None:
    """``5:3`` is capture-only and ``-N -T -F`` / command bundles disambiguate wraps."""
    sgr_res = decode_tmux_capture(
        ["\x1b[0;1;5:3;38;5;3mA\x1b[0;38:5:3mB"],
        rows=1,
        cols=4,
        trim_trailing=True,
    )
    assert sgr_res.terminal[0, 0].style.overline is True
    assert sgr_res.terminal[0, 0].style.bold is True
    assert sgr_res.terminal[0, 0].style.fg == IndexedColor(3)
    assert sgr_res.terminal[0, 1].style.overline is False
    assert sgr_res.terminal[0, 1].style.blink is False
    assert sgr_res.terminal[0, 1].style.fg == IndexedColor(3)
    sgr_res.terminal.feed(b"\x1b[0m\x1b[5:3mZ")
    assert sgr_res.terminal[0, 0].style.overline is False
    assert sgr_res.terminal[0, 0].style.blink is False

    trunc_rgb = decode_tmux_capture(
        ["\x1b[38;2;1;5:3mA\x1b[999b\x1b[?1049h"],
        rows=1,
        cols=6,
    )
    assert trunc_rgb.terminal[0, 0].style.bold is True
    assert trunc_rgb.terminal[0, 0].style.overline is True
    assert trunc_rgb.terminal[0].text == "A"
    assert trunc_rgb.snapshot.active_is_alt is False

    meta4 = TmuxPaneMetadata(cols=4, rows=8)
    ntf_term = libvt.Terminal.from_tmux_capture(
        [
            "W abc",
            "X 中文",
            "W abc ",
            "X 中文",
            "W ab ",
            "X 中文",
            "- hi  ",
            "- ",
        ],
        metadata=meta4,
        flagged=True,
        trim_trailing=True,
    )
    ntf_term.resize(rows=8, cols=8)
    assert ntf_term.lines[:4] == ("abc中文", "abc 中文", "ab 中文", "hi  ")

    trail_term = libvt.Terminal.from_tmux_capture(
        ["- hi  ", "- "],
        metadata=TmuxPaneMetadata(cols=4, rows=2),
        flagged=True,
        trim_trailing=True,
    )
    trail_term.resize(rows=2, cols=3)
    assert (trail_term[0].text, trail_term[0].wrapped) == ("hi ", True)

    nt_joined = libvt.Terminal.from_tmux_capture(
        ["abcdefghi", "中      X"],
        metadata=TmuxPaneMetadata(cols=10, rows=2),
        trim_trailing=True,
        joined_capture=["abcdefghi中\tX"],
    )
    nt_joined.resize(rows=2, cols=20)
    assert nt_joined.lines == ("abcdefghi中      X", "")

    cmd_38 = build_tmux_capture_command("sess;", tmux_version="3.8a", nonce="NONCE_38")
    assert "sess\\;" in cmd_38.argv
    assert "-T" in cmd_38.argv
    assert "-F" in cmd_38.argv
    assert cmd_38.flagged is True
    assert cmd_38.trim_trailing is True
    assert cmd_38.has_joined is False

    cmd_36 = build_tmux_capture_command("%1", tmux_version="3.6", nonce="NONCE_36")
    assert "-T" in cmd_36.argv
    assert "-F" not in cmd_36.argv
    assert "-J" in cmd_36.argv
    assert cmd_36.flagged is False
    assert cmd_36.trim_trailing is True
    assert cmd_36.has_joined is True

    cmd_33 = build_tmux_capture_command("%1", tmux_version="3.3a", nonce="NONCE_33")
    assert "-T" not in cmd_33.argv
    assert cmd_33.trim_trailing is False
    assert cmd_33.has_joined is True

    with pytest.raises(ValueError, match="nonce"):
        build_tmux_capture_command("%0", nonce="bad#nonce")
    with pytest.raises(ValueError, match="nonce"):
        build_tmux_capture_command("%0", nonce="NONCE_38\n")
    with pytest.raises(ValueError, match="target"):
        build_tmux_capture_command("")
    with pytest.raises(ValueError, match="invalid tmux capture-pane -F"):
        parse_flagged_line("-\n rest")
    with pytest.raises(ValueError, match="invalid tmux version"):
        libvt.Profile.tmux((True, 8))
    with pytest.raises(ValueError, match="invalid tmux version"):
        libvt.Profile.tmux("9999.0")

    clamped_meta = TmuxPaneMetadata.from_format_line("2147483647\t2147483647")
    assert (clamped_meta.cols, clamped_meta.rows) == (10_000, 10_000)

    sp_wrap = decode_tmux_capture(
        ["ABCD", "    ", "XYZ "],
        rows=3,
        cols=4,
        trim_trailing=False,
        joined_capture=["ABCD  ", "XYZ"],
    )
    assert sp_wrap.completeness.exact_wraps is True
    assert [sp_wrap.terminal[i].wrapped for i in range(3)] == [True, False, False]
    sp_wrap.terminal.resize(rows=3, cols=8)
    assert sp_wrap.terminal.lines == ("ABCD", "XYZ", "")

    empty_mid = decode_tmux_capture(
        ["ABCD", "", "EF"],
        rows=3,
        cols=4,
        trim_trailing=True,
        joined_capture=["ABCD", "EF"],
    )
    assert empty_mid.completeness.exact_wraps is False
    assert [empty_mid.terminal[i].wrapped for i in range(3)] == [False, False, False]

    alt_short = decode_tmux_capture(
        ["P H0", "P H1", "- ALT0", "- ALT1", "- ALT2"],
        metadata=TmuxPaneMetadata(cols=4, rows=3, alternate_on=True, history_size=2),
        scrollback=1,
        flagged=True,
        saved_primary_capture=["- P0"],
    )
    assert [z.abs_row for z in alt_short.terminal._prompt_zones] == [1]
    alt_short.terminal.feed(b"\x1b[?1049l")
    assert [r.text for r in alt_short.terminal.history] == ["H1"]
    assert alt_short.terminal.lines == ("P0", "", "")

    pre_line = (
        "4\t2\t0\t1\t1\t0\t4294967295\t4294967295\t0\t1\t"
        "0\t0\t1\t0\t0\t0\t0\t0\t0\t\t\t0\t2000\t0\t9\t/tmp/dir\ninject\tt"
    )
    bundle_out = (
        f"NONCE_38:PRE:{pre_line}\r\n"
        "NONCE_38:CAPTURE\r\n"
        "W abc\r\n"
        "X 中文\r\n"
        "NONCE_38:SAVED\r\n"
        "\r\n"
        f"NONCE_38:POST:{pre_line}\r\n"
    )
    parsed_bundle = cmd_38.parse(bundle_out.encode("utf-8"))
    assert parsed_bundle.completeness.exact_generation_bracket is True
    parsed_bundle.terminal.resize(rows=2, cols=8)
    assert parsed_bundle.terminal.lines == ("abc中文", "")

    spoof_pre = "24" + pre_line[1:]
    spoof_bundle = (
        f"NONCE_36:PRE:{spoof_pre}\n"
        "NONCE_36:CAPTURE\n"
        "NONCE_36:POST:spoof\n"
        "row2\n"
        "NONCE_36:JOINED\n"
        "NONCE_36:POST:spoof\n"
        "row2\n"
        "NONCE_36:SAVED\n"
        "\n"
        "NONCE_36:SAVED_JOINED\n"
        "\n"
        f"NONCE_36:POST:{spoof_pre}\n"
    )
    spoof_res = cmd_36.parse(spoof_bundle)
    assert spoof_res.completeness.exact_generation_bracket is True
    assert spoof_res.completeness.exact_wraps is True
    assert spoof_res.terminal.lines == ("NONCE_36:POST:spoof", "row2")


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux binary not installed")
def test_live_tmux_chained_bundle_overline_wraps_and_alt_screen() -> None:
    """Live ``tmux`` chained command bundle preserves wraps, ``5:3``, and ``-a``."""
    sock = f"libvt-test-{zlib.crc32(b'tmux-bundle')}"
    try:
        ver_out = subprocess.run(
            ["tmux", "-V"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
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
                "4",
                "-y",
                "5",
                "sh",
                "-c",
                (
                    "printf '\\033[53mOV\\033[0m\\r\\n"
                    "abc\\344\\270\\255\\346\\226\\207\\r\\n"
                    "abc \\344\\270\\255\\346\\226\\207\\r\\n"
                    "ab \\344\\270\\255\\346\\226\\207\\r\\n"
                    "\\033[?1049h\\033[HALT!'; sleep 30"
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
            if "ALT!" in chk.stdout:
                break
            time.sleep(0.01)
        else:
            pytest.fail("timed out waiting for tmux alt-screen output")

        cmd = build_tmux_capture_command("%0", tmux_version=ver_out)
        raw_out = subprocess.run(
            ["tmux", "-L", sock, *cmd.argv],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        libtmux_lines = raw_out.split("\n")
        while libtmux_lines and libtmux_lines[-1] == "":
            libtmux_lines.pop()

        for payload in (raw_out, libtmux_lines):
            res = cmd.parse(payload)
            assert res.snapshot.active_is_alt is True
            assert res.terminal[0].text == "ALT!"
            res.terminal.feed(b"\x1b[?1049l")
            assert res.terminal.history[0][0].style.overline is True
            res.terminal.resize(rows=6, cols=8)
            assert res.terminal.lines[:4] == ("OV", "abc中文", "abc 中文", "ab 中文")
    finally:
        subprocess.run(
            ["tmux", "-L", sock, "kill-server"],
            check=False,
            capture_output=True,
        )
