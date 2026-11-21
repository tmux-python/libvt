"""Async stream, POSIX PTY subprocess, pytest fixture, and boxed assertion tests."""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import socket
import sys

import pytest

import libvt
from libvt.aio import AsyncTerminalStream, open_pty
from libvt.pytest_plugin import PtyFactory, TerminalFactory
from libvt.terminal import FlightRecorder, Terminal
from libvt.testing import assert_screen, assert_screen_box


class _MemoryWriter:
    """Minimal writer stub capturing reply bytes flushed by ``AsyncTerminalStream``."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes, /) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


async def test_async_terminal_stream_sync_coalescing_timeout_and_replies() -> None:
    """``AsyncTerminalStream`` coalesces ``?2026``, expires wedges, and writes DSR."""
    reader = asyncio.StreamReader()
    writer = _MemoryWriter()
    stream = AsyncTerminalStream(
        reader,
        writer,
        rows=4,
        cols=20,
        sync_timeout=0.05,
    )

    reader.feed_data(b"base\x1b[?2026h\x1b[Hwedged\x1b[6n")
    snap = await stream.wait_for("wedged", timeout=0.5)
    assert snap[0].text == "wedged"
    assert bytes(writer.buffer) == b"\x1b[1;7R"

    async with AsyncTerminalStream(
        asyncio.StreamReader(),
        rows=4,
        cols=20,
        sync_timeout=0.05,
    ) as bg_stream:
        bg_stream.reader.feed_data(b"\x1b[?2026h\x1b[Hatomic-ok\x1b[?2026l")
        snap2 = await bg_stream.wait_for("atomic-ok", timeout=0.5)
        assert snap2[0].text == "atomic-ok"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
async def test_open_pty_controlling_tty_signals_tty_mode_and_errors(
    vt_pty: PtyFactory,
) -> None:
    """``open_pty`` sets ``TIOCSCTTY``, ``SIGWINCH``/``SIGINT``, and ``SIGPIPE``."""
    host = socket.gethostname()
    child_code = (
        "import os, signal, sys\n"
        "tty_fd = os.open('/dev/tty', os.O_RDWR)\n"
        "os.close(tty_fd)\n"
        "fg_ok = (os.tcgetpgrp(0) == os.getpid())\n"
        "winch = False\n"
        "def _on_winch(s, f):\n"
        "    global winch\n"
        "    winch = True\n"
        "signal.signal(signal.SIGWINCH, _on_winch)\n"
        f"sys.stdout.write('\\x1b]7;file://{host}/var/tmp\\x07')\n"
        "sys.stdout.write('\\x1b]7501;state=working:id=job\\x1b\\\\')\n"
        "sys.stdout.write(f'CTTY={fg_ok} \\x1b[6n')\n"
        "sys.stdout.flush()\n"
        "reply = b''\n"
        "while not reply.endswith(b'R'):\n"
        "    ch = sys.stdin.buffer.read(1)\n"
        "    if not ch: break\n"
        "    reply += ch\n"
        "print('DSR=' + repr(reply.decode('ascii', 'replace')), flush=True)\n"
        "while not winch:\n"
        "    signal.pause()\n"
        "print('WINCH=1', flush=True)\n"
        "try:\n"
        "    while True:\n"
        "        signal.pause()\n"
        "except KeyboardInterrupt:\n"
        "    print('SIGINT=1', flush=True)\n"
    )
    async with open_pty(
        (sys.executable, "-u", "-c", child_code),
        rows=8,
        cols=60,
        tty_mode="cbreak",
    ) as pty_sess:
        await pty_sess.wait_for("DSR='\\x1b[1;11R'", timeout=3.0)
        assert "CTTY=True" in pty_sess.terminal.text
        assert pty_sess.terminal.cwd == "/var/tmp"
        assert "job" in pty_sess.terminal.program_statuses
        pty_sess.resize(rows=10, cols=70)
        await pty_sess.wait_for("WINCH=1", timeout=3.0)
        pty_sess.send(b"\x03")
        await pty_sess.wait_for("SIGINT=1", timeout=3.0)
    assert pty_sess.terminal.program_statuses == {}

    # Non-Python execvp targets inherit default SIGPIPE (not Python's SIG_IGN).
    cat_bin = shutil.which("cat")
    if cat_bin is not None and pathlib.Path("/proc/self/status").exists():
        async with vt_pty((cat_bin, "/proc/self/status"), rows=40, cols=80) as cat_sess:
            snap = await cat_sess.wait_for("SigIgn:", timeout=3.0)
        sig_ign_hex = next(
            ln.split(":")[1].strip() for ln in snap.lines if ln.startswith("SigIgn:")
        )
        sigpipe_bit = 1 << 12  # signal 13 (SIGPIPE) -> bit 12
        assert (int(sig_ign_hex, 16) & sigpipe_bit) == 0

    # Missing executable raises FileNotFoundError in parent with zero PTY noise.
    with pytest.raises(FileNotFoundError):
        async with open_pty(("definitely-missing-libvt-command-xyz",)):
            pass


def test_assert_screen_literal_borders_and_assert_screen_box(
    vt_terminal: Terminal,
    vt_factory: TerminalFactory,
    vt_parser: libvt.Parser,
    vt_recorder: FlightRecorder,
) -> None:
    """``assert_screen`` keeps borders literal; ``assert_screen_box`` checks frames."""
    assert vt_terminal.rows == 24
    assert vt_terminal.cols == 80
    assert vt_terminal.recorder is vt_recorder
    assert isinstance(vt_parser, libvt.Parser)

    # Regression test: real DEC Special Graphics border passes assert_screen verbatim!
    bordered = vt_factory(rows=3, cols=5)
    bordered.feed(b"\x1b(0lqqqk\r\nx X x\r\nmqqqj\x1b(B")
    assert bordered.text == "┌───┐\n│ X │\n└───┘"
    assert_screen(bordered, bordered.text)
    assert_screen(
        bordered,
        """
        ┌───┐
        │ X │
        └───┘
        """,
    )

    # Boxed assertions verify column width, soft-wrap 'W', and literal 'W' inside cells.
    small = vt_factory(rows=2, cols=8)
    small.feed("日本ok\r\nline2  W".encode())
    assert_screen_box(
        small,
        """
        ┌────────┐
        │日本ok  │
        │line2  W│
        └────────┘
        """,
    )

    wrapped = vt_factory(rows=2, cols=4)
    wrapped.feed(b"abcdef")
    assert_screen_box(
        wrapped,
        """
        ┌────┐
        │abcd│W
        │ef  │
        └────┘
        """,
        cursor=(1, 2),
    )

    with pytest.raises(ValueError, match="display width"):
        assert_screen_box(small, "┌────┐\n│short│\n└────┘")

    spoof_term = vt_factory(rows=1, cols=10, recorder=vt_recorder)
    spoof_term.feed(b"\x1b[31mhi")
    spoof_term.screen._rows[0].chars[2] = "\u202e"
    spoof_term.screen._rows[0].used = 3
    with pytest.raises(AssertionError) as exc_info:
        assert_screen_box(spoof_term, "┌──────────┐\n│mismatch  │\n└──────────┘")
    msg = str(exc_info.value)
    assert "\\u202e" in msg
    assert "Flight Recorder:" in msg
