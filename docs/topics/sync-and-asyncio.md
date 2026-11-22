(sync-and-asyncio)=

# Synchronized Output and Asyncio

Modern terminal applications wrap redraws in DEC private mode `?2026`
(`CSI ? 2026 h ... CSI ? 2026 l`). Reading `term.text` mid-batch observes
half-drawn screens, whereas {attr}`~libvt.terminal.Terminal.committed`
retains the last completed {class}`~libvt.terminal.Snapshot` until
`CSI ? 2026 l` arrives:

```python
>>> from libvt import StopOn, Terminal
>>> term = Terminal(rows=4, cols=20)
>>> term.feed(b"frame-1")
>>> term.feed(b"\x1b[?2026h\x1b[2J\x1b[Hpartial")
>>> term.committed.text
'frame-1'
>>> term.snapshot().text
'partial'
>>> step = term.feed_until(b"-done\x1b[?2026ltrailing", stop=StopOn.SYNC_END)
>>> term.committed.text
'partial-done'
>>> step.stopped_on == StopOn.SYNC_END
True
```

For `asyncio` transports (`StreamReader` / `StreamWriter`), import
{class}`~libvt.aio.AsyncTerminalStream` from {mod}`libvt.aio`. It pumps
bytes in the background, flushes `drain_replies()` back to the writer, and
holds `wait_for()` notifications while `?2026` is active up to
`sync_timeout`.
