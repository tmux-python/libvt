"""Transport-only ``asyncio`` adapter over :class:`~libvt.terminal.Terminal`."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import time
import typing as t

from libvt.exc import TerminalTimeoutError
from libvt.terminal import Snapshot, Terminal


class AsyncTerminalStream:
    """Drive a sans-I/O :class:`~libvt.terminal.Terminal` from ``asyncio`` streams.

    Coalesces frame wakeups across DEC mode ``?2026`` synchronized-output
    batches (bounded by *sync_timeout*) and automatically flushes
    :meth:`~libvt.terminal.Terminal.drain_replies` when a ``StreamWriter`` is
    attached.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None = None,
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
        self._task: asyncio.Task[None] | None = None

    @property
    def eof(self) -> bool:
        """Return ``True`` once ``reader`` reaches end-of-stream."""
        return self._eof

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

        now = time.monotonic()
        if self.terminal.synchronized:
            if self._sync_started_at is None:
                self._sync_started_at = now
            if now - self._sync_started_at >= self.sync_timeout:
                # Release held frame when ?2026 exceeds sync_timeout.
                self.terminal._held_snapshot = None
                self._sync_started_at = now
                self._frame_event.set()
        else:
            self._sync_started_at = None
            self._frame_event.set()
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
            snap = self.terminal.snapshot() if self._eof else self.terminal.committed
            if check(snap):
                return snap
            if self._eof:
                msg = (
                    f"stream reached EOF before predicate matched; "
                    f"screen text={snap.text!r}"
                )
                raise TerminalTimeoutError(msg)
            remaining = deadline - time.monotonic()
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
            if self._task is None:
                try:
                    await asyncio.wait_for(self.pump_once(), timeout=remaining)
                except TimeoutError as exc:
                    msg = f"timed out after {timeout:.2f}s waiting for stream bytes"
                    raise TerminalTimeoutError(msg) from exc
            else:
                wait_slice = (
                    min(remaining, self.sync_timeout)
                    if self.terminal.synchronized
                    else remaining
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._frame_event.wait(),
                        timeout=wait_slice,
                    )
                if (
                    self.terminal.synchronized
                    and self._sync_started_at is not None
                    and time.monotonic() - self._sync_started_at >= self.sync_timeout
                ):
                    self.terminal._held_snapshot = None


__all__ = [
    "AsyncTerminalStream",
]
