(topics-sync-and-asyncio)=

# Synchronized Output & `asyncio`

Terminal applications wrap screen redraws in DEC private mode `?2026`
(`CSI ? 2026 h` … `CSI ? 2026 l`) to mark the start and end of a frame.

## Synchronized redraws and `feed_until`

During a synchronized redraw,
{attr}`term.committed <libvt.terminal.Terminal.committed>` retains the previous
frame. {meth}`term.snapshot() <libvt.terminal.Terminal.snapshot>` exposes the
screen as it changes:

```python
>>> import libvt
>>> term = libvt.Terminal(rows=2, cols=16)
>>> term.feed(b"stable")
>>> term.feed(b"\x1b[?2026h\x1b[2J\x1b[Hredrawing")
>>> term.committed.text
'stable'
>>> term.snapshot().text
'redrawing'
>>> term.feed(b"\x1b[?2026l")
>>> term.committed.text
'redrawing'
```

{meth}`~libvt.terminal.Terminal.feed_until` steps through concatenated output
and stops at `?2026l` ({attr}`StopOn.SYNC_END <libvt.terminal.StopOn.SYNC_END>`),
`OSC 133` prompt boundaries, or `OSC 7501` status transitions:

```python
>>> batch = b"\x1b[?2026h\x1b[Hf1\x1b[?2026l\x1b[?2026h\x1b[Hf2\x1b[?2026l"
>>> t = libvt.Terminal(rows=2, cols=8)
>>> step = t.feed_until(batch, stop=libvt.StopOn.SYNC_END)
>>> t.text, step.stopped_on
('f1', <StopOn.SYNC_END: 8>)
```

If an application exits or stalls mid-frame without sending `?2026l`,
{meth}`~libvt.terminal.Terminal.expire_synchronized_output` releases the hold
when {attr}`~libvt.terminal.Terminal.sync_generation` still matches the stalled
batch.

## Asyncio streams and POSIX PTYs

{class}`~libvt.aio.AsyncTerminalStream` feeds an {class}`asyncio.StreamReader`
into a terminal and can write terminal replies back to a stream.
{func}`~libvt.aio.open_pty` starts a child process in a POSIX PTY and yields a
{class}`~libvt.aio.PtySession` (Linux, macOS, and BSD; raises
{exc}`NotImplementedError` on Windows).

### Waiting for screen state

{meth}`AsyncTerminalStream.wait_for() <libvt.aio.AsyncTerminalStream.wait_for>`
and {meth}`PtySession.wait_for() <libvt.aio.PtySession.wait_for>` test a
substring or `Callable[[Snapshot], bool]` predicate each time
{attr}`~libvt.terminal.Terminal.committed_generation` advances, expiring
unclosed `?2026` frames after `sync_timeout` seconds. On timeout or premature
`EOF`, both raise {exc}`~libvt.exc.TerminalTimeoutError` with the boxed screen
and recent decoded input attached via `__notes__`:

```python
>>> import asyncio
>>> from libvt.aio import AsyncTerminalStream
>>> async def demo() -> str:
...     reader = asyncio.StreamReader()
...     reader.feed_data(b"\x1b[?2026hPrompt>\x1b[?2026l")
...     reader.feed_eof()
...     async with AsyncTerminalStream(reader, rows=2, cols=16) as stream:
...         snap = await stream.wait_for("Prompt>")
...         return snap.text
>>> asyncio.run(demo())
'Prompt>'
```

### Write queues and backpressure

{meth}`PtySession.send() <libvt.aio.PtySession.send>`,
{meth}`~libvt.aio.PtySession.send_event`,
{meth}`~libvt.aio.PtySession.send_key`, {meth}`~libvt.aio.PtySession.paste`,
and automatic query replies share a bounded FIFO queue drained via
`loop.add_writer`. Call `await session.drain()`
({meth}`~libvt.aio.PtySession.drain`) to pause while queued bytes exceed
`write_high_water`; exceeding `max_write_buffer` raises {exc}`BufferError`.

### Resizing

{meth}`PtySession.resize() <libvt.aio.PtySession.resize>` updates the kernel
window size via `TIOCSWINSZ`, resizes `session.terminal`, and queues any
`?2048` in-band resize notification.

### Cleanup

Leaving `async with` or calling
{meth}`AsyncTerminalStream.aclose() <libvt.aio.AsyncTerminalStream.aclose>` /
{meth}`PtySession.aclose() <libvt.aio.PtySession.aclose>` expires any active
`?2026` hold. `PtySession.aclose()` signals the child process group (`SIGHUP`,
`SIGTERM`, then `SIGKILL`), drains remaining non-blocking output from the
master descriptor, closes it, and calls
{meth}`~libvt.terminal.Terminal.notify_process_exit`.

## Concurrency and thread ownership

Every method call and attribute read on a {class}`~libvt.terminal.Terminal`
instance (including capturing state via
{meth}`~libvt.terminal.Terminal.snapshot` or
{attr}`~libvt.terminal.Terminal.committed`) belongs to a single owning thread
or {mod}`asyncio` event loop unless externally synchronized. Returned
{class}`~libvt.terminal.Snapshot` values are immutable and safe to pass across
threads without locking.
