(topics-sync-and-asyncio)=

# Synchronized Output & `asyncio`

Modern TUI applications wrap screen redraws in DEC private mode `?2026`
(`CSI ? 2026 h` … `CSI ? 2026 l`) so observers never inspect half-drawn
frames.

## Copy-on-write `?2026` holds and `feed_until`

While {attr}`~libvt.terminal.Terminal.synchronized` is `True`,
{attr}`~libvt.terminal.Terminal.committed` returns the pre-batch
{class}`~libvt.terminal.Snapshot` while
{meth}`~libvt.terminal.Terminal.snapshot` reflects live in-progress
mutations:

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

{meth}`~libvt.terminal.Terminal.feed_until` steps through concatenated
captures and stops cleanly at `?2026l`
({attr}`StopOn.SYNC_END <libvt.terminal.StopOn.SYNC_END>`), `OSC 133`
prompt boundaries, or `OSC 7501` status transitions:

```python
>>> batch = b"\x1b[?2026h\x1b[Hf1\x1b[?2026l\x1b[?2026h\x1b[Hf2\x1b[?2026l"
>>> t = libvt.Terminal(rows=2, cols=8)
>>> step = t.feed_until(batch, stop=libvt.StopOn.SYNC_END)
>>> t.text, step.stopped_on
('f1', <StopOn.SYNC_END: 8>)
```

If an application crashes mid-frame without closing `?2026l`,
{meth}`~libvt.terminal.Terminal.expire_synchronized_output` releases the
hold only when {attr}`~libvt.terminal.Terminal.sync_generation` still matches
the stalled batch.

## `AsyncTerminalStream` and `open_pty`

{mod}`libvt.aio` wraps sans-I/O terminals for {mod}`asyncio` readers/writers
and POSIX pseudo-terminals without adding background threads to the core:

- {class}`~libvt.aio.AsyncTerminalStream` pumps an
  {class}`asyncio.StreamReader`, flushes
  {meth}`~libvt.terminal.Terminal.drain_replies` to an optional writer, wakes
  {meth}`~libvt.aio.AsyncTerminalStream.wait_for` whenever
  {attr}`~libvt.terminal.Terminal.committed_generation` advances (including
  straddling `?2026l` … `?2026h` chunks), and expires unclosed `?2026` holds on
  `EOF` and {meth}`~libvt.aio.AsyncTerminalStream.aclose`.
- {func}`~libvt.aio.open_pty` spawns a child process attached to a
  controlling {func}`pty.openpty` terminal (`TIOCSCTTY`, `TIOCSWINSZ`,
  default `SIGPIPE`, and selectable `tty_mode`), queues outbound user input and
  auto-replies on a bounded FIFO drained via `loop.add_writer` and
  {meth}`~libvt.aio.PtySession.drain`, flushes `?2048` replies on
  {meth}`~libvt.aio.PtySession.resize`, and calls
  {meth}`~libvt.terminal.Terminal.notify_process_exit` on teardown.

## Concurrency and thread ownership

Every method call and attribute read on a {class}`~libvt.terminal.Terminal`
instance (including capturing state via
{meth}`~libvt.terminal.Terminal.snapshot` or
{attr}`~libvt.terminal.Terminal.committed`) belongs to a single owning thread
or {mod}`asyncio` event loop unless externally synchronized. Returned
{class}`~libvt.terminal.Snapshot` values are immutable and safe to pass across
threads without locking.
