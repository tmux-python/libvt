"""Async stream, POSIX PTY subprocess, pytest fixture, and boxed assertion tests."""

from __future__ import annotations

import asyncio
import sys

import pytest

import libvt
from libvt.aio import AsyncTerminalStream, open_pty
from libvt.pytest_plugin import PtyFactory, TerminalFactory
from libvt.terminal import FlightRecorder, Terminal
from libvt.testing import assert_screen


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

    # Direct wait_for (without background start()) expires wedged ?2026h cleanly.
    reader.feed_data(b"base\x1b[?2026h\x1b[Hwedged\x1b[6n")
    snap = await stream.wait_for("wedged", timeout=0.5)
    assert snap[0].text == "wedged"
    assert bytes(writer.buffer) == b"\x1b[1;7R"

    # Background pump task mode coalesces normal ?2026h ... ?2026l batches cleanly.
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
async def test_open_pty_and_vt_pty_live_posix_subprocess(
    vt_pty: PtyFactory,
) -> None:
    """``open_pty`` and ``vt_pty`` bridge live child processes and DSR replies."""
    child_code = (
        "import sys, tty; "
        "tty.setcbreak(sys.stdin.fileno()); "
        "sys.stdout.write('\\x1b]7501;state=working:id=job\\x1b\\\\hello\\x1b[6n'); "
        "sys.stdout.flush(); "
        "reply = b''; "
        "\nwhile not reply.endswith(b'R'):\n"
        "    ch = sys.stdin.buffer.read(1)\n"
        "    if not ch: break\n"
        "    reply += ch\n"
        "print(' DSR=' + repr(reply.decode('ascii', 'replace')), flush=True); "
        "line = b''\n"
        "while not line.endswith((b'\\r', b'\\n')):\n"
        "    b = sys.stdin.buffer.read(1)\n"
        "    if not b: break\n"
        "    line += b\n"
        "print('ECHO=' + line.decode('utf-8').strip(), flush=True)"
    )
    async with open_pty(
        (sys.executable, "-u", "-c", child_code),
        rows=6,
        cols=40,
    ) as pty_sess:
        await pty_sess.wait_for("DSR='\\x1b[1;6R'", timeout=3.0)
        assert "job" in pty_sess.terminal.program_statuses
        pty_sess.resize(rows=8, cols=50)
        pty_sess.send("ping")
        pty_sess.send_key("enter")
        snap = await pty_sess.wait_for("ECHO=ping", timeout=3.0)
        assert "ECHO=ping" in snap.text
    # After exit, active working status is pruned automatically.
    assert pty_sess.terminal.program_statuses == {}

    async with vt_pty(
        (sys.executable, "-c", "print('fixture-pty-ok')"),
        rows=4,
        cols=30,
    ) as sess2:
        await sess2.wait_for("fixture-pty-ok", timeout=3.0)


def test_pytest_fixtures_and_assert_screen_diagnostics(
    vt_terminal: Terminal,
    vt_factory: TerminalFactory,
    vt_parser: libvt.Parser,
    vt_recorder: FlightRecorder,
) -> None:
    """Fixtures construct clean instances and ``assert_screen`` escapes BiDi spoofs."""
    assert vt_terminal.rows == 24
    assert vt_terminal.cols == 80
    assert vt_terminal.recorder is vt_recorder
    assert isinstance(vt_parser, libvt.Parser)

    small = vt_factory(rows=2, cols=8)
    small.feed("日本ok\r\nline2".encode())
    assert_screen(
        small,
        "┌────────┐\n│日本ok  │\n│line2   │\n└────────┘",
    )

    # BiDi spoofing in cells is escaped in failure diffs alongside FlightRecorder dump.
    spoof_term = vt_factory(rows=1, cols=10, recorder=vt_recorder)
    spoof_term.feed(b"\x1b[31mhi")
    spoof_term.screen._rows[0].chars[2] = "\u202e"
    spoof_term.screen._rows[0].used = 3
    with pytest.raises(AssertionError) as exc_info:
        assert_screen(spoof_term, "┌──────────┐\n│mismatch  │\n└──────────┘")
    msg = str(exc_info.value)
    assert "\\u202e" in msg
    assert "Flight Recorder:" in msg
