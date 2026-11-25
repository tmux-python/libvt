(topics-architecture)=

# Architecture & Grid Storage

{class}`~libvt.parser.Parser` ({mod}`libvt.parser`) recognizes text and control
sequences. {class}`~libvt.screen.Screen` ({mod}`libvt.screen`) stores cells,
cursor position, and scrollback. {class}`~libvt.terminal.Terminal`
({mod}`libvt.terminal`) applies sequences across primary and alternate screens
and prepares replies and events. The adapters in {mod}`libvt.aio` connect
`Terminal` to `asyncio` streams and POSIX child processes.

{meth}`Terminal.feed() <libvt.terminal.Terminal.feed>` processes supplied
bytes. The caller provides incoming data, retrieves queued replies with
{meth}`~libvt.terminal.Terminal.drain_replies`, and decides when to read or
write the transport (a sans-I/O boundary).

Importing `libvt` loads the emulation, input, formatting, and `tmux` modules.
{mod}`libvt.aio`, {mod}`libvt.testing`, and {mod}`libvt.pytest_plugin` are
imported separately when used, and POSIX `pty` / `termios` modules are loaded
inside {func}`~libvt.aio.open_pty`.

## Configuration

{class}`~libvt.config.Profile` selects terminal behavior and reported
capabilities. {class}`~libvt.config.Policy` controls which operations are
allowed. {class}`~libvt.config.Limits` sets bounds on buffers and other stored
data:

```python
>>> import libvt
>>> term = libvt.Terminal(
...     rows=3,
...     cols=5,
...     profile=libvt.TMUX_PROFILE,
...     policy=libvt.Policy(strict=False),
...     limits=libvt.Limits(max_osc_bytes=8192),
... )
>>> term.feed(b"12345\x1b[1DX")
>>> term[0].text
'1234X'
```

## Viewport and scrollback storage

Active viewport rows store a single uniform 64-bit attribute word while a line
contains plain default-styled text, allocating a per-column `array('Q')`
attribute array only when styled or wide cells are written. Lines scrolled off
the top of the primary screen freeze into immutable run-length-encoded rows on
{attr}`~libvt.terminal.Terminal.history`, and resizing the primary screen
reflows soft-wrapped lines across history and the viewport:

```python
>>> term = libvt.Terminal(rows=2, cols=12, scrollback=10)
>>> term.feed(b"\x1b[32mline-0\x1b[0m\r\nline-1\r\nline-2")
>>> [r.text for r in term.history]
['line-0']
>>> term.history[0].runs[0].style.fg
<AnsiColor.GREEN: 2>
```

## Unicode 17 width and grapheme clustering

`libvt` bundles two-stage lookup tables generated from Unicode 17.0.0 data
(`EastAsianWidth`, UAX #29 `GraphemeBreakProperty`, `emoji-data`, and
`DerivedCoreProperties` `InCB`). {func}`~libvt.display_width` computes column
widths, and enabling DEC mode `?2027`
({attr}`~libvt.modes.Mode.GRAPHEME_CLUSTERING`) groups ZWJ emoji, Regional
Indicator flag pairs, Indic conjuncts, and `VS15` / `VS16` variation sequences
into display cells:

```python
>>> term = libvt.Terminal(rows=2, cols=10)
>>> term.feed(b"\x1b[?2027h")
>>> term.feed_str("🧑\u200d🌾ok")
>>> term[0, 0].width, term.cursor.col
(2, 4)
```

For design rationale and memory measurements, see {doc}`../project/adr/index`.
