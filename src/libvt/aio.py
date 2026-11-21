"""Transport-only ``asyncio`` stream and POSIX PTY adapters over :class:`Terminal`."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import os
import struct
import sys
import time
import typing as t

from libvt.config import (
    DEFAULT_LIMITS,
    DEFAULT_POLICY,
    DEFAULT_PROFILE,
    Limits,
    Policy,
    Profile,
)
from libvt.exc import TerminalTimeoutError
from libvt.input import InputEvent, KeyEvent, PasteEvent
from libvt.terminal import FlightRecorder, Snapshot, Terminal


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


class AsyncTerminalStream:
    """Drive a sans-I/O :class:`~libvt.terminal.Terminal` from ``asyncio`` streams.

    Coalesces frame wakeups across DEC mode ``?2026`` synchronized-output
    batches (bounded by *sync_timeout* using generation-safe expiry) and
    automatically flushes :meth:`~libvt.terminal.Terminal.drain_replies` when a
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
        self.reader = reader
        self.writer = writer
        self.terminal = (
            terminal if terminal is not None else Terminal(rows=rows, cols=cols)
        )
        self.chunk_size = chunk_size
        self.sync_timeout = sync_timeout
        self._frame_event = asyncio.Event()
        self._eof = False
        self._sync_started_at: float | None = None
        self._sync_gen_tracked: int | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def eof(self) -> bool:
        """Return ``True`` once ``reader`` reaches end-of-stream."""
        return self._eof

    def _update_sync_timer(self, now: float) -> None:
        if self.terminal.synchronized:
            cur_gen = self.terminal.sync_generation
            if self._sync_gen_tracked != cur_gen or self._sync_started_at is None:
                self._sync_gen_tracked = cur_gen
                self._sync_started_at = now
            elif (
                now - self._sync_started_at >= self.sync_timeout
                and self.terminal.expire_synchronized_output(cur_gen)
            ):
                self._sync_started_at = None
                self._sync_gen_tracked = None
                self._frame_event.set()
        else:
            self._sync_started_at = None
            self._sync_gen_tracked = None
            self._frame_event.set()

    async def pump_once(self) -> bool:
        """Read and feed one chunk; return ``False`` on EOF."""
        chunk = await self.reader.read(self.chunk_size)
        if not chunk:
            self._eof = True
            self._frame_event.set()
            return False
        self.terminal.feed(chunk)
        replies = self.terminal.drain_replies()
        if replies and self.writer is not None:
            self.writer.write(replies)
            await self.writer.drain()
        self._update_sync_timer(time.monotonic())
        return True

    async def _pump_loop(self) -> None:
        while await self.pump_once():
            pass

    def start(self) -> asyncio.Task[None]:
        """Start background stream pumping on the running event loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump_loop())
        return self._task

    async def aclose(self) -> None:
        """Cancel background pump task and close the writer if present."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()

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
    ) -> Snapshot:
        """Wait until *predicate* matches the committed :class:`Snapshot`."""
        check = (
            (lambda snap: predicate in snap.text)
            if isinstance(predicate, str)
            else predicate
        )
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            if self.terminal.synchronized:
                self._update_sync_timer(now)
            snap = self.terminal.snapshot() if self._eof else self.terminal.committed
            if check(snap):
                return snap
            if self._eof:
                msg = (
                    f"stream reached EOF before predicate matched; "
                    f"screen text={snap.text!r}"
                )
                raise TerminalTimeoutError(msg)
            remaining = deadline - now
            if remaining <= 0:
                live = self.terminal.snapshot()
                if check(live):
                    return live
                msg = (
                    f"timed out after {timeout:.2f}s waiting for terminal state; "
                    f"screen text={live.text!r}"
                )
                raise TerminalTimeoutError(msg)
            self._frame_event.clear()
            wait_slice = (
                min(remaining, self.sync_timeout)
                if self.terminal.synchronized
                else remaining
            )
            if self._task is None:
                try:
                    await asyncio.wait_for(self.pump_once(), timeout=wait_slice)
                except TimeoutError as exc:
                    if self.terminal.synchronized:
                        self._update_sync_timer(time.monotonic())
                        continue
                    msg = f"timed out after {timeout:.2f}s waiting for stream bytes"
                    raise TerminalTimeoutError(msg) from exc
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._frame_event.wait(),
                        timeout=wait_slice,
                    )
                if self.terminal.synchronized:
                    self._update_sync_timer(time.monotonic())


class PtySession:
    """Async POSIX pseudo-terminal subprocess session backed by :class:`Terminal`."""

    def __init__(
        self,
        *,
        master_fd: int,
        process: asyncio.subprocess.Process,
        terminal: Terminal,
        sync_timeout: float = 1.0,
    ) -> None:
        self._master_fd = master_fd
        self.process = process
        self.terminal = terminal
        self.sync_timeout = sync_timeout
        self._frame_event = asyncio.Event()
        self._eof = False
        self._closed = False
        self._sync_started_at: float | None = None
        self._sync_gen_tracked: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def eof(self) -> bool:
        """Return ``True`` once the PTY master reaches EOF."""
        return self._eof

    def _on_readable(self) -> None:
        if self._closed:
            return
        try:
            chunk = os.read(self._master_fd, 16_384)
        except OSError:
            chunk = b""
        if not chunk:
            self._eof = True
            if self._loop is not None:
                self._loop.remove_reader(self._master_fd)
            self._frame_event.set()
            return
        self.terminal.feed(chunk)
        replies = self.terminal.drain_replies()
        if replies:
            with contextlib.suppress(OSError):
                os.write(self._master_fd, replies)
        now = time.monotonic()
        if self.terminal.synchronized:
            cur_gen = self.terminal.sync_generation
            if self._sync_gen_tracked != cur_gen or self._sync_started_at is None:
                self._sync_gen_tracked = cur_gen
                self._sync_started_at = now
        else:
            self._sync_started_at = None
            self._sync_gen_tracked = None
            self._frame_event.set()

    def send(self, data: bytes | str, /) -> None:
        """Write raw *data* bytes (or UTF-8 string) to the PTY master."""
        raw = data.encode("utf-8") if isinstance(data, str) else data
        os.write(self._master_fd, raw)

    def send_event(self, event: InputEvent, /) -> None:
        """Encode *event* via :meth:`Terminal.encode` and write to the PTY."""
        encoded = self.terminal.encode(event)
        if encoded:
            os.write(self._master_fd, encoded)

    def send_key(self, chord: str, /) -> None:
        """Parse *chord* (e.g. ``"ctrl+c"``, ``"enter"``) and send to the PTY."""
        self.send_event(KeyEvent.parse(chord))

    def paste(self, text: str, /, *, sanitize: bool = False) -> None:
        """Encode *text* as a :class:`PasteEvent` and write to the PTY."""
        self.send_event(PasteEvent(text=text, sanitize=sanitize))

    def resize(self, *, rows: int, cols: int, reflow: bool = True) -> None:
        """Update kernel ``TIOCSWINSZ`` and resize :attr:`terminal`."""
        import fcntl
        import termios

        winsz = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsz)
        self.terminal.resize(rows=rows, cols=cols, reflow=reflow)

    async def wait_for(
        self,
        predicate: collections.abc.Callable[[Snapshot], bool] | str,
        *,
        timeout: float = 2.0,
    ) -> Snapshot:
        """Wait until *predicate* matches the committed :class:`Snapshot`."""
        check = (
            (lambda snap: predicate in snap.text)
            if isinstance(predicate, str)
            else predicate
        )
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            if (
                self.terminal.synchronized
                and self._sync_started_at is not None
                and now - self._sync_started_at >= self.sync_timeout
            ):
                self.terminal.expire_synchronized_output(self._sync_gen_tracked)
                self._sync_started_at = None
                self._sync_gen_tracked = None
            snap = self.terminal.snapshot() if self._eof else self.terminal.committed
            if check(snap):
                return snap
            if self._eof:
                msg = (
                    f"PTY reached EOF before predicate matched; "
                    f"screen text={snap.text!r}"
                )
                raise TerminalTimeoutError(msg)
            remaining = deadline - now
            if remaining <= 0:
                live = self.terminal.snapshot()
                if check(live):
                    return live
                msg = (
                    f"timed out after {timeout:.2f}s waiting for PTY state; "
                    f"screen text={live.text!r}"
                )
                raise TerminalTimeoutError(msg)
            self._frame_event.clear()
            wait_slice = (
                min(remaining, self.sync_timeout)
                if self.terminal.synchronized
                else remaining
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._frame_event.wait(), timeout=wait_slice)

    async def aclose(self) -> int:
        """Stop reading, close PTY master fd, and terminate/reap child process."""
        if not self._closed:
            self._closed = True
            if self._loop is not None:
                with contextlib.suppress(Exception):
                    self._loop.remove_reader(self._master_fd)
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.0)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.terminal.notify_process_exit()
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
    profile: Profile = DEFAULT_PROFILE,
    policy: Policy = DEFAULT_POLICY,
    limits: Limits = DEFAULT_LIMITS,
    sync_timeout: float = 1.0,
) -> collections.abc.AsyncGenerator[PtySession]:
    """Spawn *argv* on a POSIX pseudo-terminal driven by :class:`Terminal`."""
    if sys.platform == "win32":
        msg = "open_pty requires POSIX pty.openpty()"
        raise NotImplementedError(msg)

    import fcntl
    import pty
    import termios

    master_fd, slave_fd = pty.openpty()
    try:
        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        os.set_blocking(master_fd, False)
        child_env = dict(os.environ if env is None else env)
        child_env.setdefault("TERM", "xterm-256color")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)

    recorder = FlightRecorder(max_bytes=limits.max_recorder_bytes)
    terminal = Terminal(
        rows=rows,
        cols=cols,
        scrollback=scrollback,
        profile=profile,
        policy=policy,
        limits=limits,
        recorder=recorder,
    )
    session = PtySession(
        master_fd=master_fd,
        process=proc,
        terminal=terminal,
        sync_timeout=sync_timeout,
    )
    loop = asyncio.get_running_loop()
    session._loop = loop
    loop.add_reader(master_fd, session._on_readable)
    try:
        yield session
    finally:
        await session.aclose()


__all__ = [
    "AsyncTerminalStream",
    "PtySession",
    "ReplyWriter",
    "open_pty",
]
