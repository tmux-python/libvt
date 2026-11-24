"""Tests for ``libvt.aio`` async terminal streams and POSIX PTY sessions."""

from __future__ import annotations

import asyncio
import errno
import importlib.util
import os
import pathlib
import socket
import struct
import sys
import typing as t

import pytest

import libvt
from libvt.aio import AsyncTerminalStream, _pack_winsize, open_pty
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


async def test_stream_eof_aclose_and_straddling_sync_frame_wakeups() -> None:
    """``AsyncTerminalStream`` expires ``?2026`` on EOF/aclose and wakes mid-chunk."""
    reader = asyncio.StreamReader()
    stream = AsyncTerminalStream(reader, rows=3, cols=24, sync_timeout=10.0)
    reader.feed_data(b"before\x1b[?2026hCRASH_REASON")
    reader.feed_eof()
    await stream.pump_once()
    assert stream.terminal.synchronized is False
    assert stream.terminal.committed.text == "beforeCRASH_REASON"

    aclose_r = asyncio.StreamReader()
    aclose_s = AsyncTerminalStream(aclose_r, rows=2, cols=20, sync_timeout=10.0)
    aclose_r.feed_data(b"init\x1b[?2026hHELD_ON_CLOSE")
    await aclose_s.pump_once()
    assert aclose_s.terminal.synchronized is True
    await aclose_s.aclose()
    assert aclose_s.terminal.synchronized is False
    assert aclose_s.terminal.committed.text == "initHELD_ON_CLOSE"

    async with AsyncTerminalStream(
        asyncio.StreamReader(),
        rows=2,
        cols=20,
        sync_timeout=10.0,
    ) as straddle:
        waiter = asyncio.create_task(straddle.wait_for("FRAME_ONE", timeout=0.3))
        await asyncio.sleep(0)
        straddle.reader.feed_data(
            b"\x1b[?2026h\x1b[HFRAME_ONE\x1b[?2026l\x1b[?2026h\x1b[HFRAME_TWO"
        )
        snap = await waiter
        assert snap[0].text == "FRAME_ONE"
        assert straddle.terminal.synchronized is True
        with pytest.raises(TerminalTimeoutError):
            await straddle.wait_for("FRAME_TWO", timeout=0.01)
    assert straddle.terminal.synchronized is False
    assert straddle.terminal.committed[0].text == "FRAME_TWO"

    async with AsyncTerminalStream(
        asyncio.StreamReader(),
        rows=2,
        cols=20,
        sync_timeout=0.02,
    ) as timer_stream:
        w2 = asyncio.create_task(timer_stream.wait_for("WEDGED", timeout=5.0))
        await asyncio.sleep(0)
        timer_stream.reader.feed_data(b"\x1b[?2026hWEDGED")
        snap_w2 = await asyncio.wait_for(w2, timeout=0.4)
        assert snap_w2[0].text == "WEDGED"

    mode_term = libvt.Terminal(rows=2, cols=20)
    mode_term.feed(b"BEFORE\x1b[?2026h\x1b[HAFTER")
    mode_term.modes.set_mode(libvt.Mode.SYNCHRONIZED_OUTPUT, enabled=False)
    mode_term.feed(b" NEW")
    assert mode_term.synchronized is False
    assert mode_term.committed.text == "AFTER NEW"
    with pytest.raises(ValueError, match="finite"):
        AsyncTerminalStream(asyncio.StreamReader(), sync_timeout=float("inf"))


async def test_aio_lazy_posix_imports_under_win32_simulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``libvt.aio`` imports cleanly when POSIX PTY modules are unavailable."""
    for mod_name in ("pty", "termios", "tty", "fcntl"):
        monkeypatch.setitem(sys.modules, mod_name, None)
    aio_path = pathlib.Path(libvt.__file__).resolve().parent / "aio.py"
    spec = importlib.util.spec_from_file_location("_libvt_aio_win_check", aio_path)
    assert spec is not None and spec.loader is not None
    fresh_mod = importlib.util.module_from_spec(spec)
    sys.modules["_libvt_aio_win_check"] = fresh_mod
    try:
        spec.loader.exec_module(fresh_mod)
        r = asyncio.StreamReader()
        s = fresh_mod.AsyncTerminalStream(r, rows=2, cols=10)
        assert s.terminal.rows == 2
        await asyncio.sleep(0)
    finally:
        sys.modules.pop("_libvt_aio_win_check", None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
async def test_pty_session_write_queue_paste_ordering_resize_and_cancel() -> None:
    """``PtySession`` buffers pastes, orders replies, flushes ?2048, and closes."""
    child_code = (
        "import sys\n"
        "sys.stdout.write('\\x1b[?2004h\\x1b[?2048hREADY\\n')\n"
        "sys.stdout.flush()\n"
        "buf = bytearray(sys.stdin.buffer.read(512))\n"
        "sys.stdout.write('\\x1b[6n')\n"
        "sys.stdout.flush()\n"
        "target = 24576 + 12\n"
        "while len(buf) < target:\n"
        "    chunk = sys.stdin.buffer.read(min(4096, target - len(buf)))\n"
        "    if not chunk: break\n"
        "    buf.extend(chunk)\n"
        "while not (b'R' in buf[target:] and b't' in buf[target:]):\n"
        "    ch = sys.stdin.buffer.read(1)\n"
        "    if not ch: break\n"
        "    buf.extend(ch)\n"
        "paste_ok = buf[:target] == (b'\\x1b[200~' + b'P' * 24576 + b'\\x1b[201~')\n"
        "tail = bytes(buf[target:])\n"
        "dsr_ok = b'R' in tail\n"
        "rsz_ok = b'\\x1b[48;12;64;192;512t' in tail\n"
        "import fcntl, struct, termios\n"
        "ws = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\x00' * 8))\n"
        "ws_ok = ws == (12, 64, 512, 192)\n"
        "sys.stdout.write(\n"
        "    f'PASTE={paste_ok} DSR={dsr_ok} RSZ={rsz_ok} WS={ws_ok}'"
        " + '\\x1b[?2026hTAIL_EOF'\n"
        ")\n"
        "sys.stdout.flush()\n"
    )
    with pytest.raises(ValueError, match="rows and cols"):
        async with open_pty((sys.executable, "-c", "pass"), rows=0):
            pass
    async with open_pty(
        (sys.executable, "-u", "-c", child_code),
        rows=6,
        cols=50,
        tty_mode="raw",
        sync_timeout=10.0,
        write_high_water=0,
    ) as sess:
        with pytest.raises(ValueError, match="rows and cols"):
            sess.resize(rows=70_000, cols=50)
        with pytest.raises(TypeError):
            t.cast("t.Any", sess).send(12345)
        await sess.wait_for("READY", timeout=3.0)
        sess.paste("P" * 24_576)
        sess.resize(rows=12, cols=64)
        await sess.drain()
        snap = await sess.wait_for("TAIL_EOF", timeout=3.0)
        assert "PASTE=True" in snap.text
        assert "DSR=True" in snap.text
        assert "RSZ=True" in snap.text
        assert "WS=True" in snap.text
    assert sess.terminal.synchronized is False
    packed_ws = _pack_winsize(
        1_000,
        1_000,
        libvt.Profile(cell_width_px=100, cell_height_px=100),
    )
    assert struct.unpack("HHHH", packed_ws) == (1_000, 1_000, 65_535, 65_535)
    gen_after_close = sess.terminal.committed_generation
    await sess.aclose()
    assert sess.terminal.committed_generation == gen_after_close

    async with open_pty(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        tty_mode="raw",
        max_write_buffer=32_768,
    ) as hw_sess:
        hw_sess.send(b"x" * 16_384)
        await asyncio.wait_for(hw_sess.drain(), timeout=0.2)

    # Verify master_fd closes even if CancelledError interrupts aclose()/open_pty.
    ign_code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.write('UP\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    captured_fd = -1

    async def _run_and_hang_close() -> None:
        nonlocal captured_fd
        async with open_pty((sys.executable, "-u", "-c", ign_code)) as s2:
            captured_fd = s2._master_fd
            await s2.wait_for("UP", timeout=3.0)
            await asyncio.sleep(10.0)

    t_cancel = asyncio.create_task(_run_and_hang_close())
    while captured_fd < 0:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    t_cancel.cancel()
    await asyncio.sleep(0.05)
    t_cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t_cancel
    with pytest.raises(OSError) as fd_exc:  # noqa: PT011
        os.fstat(captured_fd)
    assert fd_exc.value.errno == errno.EBADF
