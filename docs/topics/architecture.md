(topics-architecture)=

# Architecture & Grid Storage

`libvt` separates terminal emulation into four acyclic layers so callers
import only the machinery they need:

1. **Streaming parser** ({mod}`libvt.parser`): VT500 byte state machine
   with colon-subparameter `CSI` bitmasks, incremental UTF-8 decoding,
   `STRING_ESC` 7-bit string termination, optional Latin-1 8-bit `C1`
   wire parsing, and `DCS tmux;` `ESC ESC` collapsing.
2. **Grid and screen model** ({mod}`libvt.screen`): Zero-based
   `(row, col)` viewport with lazy 64-bit `array('Q')` attribute words,
   wide-spacer boundary healing, run-length-encoded scrollback rows, and
   primary-screen soft-wrap resize reflow.
3. **Sans-I/O terminal facade** ({mod}`libvt.terminal`,
   {mod}`libvt.status`, {mod}`libvt.input`, {mod}`libvt.codec`):
   Coordinates dual primary/alternate screens, `Profile` / `Policy` /
   `Limits`, `OSC 7501` status records, outbound replies, and input
   encoding with zero system calls.
4. **Transport and test edges** ({mod}`libvt.aio`,
   {mod}`libvt.testing`, {mod}`libvt.pytest_plugin`): Optional
   `asyncio` stream/PTY runners and pytest diagnostics.

## Configuration split

{class}`~libvt.config.Profile`, {class}`~libvt.config.Policy`, and
{class}`~libvt.config.Limits` separate compatibility identity from
security gates and resource caps:

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

## Two-tier viewport and RLE scrollback

Active viewport rows keep plain ASCII lines at a single uniform 64-bit
word and promote to `array('Q')` only when styled or wide cells appear.
Evicted scrollback lines freeze into immutable run-length-encoded rows
exposed through {attr}`~libvt.terminal.Terminal.history`:

```python
>>> term = libvt.Terminal(rows=2, cols=12, scrollback=10)
>>> term.feed(b"\x1b[32mline-0\x1b[0m\r\nline-1\r\nline-2")
>>> [r.text for r in term.history]
['line-0']
>>> term.history[0].runs[0].style.fg
<AnsiColor.GREEN: 2>
```

For rationale and memory measurements, see {doc}`../project/adr/index`.
