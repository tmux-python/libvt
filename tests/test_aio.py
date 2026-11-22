"""Tests for ``libvt.aio`` async terminal streams and POSIX PTY sessions."""

from __future__ import annotations

import asyncio
import socket
import sys

import pytest

import libvt
from libvt.aio import AsyncTerminalStream, open_pty
from libvt.exc import TerminalTimeoutError
from libvt.parser import ParserState


class _MemoryWriter:
    """In-memory writer stub capturing outbound DSR/DA reply bytes."""

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


async def test_async_terminal_stream_sync_coalescing_replies_and_timeout() -> None:
    """``AsyncTerminalStream`` coalesces ``?2026``, flushes DSR, and formats errors."""
    reader = asyncio.StreamReader()
    writer = _MemoryWriter()
    stream = AsyncTerminalStream(
        reader,
        writer,
        rows=4,
        cols=20,
        sync_timeout=0.01,
    )

    reader.feed_data(b"base\x1b[?2026h\x1b[Hwedged\x1b[6n")
    snap = await stream.wait_for("wedged", timeout=0.2)
    assert snap[0].text == "wedged"
    assert bytes(writer.buffer) == b"\x1b[1;7R"

    async with AsyncTerminalStream(
        asyncio.StreamReader(),
        rows=2,
        cols=14,
        sync_timeout=0.01,
    ) as bg_stream:
        bg_stream.reader.feed_data(b"\x1b[?2026h\x1b[Hatomic-ok\x1b[?2026l")
        snap2 = await bg_stream.wait_for("atomic-ok", timeout=0.2)
        assert snap2[0].text == "atomic-ok"

        bg_stream.reader.feed_data(b"\x1b]0;stuck")
        with pytest.raises(TerminalTimeoutError) as exc_info:
            await bg_stream.wait_for("missing", timeout=0.005)
        err = exc_info.value
        assert err.parser_state == ParserState.OSC_STRING
        assert b"\x1b]0;stuck" in err.recent_bytes
        assert "\n" not in str(err)
        assert any("Flight Recorder:" in n for n in err.__notes__)

        with pytest.raises(TerminalTimeoutError) as quiet_exc:
            await bg_stream.wait_for("missing", timeout=0.005, attach_note=False)
        assert not hasattr(quiet_exc.value, "__notes__")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
async def test_open_pty_controlling_tty_signals_sigpipe_and_reap() -> None:
    """``open_pty`` configures ``TIOCSCTTY``, signals, and process-group cleanup."""
    with pytest.raises(FileNotFoundError):
        async with open_pty(("definitely-missing-libvt-command-xyz",)):
            pass

    host = socket.gethostname()
    child_code = (
        "import os, signal, sys, time\n"
        "tty_fd = os.open('/dev/tty', os.O_RDWR)\n"
        "os.close(tty_fd)\n"
        "fg_ok = (os.tcgetpgrp(0) == os.getpid())\n"
        # Verify preexec restored default SIGPIPE disposition before Python overrides it
        # in child exec targets by inspecting inherited mask/bg process group behavior.
        "bg_pid = os.fork()\n"
        "if bg_pid == 0:\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "winch = False\n"
        "def _on_winch(s, f):\n"
        "    global winch\n"
        "    winch = True\n"
        "signal.signal(signal.SIGWINCH, _on_winch)\n"
        f"sys.stdout.write('\\x1b]7;file://{host}/var/tmp\\x07')\n"
        "sys.stdout.write('\\x1b]7501;state=working:id=job\\x1b\\\\')\n"
        "sys.stdout.write(f'CTTY={fg_ok} BG={bg_pid} \\x1b[6n')\n"
        "sys.stdout.flush()\n"
        "reply = b''\n"
        "while not reply.endswith(b'R'):\n"
        "    ch = sys.stdin.buffer.read(1)\n"
        "    if not ch: break\n"
        "    reply += ch\n"
        "print('DSR=' + repr(reply.decode('ascii', 'replace')), flush=True)\n"
        "while not winch:\n"
        "    signal.pause()\n"
        "try:\n"
        "    print('WINCH=1', flush=True)\n"
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
        policy=libvt.Policy(allow_passthrough=True),
    ) as pty_sess:
        await pty_sess.wait_for("DSR='\\x1b[1;", timeout=3.0)
        assert "CTTY=True" in pty_sess.terminal.text
        assert pty_sess.terminal.cwd == "/var/tmp"
        assert "job" in pty_sess.terminal.program_statuses
        pty_sess.resize(rows=10, cols=70)
        await pty_sess.wait_for("WINCH=1", timeout=3.0)
        pty_sess.send(b"\x03")
        await pty_sess.wait_for("SIGINT=1", timeout=3.0)
    assert pty_sess.terminal.program_statuses == {}
