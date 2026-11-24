"""``asyncio`` stream and POSIX PTY adapters over :class:`~libvt.terminal.Terminal`."""

from __future__ import annotations

import asyncio
import collections
import collections.abc
import contextlib
import dataclasses
import errno
import math
import os
import signal
import socket
import struct
import sys
import time
import typing as t

from libvt.config import (
    DEFAULT_LIMITS,
    DEFAULT_OSC7_HOSTS,
    DEFAULT_POLICY,
    DEFAULT_PROFILE,
    Limits,
    Policy,
    Profile,
)
from libvt.exc import TerminalTimeoutError
from libvt.input import InputEvent, KeyEvent, PasteEvent
from libvt.parser import _parse_ascii_uint
from libvt.terminal import FlightRecorder, Snapshot, Terminal

type TtyMode = t.Literal["cooked", "cbreak", "raw"]

_PTY_TRAMPOLINE = (
    "stage = 'bootstrap'\n"
    "err_fd = -1\n"
    "try:\n"
    "    import errno, fcntl, os, signal, sys, termios\n"
    "    err_fd = int(sys.argv[1])\n"
    "    os.set_inheritable(err_fd, False)\n"
    "    stage = 'ctty'\n"
    "    if hasattr(termios, 'TIOCSCTTY'):\n"
    "        fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "    stage = 'signal'\n"
    "    signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
    "    if hasattr(signal, 'SIGXFSZ'):\n"
    "        signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
    "    stage = 'exec'\n"
    "    os.write(err_fd, b'\\x00')\n"
    "    os.execvp(sys.argv[2], sys.argv[2:])\n"
    "except BaseException as exc:\n"
    "    try:\n"
    "        import errno as _e, os as _os\n"
    "        if err_fd >= 0:\n"
    "            code = getattr(exc, 'errno', None)\n"
    "            if not isinstance(code, int):\n"
    "                code = _e.ENOEXEC\n"
    "            msg = (getattr(exc, 'strerror', None) or str(exc))[:360]\n"
    "            pkt = b'\\x01' + f'{stage}:{code}:{msg}'.encode('utf-8', 'replace')\n"
    "            _os.write(err_fd, pkt[:500])\n"
    "    except BaseException:\n"
    "        pass\n"
    "    import os as _os2\n"
    "    _os2._exit(127)\n"
)


def _raise_exec_error(err_payload: bytes, cmd: str, default_errno: int) -> t.NoReturn:
    if not err_payload:
        msg = f"PTY launcher exited before pre-exec handoff while starting {cmd!r}"
        raise RuntimeError(msg)
    idx = err_payload.find(b"\x01")
    body = err_payload[idx + 1 :] if idx != -1 else err_payload
    parts = body.decode("utf-8", "replace").split(":", 2)
    if len(parts) == 3:
        stage, code_s, err_msg = parts
    else:
        stage, code_s, err_msg = "exec", "", body.decode("utf-8", "replace")
    parsed_err = _parse_ascii_uint(code_s, max_val=65_535)
    err_no = default_errno if parsed_err is None else parsed_err
    target = cmd if stage == "exec" else f"open_pty({stage})"
    raise OSError(err_no, err_msg or os.strerror(err_no), target)


async def _read_cloexec_pipe(loop: asyncio.AbstractEventLoop, fd: int) -> bytes:
    """Read non-blocking *fd* until EOF without spawning executor threads."""
    chunks: list[bytes] = []
    done = loop.create_future()

    def _on_ready() -> None:
        while True:
            try:
                data = os.read(fd, 4_096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                loop.remove_reader(fd)
                if not done.done():
                    done.set_exception(exc)
                return
            if not data:
                loop.remove_reader(fd)
                if not done.done():
                    done.set_result(b"".join(chunks))
                return
            chunks.append(data)

    loop.add_reader(fd, _on_ready)
    try:
        return await done
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(fd)


def _make_timeout_error(
    terminal: Terminal,
    snap: Snapshot,
    header: str,
    *,
    eof: bool,
    attach_note: bool = True,
) -> TerminalTimeoutError:
    from libvt.testing import format_flight_recorder, format_screen_box

    rec = terminal.recorder
    recent = rec.tail(256) if rec is not None else b""
    box = format_screen_box(snap, rows=min(snap.rows, 12), cols=min(snap.cols, 80))
    diag = format_flight_recorder(terminal)
    return TerminalTimeoutError(
        header,
        parser_state=terminal.parser_state,
        screen_text=snap.text,
        recent_bytes=recent,
        eof=eof,
        diagnostics=f"{box}\n{diag}",
        attach_note=attach_note,
    )


class ReplyWriter(t.Protocol):
    """Structural writer protocol satisfied by :class:`asyncio.StreamWriter`."""

    def write(self, data: bytes, /) -> object:
        """Buffer outbound *data* bytes."""

    async def drain(self) -> None:
        """Flush buffered outbound bytes."""

    def close(self) -> object:
        """Initiate writer close."""

    async def wait_closed(self) -> None:
        """Wait until writer close completes."""


_PEER_EOF_ERRNOS = (errno.EIO, errno.EPIPE, errno.ENXIO)
_COALESCE_SLAB = 16_384


def _validate_nonneg_seconds(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        msg = f"{name} must be a finite non-negative number, got {value!r}"
        raise ValueError(msg)


def _validate_pty_dims(rows: int, cols: int) -> None:
    if (
        isinstance(rows, bool)
        or isinstance(cols, bool)
        or not isinstance(rows, int)
        or not isinstance(cols, int)
        or not (1 <= rows <= 65_535)
        or not (1 <= cols <= 65_535)
    ):
        msg = f"rows and cols must be integers in 1..65535, got ({rows!r}, {cols!r})"
        raise ValueError(msg)


def _resolve_write_limits(
    write_high_water: int | None,
    max_write_buffer: int,
) -> tuple[int, int]:
    if (
        isinstance(max_write_buffer, bool)
        or not isinstance(max_write_buffer, int)
        or max_write_buffer < 1
    ):
        msg = f"max_write_buffer must be an int >= 1, got {max_write_buffer!r}"
        raise ValueError(msg)
    if write_high_water is None:
        eff_hw = min(65_536, max_write_buffer)
    else:
        if (
            isinstance(write_high_water, bool)
            or not isinstance(write_high_water, int)
            or write_high_water < 0
        ):
            msg = f"write_high_water must be >= 0, got {write_high_water!r}"
            raise ValueError(msg)
        if max_write_buffer < write_high_water:
            msg = (
                "max_write_buffer must be >= write_high_water, "
                f"got {max_write_buffer!r} < {write_high_water!r}"
            )
            raise ValueError(msg)
        eff_hw = write_high_water
    return eff_hw, max_write_buffer


class AsyncTerminalStream:
    """Drive a sans-I/O :class:`~libvt.terminal.Terminal` from ``asyncio`` streams.

    Coalesces frame wakeups across DEC mode ``?2026`` synchronized-output
    batches (bounded by *sync_timeout* using generation-safe expiry), expires
    any active ``?2026`` hold on ``EOF`` and :meth:`aclose`, and automatically
    flushes :meth:`~libvt.terminal.Terminal.drain_replies` when a
    ``StreamWriter`` is attached.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: ReplyWriter | None = None,
        *,
        terminal: Terminal | None = None,
        rows: int = 24,
        cols: int = 80,
        chunk_size: int = 4_096,
        sync_timeout: float = 1.0,
    ) -> None:
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or chunk_size < 1
        ):
            msg = f"chunk_size must be >= 1, got {chunk_size!r}"
            raise ValueError(msg)
        if terminal is None:
            _validate_pty_dims(rows, cols)
        _validate_nonneg_seconds(sync_timeout, "sync_timeout")
        self.reader = reader
        self.writer = writer
        self.terminal = (
            terminal
            if terminal is not None
            else Terminal(rows=rows, cols=cols, recorder=FlightRecorder())
        )
        self.chunk_size = chunk_size
        self.sync_timeout = sync_timeout
        self._frame_event = asyncio.Event()
        self._eof = False
        self._closed = False
        self._sync_started_at: float | None = None
        self._sync_gen_tracked: int | None = None
        self._sync_timer_handle: asyncio.TimerHandle | None = None
        self._sync_timer_gen: int | None = None
        self._last_committed_gen: int = self.terminal.committed_generation
        self._task: asyncio.Task[None] | None = None

    @property
    def eof(self) -> bool:
        """Return ``True`` once ``reader`` reaches end-of-stream."""
        return self._eof

    def _cancel_sync_timer(self) -> None:
        if self._sync_timer_handle is not None:
            self._sync_timer_handle.cancel()
            self._sync_timer_handle = None
        self._sync_timer_gen = None

    def _on_sync_timeout(self) -> None:
        self._sync_timer_handle = None
        self._sync_timer_gen = None
        self._update_sync_timer(time.monotonic())

    def _mark_eof(self) -> None:
        self._eof = True
        self._cancel_sync_timer()
        self.terminal.expire_synchronized_output()
        self._sync_started_at = None
        self._sync_gen_tracked = None
        self._last_committed_gen = self.terminal.committed_generation
        self._frame_event.set()

    def _update_sync_timer(self, now: float) -> None:
        if self.terminal.synchronized:
            cur_gen = self.terminal.sync_generation
            if self._sync_gen_tracked != cur_gen or self._sync_started_at is None:
                self._sync_gen_tracked = cur_gen
                self._sync_started_at = now
            if (
                now - self._sync_started_at >= self.sync_timeout
                and self.terminal.expire_synchronized_output(cur_gen)
            ):
                self._cancel_sync_timer()
                self._sync_started_at = None
                self._sync_gen_tracked = None
            elif (
                self._task is not None
                and not self._task.done()
                and self._sync_timer_gen != cur_gen
            ):
                self._cancel_sync_timer()
                delay = max(0.0, self._sync_started_at + self.sync_timeout - now)
                with contextlib.suppress(RuntimeError):
                    loop = asyncio.get_running_loop()
                    self._sync_timer_gen = cur_gen
                    self._sync_timer_handle = loop.call_later(
                        delay,
                        self._on_sync_timeout,
                    )
        else:
            self._cancel_sync_timer()
            self._sync_started_at = None
            self._sync_gen_tracked = None
        cur_committed = self.terminal.committed_generation
        if cur_committed != self._last_committed_gen:
            self._last_committed_gen = cur_committed
            self._frame_event.set()

    async def pump_once(self) -> bool:
        """Read and feed one chunk; return ``False`` on EOF or after close."""
        if self._closed:
            return False
        chunk = await self.reader.read(self.chunk_size)
        if not chunk:
            self._mark_eof()
            return False
        self.terminal.feed(chunk)
        replies = self.terminal.drain_replies()
        if self.reader.at_eof():
            self._mark_eof()
        else:
            self._update_sync_timer(time.monotonic())
        if replies and self.writer is not None and not self._closed:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.writer.write(replies)
                await self.writer.drain()
        return True

    async def _pump_loop(self) -> None:
        try:
            while await self.pump_once():
                pass
        finally:
            self._cancel_sync_timer()
            self._frame_event.set()

    def start(self) -> asyncio.Task[None]:
        """Start background stream pumping on the running event loop."""
        if self._task is None or self._task.done():
            self._closed = False
            self._task = asyncio.create_task(self._pump_loop())
        return self._task

    async def aclose(self) -> None:
        """Cancel background pump task, expire ``?2026`` holds, and close writer."""
        self._closed = True
        self._cancel_sync_timer()
        try:
            if self._task is not None:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
                self._task = None
            if self.writer is not None:
                self.writer.close()
                with contextlib.suppress(OSError):
                    await self.writer.wait_closed()
        finally:
            self._task = None
            self._cancel_sync_timer()
            self.terminal.expire_synchronized_output()
            self._sync_started_at = None
            self._sync_gen_tracked = None
            self._last_committed_gen = self.terminal.committed_generation
            self._frame_event.set()

    async def __aenter__(self) -> t.Self:
        """Start background pumping when entering an ``async with`` block."""
        self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """Stop background pumping and close writer when leaving ``async with``."""
        await self.aclose()

    async def wait_for(
        self,
        predicate: collections.abc.Callable[[Snapshot], bool] | str,
        *,
        timeout: float = 2.0,
        attach_note: bool = True,
    ) -> Snapshot:
        """Wait until *predicate* matches :class:`~libvt.terminal.Snapshot`.

        Parameters
        ----------
        predicate : collections.abc.Callable[[Snapshot], bool] | str
            Callable tested against each committed snapshot, or a substring
            searched inside ``snapshot.text``.
        timeout : float
            Maximum wait duration in seconds before raising
            :exc:`~libvt.exc.TerminalTimeoutError`.
        attach_note : bool
            When ``True`` (default), attach multi-line screen and flight
            recorder diagnostics via PEP 678 ``__notes__``.

        Returns
        -------
        Snapshot
            First matching :class:`~libvt.terminal.Snapshot`.

        Raises
        ------
        TerminalTimeoutError
            If the stream reaches EOF/close before matching or *timeout*
            expires.
        """
        _validate_nonneg_seconds(timeout, "timeout")
        check = (
            (lambda snap: predicate in snap.text)
            if isinstance(predicate, str)
            else predicate
        )
        deadline = time.monotonic() + timeout
        while True:
            if self._task is not None and self._task.done():
                if self._task.cancelled():
                    self._task = None
                else:
                    task_exc = self._task.exception()
                    if task_exc is not None:
                        raise task_exc
            now = time.monotonic()
            if self.terminal.synchronized:
                self._update_sync_timer(now)
            ended = self._eof or self._closed
            snap = self.terminal.snapshot() if ended else self.terminal.committed
            if check(snap):
                return snap
            if ended:
                reason = (
                    "stream reached EOF before predicate matched"
                    if self._eof
                    else "stream closed before predicate matched"
                )
                raise _make_timeout_error(
                    self.terminal,
                    snap,
                    reason,
                    eof=self._eof,
                    attach_note=attach_note,
                )
            remaining = deadline - now
            if remaining <= 0:
                live = self.terminal.snapshot()
                raise _make_timeout_error(
                    self.terminal,
                    live,
                    f"timed out after {timeout:.2f}s waiting for terminal state",
                    eof=False,
                    attach_note=attach_note,
                )
            self._frame_event.clear()
            if self.terminal.synchronized:
                sync_rem = (
                    self.sync_timeout
                    if self._sync_started_at is None
                    else max(0.0, self._sync_started_at + self.sync_timeout - now)
                )
                wait_slice = min(remaining, sync_rem)
            else:
                wait_slice = remaining
            if self._task is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.pump_once(), timeout=wait_slice)
                if self.terminal.synchronized:
                    self._update_sync_timer(time.monotonic())
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._frame_event.wait(),
                        timeout=wait_slice,
                    )
                if self.terminal.synchronized:
                    self._update_sync_timer(time.monotonic())


class PtySession:
    """Async POSIX PTY subprocess session over :class:`~libvt.terminal.Terminal`.

    Outbound bytes from :meth:`send`, :meth:`send_event`, :meth:`send_key`,
    :meth:`paste`, and synchronous terminal query auto-replies share a single
    bounded FIFO queue drained via ``loop.add_writer``, paired with
    :meth:`drain` for high-water backpressure.

    Attributes
    ----------
    process : asyncio.subprocess.Process
        Managed child process attached to the slave PTY.
    terminal : Terminal
        Live :class:`~libvt.terminal.Terminal` fed from ``master_fd``.
    sync_timeout : float
        Watchdog timeout in seconds before expiring stalled ``?2026`` frames.
    write_high_water : int
        High-water mark in bytes above which :meth:`drain` suspends.
    max_write_buffer : int
        Hard cap in bytes above which :meth:`send` raises :exc:`BufferError`.
    """

    process: asyncio.subprocess.Process
    terminal: Terminal
    sync_timeout: float
    write_high_water: int
    max_write_buffer: int

    def __init__(
        self,
        *,
        master_fd: int,
        process: asyncio.subprocess.Process,
        terminal: Terminal,
        sync_timeout: float = 1.0,
        write_high_water: int | None = None,
        max_write_buffer: int = 16_777_216,
    ) -> None:
        """Initialize a :class:`PtySession` around an open non-blocking *master_fd*.

        Parameters
        ----------
        master_fd : int
            Non-blocking master pseudo-terminal file descriptor.
        process : asyncio.subprocess.Process
            Spawned child process bound to the slave PTY.
        terminal : Terminal
            Target :class:`~libvt.terminal.Terminal` instance.
        sync_timeout : float
            Watchdog timeout in seconds before expiring stalled ``?2026`` holds.
        write_high_water : int | None
            Outbound high-water mark in bytes (defaults to
            ``min(65_536, max_write_buffer)``).
        max_write_buffer : int
            Maximum queued outbound bytes before raising :exc:`BufferError`.
        """
        _validate_nonneg_seconds(sync_timeout, "sync_timeout")
        eff_hw, eff_max = _resolve_write_limits(write_high_water, max_write_buffer)
        self._master_fd = master_fd
        self.process = process
        self.terminal = terminal
        self.sync_timeout = sync_timeout
        self.write_high_water = eff_hw
        self.max_write_buffer = eff_max
        self._frame_event = asyncio.Event()
        self._drain_event = asyncio.Event()
        self._drain_event.set()
        self._eof = False
        self._closed = False
        self._fd_closed = False
        self._reader_registered = False
        self._writer_registered = False
        self._write_closed = False
        self._write_error: OSError | None = None
        self._async_write_error: OSError | None = None
        self._read_error: OSError | None = None
        self._write_queue: collections.deque[bytearray] = collections.deque()
        self._write_head_offset: int = 0
        self._write_buffered_bytes: int = 0
        self._sync_started_at: float | None = None
        self._sync_gen_tracked: int | None = None
        self._sync_timer_handle: asyncio.TimerHandle | None = None
        self._sync_timer_gen: int | None = None
        self._last_committed_gen: int = self.terminal.committed_generation
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def eof(self) -> bool:
        """Return ``True`` once the PTY master reaches EOF."""
        return self._eof

    @property
    def write_buffer_size(self) -> int:
        """Return number of outbound bytes currently buffered for ``master_fd``."""
        return self._write_buffered_bytes

    def _cancel_sync_timer(self) -> None:
        if self._sync_timer_handle is not None:
            self._sync_timer_handle.cancel()
            self._sync_timer_handle = None
        self._sync_timer_gen = None

    def _on_sync_timeout(self) -> None:
        self._sync_timer_handle = None
        self._sync_timer_gen = None
        self._sync_frame_wakeup(time.monotonic())

    def _remove_reader(self) -> None:
        if self._loop is not None and self._reader_registered:
            self._reader_registered = False
            with contextlib.suppress(Exception):
                self._loop.remove_reader(self._master_fd)

    def _remove_writer(self) -> None:
        if self._loop is not None and self._writer_registered:
            self._writer_registered = False
            with contextlib.suppress(Exception):
                self._loop.remove_writer(self._master_fd)

    def _ensure_writer(self) -> None:
        if self._writer_registered or self._closed or self._fd_closed:
            return
        loop = self._loop
        if loop is None:
            loop = asyncio.get_running_loop()
            self._loop = loop
        loop.add_writer(self._master_fd, self._on_writable)
        self._writer_registered = True

    def _mark_eof(self) -> None:
        self._eof = True
        self._cancel_sync_timer()
        self._remove_reader()
        self.terminal.expire_synchronized_output()
        self._sync_started_at = None
        self._sync_gen_tracked = None
        self._last_committed_gen = self.terminal.committed_generation
        self._frame_event.set()

    def _sync_frame_wakeup(self, now: float) -> None:
        if self.terminal.synchronized:
            cur_gen = self.terminal.sync_generation
            if self._sync_gen_tracked != cur_gen or self._sync_started_at is None:
                self._sync_gen_tracked = cur_gen
                self._sync_started_at = now
            if (
                now - self._sync_started_at >= self.sync_timeout
                and self.terminal.expire_synchronized_output(cur_gen)
            ):
                self._cancel_sync_timer()
                self._sync_started_at = None
                self._sync_gen_tracked = None
            elif (
                self._loop is not None
                and not self._closed
                and not self._eof
                and self._sync_timer_gen != cur_gen
            ):
                self._cancel_sync_timer()
                delay = max(0.0, self._sync_started_at + self.sync_timeout - now)
                with contextlib.suppress(RuntimeError):
                    self._sync_timer_gen = cur_gen
                    self._sync_timer_handle = self._loop.call_later(
                        delay,
                        self._on_sync_timeout,
                    )
        else:
            self._cancel_sync_timer()
            self._sync_started_at = None
            self._sync_gen_tracked = None
        cur_committed = self.terminal.committed_generation
        if cur_committed != self._last_committed_gen:
            self._last_committed_gen = cur_committed
            self._frame_event.set()

    def _append_queued_bytes(self, mv: memoryview) -> None:
        if (
            self._write_queue
            and (len(self._write_queue) > 1 or self._write_head_offset == 0)
            and len(self._write_queue[-1]) + len(mv) <= _COALESCE_SLAB
        ):
            self._write_queue[-1].extend(mv)
        else:
            self._write_queue.append(bytearray(mv))
        self._write_buffered_bytes += len(mv)

    def _enqueue_write(
        self,
        data: collections.abc.Buffer,
        *,
        is_reply: bool = False,
    ) -> None:
        with memoryview(data) as raw_mv, raw_mv.cast("B") as mv:
            n_total = len(mv)
            if n_total == 0:
                return
            if self._closed or self._fd_closed:
                if is_reply:
                    return
                raise OSError(errno.EBADF, "PTY session is closed")
            if self._write_closed:
                if is_reply:
                    return
                if self._write_error is not None:
                    raise self._write_error
                raise OSError(errno.EIO, "PTY master write end is closed")
            if self._write_buffered_bytes + n_total > self.max_write_buffer:
                if is_reply:
                    return
                msg = (
                    f"PTY write buffer exceeded {self.max_write_buffer} bytes; "
                    "await session.drain()"
                )
                raise BufferError(msg)

            if not self._write_queue:
                pos = 0
                while pos < n_total:
                    try:
                        with mv[pos:] as tail:
                            n = os.write(self._master_fd, tail)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError as exc:
                        self._write_closed = True
                        self._write_error = exc
                        if is_reply:
                            self._async_write_error = exc
                            return
                        raise
                    if n <= 0:
                        break
                    pos += n
                if pos >= n_total:
                    return
                with mv[pos:] as rem:
                    self._write_queue.append(bytearray(rem))
                    self._write_head_offset = 0
                    self._write_buffered_bytes = len(rem)
            else:
                self._append_queued_bytes(mv)

        if self._write_buffered_bytes > 0:
            if self._write_buffered_bytes > self.write_high_water:
                self._drain_event.clear()
            self._ensure_writer()

    def _on_writable(self) -> None:
        if self._closed or self._fd_closed or not self._write_queue:
            self._remove_writer()
            self._drain_event.set()
            return
        while self._write_queue:
            head = self._write_queue[0]
            try:
                with memoryview(head) as mv, mv[self._write_head_offset :] as tail:
                    n = os.write(self._master_fd, tail)
            except (BlockingIOError, InterruptedError):
                if self._write_buffered_bytes <= self.write_high_water:
                    self._drain_event.set()
                return
            except OSError as exc:
                self._write_closed = True
                self._write_error = exc
                self._async_write_error = exc
                self._write_queue.clear()
                self._write_head_offset = 0
                self._write_buffered_bytes = 0
                self._remove_writer()
                self._drain_event.set()
                return
            if n <= 0:
                if self._write_buffered_bytes <= self.write_high_water:
                    self._drain_event.set()
                return
            self._write_head_offset += n
            self._write_buffered_bytes -= n
            if self._write_head_offset >= len(head):
                self._write_queue.popleft()
                self._write_head_offset = 0
            if self._write_buffered_bytes <= self.write_high_water:
                self._drain_event.set()
        self._remove_writer()
        self._drain_event.set()

    async def drain(self) -> None:
        """Pause while outbound buffered bytes exceed :attr:`write_high_water`."""
        if self._write_buffered_bytes <= self.write_high_water:
            if self._write_buffered_bytes > 0:
                self._ensure_writer()
            await asyncio.sleep(0)
        while (
            self._write_buffered_bytes > self.write_high_water
            and not self._closed
            and not self._fd_closed
            and not self._write_closed
        ):
            self._ensure_writer()
            await self._drain_event.wait()
        if (
            self._async_write_error is not None
            and self._async_write_error.errno not in _PEER_EOF_ERRNOS
            and not self._closed
        ):
            raise self._async_write_error

    def _drain_nonblocking_master(self) -> None:
        """Consume buffered non-blocking bytes from ``master_fd`` on teardown."""
        if self._closed or self._fd_closed or self._eof:
            return
        for _ in range(256):
            try:
                chunk = os.read(self._master_fd, 16_384)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as exc:
                if exc.errno in _PEER_EOF_ERRNOS:
                    chunk = b""
                else:
                    break
            if not chunk:
                self._eof = True
                break
            self.terminal.feed(chunk)
        self.terminal.drain_replies()

    def _on_readable(self) -> None:
        if self._closed or self._fd_closed:
            return
        try:
            chunk = os.read(self._master_fd, 16_384)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            if exc.errno in _PEER_EOF_ERRNOS:
                chunk = b""
            else:
                self._read_error = exc
                self._mark_eof()
                return
        if not chunk:
            self._mark_eof()
            return
        self.terminal.feed(chunk)
        replies = self.terminal.drain_replies()
        if replies:
            self._enqueue_write(replies, is_reply=True)
        self._sync_frame_wakeup(time.monotonic())

    def send(self, data: collections.abc.Buffer | str, /) -> None:
        """Enqueue raw *data* bytes (or UTF-8 string) onto the PTY write queue."""
        if isinstance(data, str):
            self._enqueue_write(data.encode("utf-8"))
        else:
            self._enqueue_write(data)

    def send_event(self, event: InputEvent, /) -> None:
        """Encode *event* via :meth:`~libvt.terminal.Terminal.encode` and enqueue."""
        encoded = self.terminal.encode(event)
        if encoded:
            self._enqueue_write(encoded)

    def send_key(self, chord: str, /) -> None:
        """Parse *chord* (e.g. ``"ctrl+c"``, ``"enter"``) and enqueue for the PTY."""
        self.send_event(KeyEvent.parse(chord))

    def paste(self, text: str, /, *, sanitize: bool = False) -> None:
        """Encode *text* as an atomic :class:`~libvt.input.PasteEvent` on the queue."""
        self.send_event(PasteEvent(text=text, sanitize=sanitize))

    def resize(
        self,
        *,
        rows: int,
        cols: int,
        reflow: bool | None = None,
    ) -> None:
        """Update ``TIOCSWINSZ``, resize :attr:`terminal`, and flush ``?2048``."""
        if sys.platform == "win32":
            msg = "PtySession.resize requires POSIX termios"
            raise NotImplementedError(msg)
        _validate_pty_dims(rows, cols)
        if self._closed or self._fd_closed:
            raise OSError(errno.EBADF, "PTY session is closed")
        import fcntl
        import termios

        winsz = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsz)
        self.terminal.resize(rows=rows, cols=cols, reflow=reflow)
        replies = self.terminal.drain_replies()
        if replies:
            self._enqueue_write(replies, is_reply=True)
        self._sync_frame_wakeup(time.monotonic())

    async def wait_for(
        self,
        predicate: collections.abc.Callable[[Snapshot], bool] | str,
        *,
        timeout: float = 2.0,
        attach_note: bool = True,
    ) -> Snapshot:
        """Wait until *predicate* matches :class:`~libvt.terminal.Snapshot`.

        Parameters
        ----------
        predicate : collections.abc.Callable[[Snapshot], bool] | str
            Callable tested against each committed snapshot, or a substring
            searched inside ``snapshot.text``.
        timeout : float
            Maximum wait duration in seconds before raising
            :exc:`~libvt.exc.TerminalTimeoutError`.
        attach_note : bool
            When ``True`` (default), attach multi-line screen and flight
            recorder diagnostics via PEP 678 ``__notes__``.

        Returns
        -------
        Snapshot
            First matching :class:`~libvt.terminal.Snapshot`.

        Raises
        ------
        TerminalTimeoutError
            If the PTY reaches EOF/close before matching or *timeout* expires.
        """
        _validate_nonneg_seconds(timeout, "timeout")
        check = (
            (lambda snap: predicate in snap.text)
            if isinstance(predicate, str)
            else predicate
        )
        deadline = time.monotonic() + timeout
        while True:
            if self._read_error is not None:
                err = self._read_error
                self._read_error = None
                raise err
            now = time.monotonic()
            if self.terminal.synchronized:
                self._sync_frame_wakeup(now)
            ended = self._eof or self._closed
            snap = self.terminal.snapshot() if ended else self.terminal.committed
            if check(snap):
                return snap
            if ended:
                reason = (
                    "PTY reached EOF before predicate matched"
                    if self._eof
                    else "PTY session closed before predicate matched"
                )
                raise _make_timeout_error(
                    self.terminal,
                    snap,
                    reason,
                    eof=self._eof,
                    attach_note=attach_note,
                )
            remaining = deadline - now
            if remaining <= 0:
                live = self.terminal.snapshot()
                raise _make_timeout_error(
                    self.terminal,
                    live,
                    f"timed out after {timeout:.2f}s waiting for PTY state",
                    eof=False,
                    attach_note=attach_note,
                )
            self._frame_event.clear()
            if self.terminal.synchronized:
                sync_rem = (
                    self.sync_timeout
                    if self._sync_started_at is None
                    else max(0.0, self._sync_started_at + self.sync_timeout - now)
                )
                wait_slice = min(remaining, sync_rem)
            else:
                wait_slice = remaining
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._frame_event.wait(), timeout=wait_slice)

    async def aclose(self) -> int:
        """Signal the owned process group, drain PTY output, and close master fd."""
        try:
            if self.process.returncode is None:
                if sys.platform == "win32":
                    with contextlib.suppress(ProcessLookupError):
                        self.process.terminate()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.process.wait(), timeout=0.4)
                    if self.process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            self.process.kill()
                        await self.process.wait()
                else:
                    pid = self.process.pid
                    for sig, wait_s in (
                        (signal.SIGHUP, 0.4),
                        (signal.SIGTERM, 0.4),
                    ):
                        if self.process.returncode is not None:
                            break
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(pid, sig)
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(
                                self.process.wait(),
                                timeout=wait_s,
                            )
                    if self.process.returncode is None:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(pid, signal.SIGKILL)
                        with contextlib.suppress(ProcessLookupError):
                            self.process.kill()
                        await self.process.wait()
        except BaseException:
            if self.process.returncode is None:
                if sys.platform != "win32":
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
            raise
        finally:
            was_closed = self._closed
            self._cancel_sync_timer()
            self._remove_reader()
            self._remove_writer()
            self._drain_nonblocking_master()
            self._closed = True
            self._write_queue.clear()
            self._write_head_offset = 0
            self._write_buffered_bytes = 0
            self._drain_event.set()
            if not self._fd_closed:
                self._fd_closed = True
                with contextlib.suppress(OSError):
                    os.close(self._master_fd)
            self.terminal.expire_synchronized_output()
            self._sync_started_at = None
            self._sync_gen_tracked = None
            if not was_closed:
                self.terminal.notify_process_exit()
            self._last_committed_gen = self.terminal.committed_generation
            self._frame_event.set()

        return self.process.returncode or 0


@contextlib.asynccontextmanager
async def open_pty(
    argv: collections.abc.Sequence[str],
    *,
    rows: int = 24,
    cols: int = 80,
    scrollback: int = 2_000,
    env: collections.abc.Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    tty_mode: TtyMode = "cooked",
    python_executable: str | os.PathLike[str] | None = None,
    profile: Profile = DEFAULT_PROFILE,
    policy: Policy = DEFAULT_POLICY,
    limits: Limits = DEFAULT_LIMITS,
    sync_timeout: float = 1.0,
    write_high_water: int | None = None,
    max_write_buffer: int = 16_777_216,
) -> collections.abc.AsyncGenerator[PtySession]:
    """Spawn *argv* on a POSIX PTY driven by :class:`~libvt.terminal.Terminal`.

    Parameters
    ----------
    argv : collections.abc.Sequence[str]
        Command and arguments executed via ``os.execvp``.
    rows : int
        Initial PTY height in rows (``1..65535``).
    cols : int
        Initial PTY width in columns (``1..65535``).
    scrollback : int
        Primary-screen scrollback line capacity (``>= 0``).
    env : collections.abc.Mapping[str, str] | None
        Environment variables passed to the child process (defaults to
        ``os.environ`` with ``TERM=xterm-256color``).
    cwd : str | os.PathLike[str] | None
        Working directory for the child process.
    tty_mode : TtyMode
        Initial slave terminal mode (``"cooked"``, ``"cbreak"``, or ``"raw"``).
    python_executable : str | os.PathLike[str] | None
        Python interpreter used for the ``TIOCSCTTY`` pre-exec trampoline.
    profile : Profile
        Terminal compatibility profile.
    policy : Policy
        Host security and query policy.
    limits : Limits
        Parser and state resource ceilings.
    sync_timeout : float
        Watchdog timeout in seconds before expiring stalled ``?2026`` frames.
    write_high_water : int | None
        High-water mark in bytes for outbound PTY writes (defaults to
        ``min(65_536, max_write_buffer)``).
    max_write_buffer : int
        Maximum outbound bytes queued before :meth:`PtySession.send` raises
        :exc:`BufferError`.

    Yields
    ------
    PtySession
        Active session connected to the child process and master PTY fd.

    Raises
    ------
    NotImplementedError
        On platforms lacking POSIX ``pty.openpty()`` (Windows).
    ValueError
        If *argv*, dimensions, *tty_mode*, *sync_timeout*, or buffer sizes are
        invalid.
    OSError
        If PTY allocation or ``execvp`` fails.
    """
    if sys.platform == "win32":
        msg = "open_pty requires POSIX pty.openpty()"
        raise NotImplementedError(msg)
    if not argv:
        msg = "argv must not be empty"
        raise ValueError(msg)
    _validate_pty_dims(rows, cols)
    if (
        isinstance(scrollback, bool)
        or not isinstance(scrollback, int)
        or scrollback < 0
    ):
        msg = f"scrollback must be an int >= 0, got {scrollback!r}"
        raise ValueError(msg)
    if tty_mode not in ("cooked", "cbreak", "raw"):
        msg = f"unsupported tty_mode: {tty_mode!r}"
        raise ValueError(msg)
    _validate_nonneg_seconds(sync_timeout, "sync_timeout")
    eff_hw, eff_max = _resolve_write_limits(write_high_water, max_write_buffer)

    if python_executable is None:
        if getattr(sys, "frozen", False) or not sys.executable:
            msg = (
                "open_pty requires python_executable in frozen or embedded "
                "Python runtimes"
            )
            raise RuntimeError(msg)
        py_bin = sys.executable
    else:
        py_bin = os.fspath(python_executable)

    import fcntl
    import pty
    import termios
    import tty

    loop = asyncio.get_running_loop()
    master_fd, slave_fd = pty.openpty()
    err_r, err_w = os.pipe()
    slave_closed = False
    err_w_closed = False
    master_handed_off = False
    proc: asyncio.subprocess.Process | None = None
    try:
        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        if tty_mode == "cbreak":
            tty.setcbreak(slave_fd)
        elif tty_mode == "raw":
            tty.setraw(slave_fd)
        os.set_blocking(master_fd, False)
        os.set_blocking(err_r, False)
        child_env = dict(os.environ if env is None else env)
        child_env.setdefault("TERM", "xterm-256color")
        proc = await asyncio.create_subprocess_exec(
            py_bin,
            "-I",
            "-S",
            "-c",
            _PTY_TRAMPOLINE,
            str(err_w),
            *argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=child_env,
            start_new_session=True,
            pass_fds=(err_w,),
            close_fds=True,
        )
        os.close(slave_fd)
        slave_closed = True
        os.close(err_w)
        err_w_closed = True

        err_payload = await _read_cloexec_pipe(loop, err_r)
        if err_payload != b"\x00":
            await proc.wait()
            _raise_exec_error(err_payload, argv[0], errno.ENOEXEC)

        eff_policy = policy
        if policy.osc7_hosts is None:
            fqdn = socket.gethostname().lower()
            short = fqdn.partition(".")[0]
            eff_policy = dataclasses.replace(
                policy,
                osc7_hosts=DEFAULT_OSC7_HOSTS | frozenset({fqdn, short}),
            )

        recorder = FlightRecorder(max_bytes=limits.max_recorder_bytes)
        terminal = Terminal(
            rows=rows,
            cols=cols,
            scrollback=scrollback,
            profile=profile,
            policy=eff_policy,
            limits=limits,
            recorder=recorder,
        )
        session = PtySession(
            master_fd=master_fd,
            process=proc,
            terminal=terminal,
            sync_timeout=sync_timeout,
            write_high_water=eff_hw,
            max_write_buffer=eff_max,
        )
        session._loop = loop
        master_handed_off = True
    except BaseException:
        if proc is not None and proc.returncode is None:
            if sys.platform != "win32":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        raise
    finally:
        if not slave_closed:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        if not err_w_closed:
            with contextlib.suppress(OSError):
                os.close(err_w)
        with contextlib.suppress(OSError):
            os.close(err_r)
        if not master_handed_off:
            with contextlib.suppress(OSError):
                os.close(master_fd)

    try:
        loop.add_reader(master_fd, session._on_readable)
        session._reader_registered = True
        yield session
    finally:
        await session.aclose()


__all__ = [
    "AsyncTerminalStream",
    "PtySession",
    "ReplyWriter",
    "TtyMode",
    "open_pty",
]
