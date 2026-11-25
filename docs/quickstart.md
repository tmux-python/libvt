(quickstart)=

# Quickstart

## Requirements

- Python >= 3.12 (CPython and PyPy)
- No runtime dependencies

## Installation

```console
$ uv add "libvt>=0.0.1a0,<0.1"
```

```console
$ pip install "libvt>=0.0.1a0,<0.1"
```

`libvt` is in pre-alpha (`0.0.1a0`); pin `libvt>=0.0.1a0,<0.1` while the
package is pre-1.0.

## Reading screen state

{class}`libvt.Terminal` uses keyword-only `(rows, cols)` dimensions and
zero-based `(row, col)` indexing across cells, rows, and 2D slices. Each call
to {meth}`~libvt.Terminal.feed` updates the screen and buffers any terminal
query replies for {meth}`~libvt.Terminal.drain_replies`:

```python
>>> import libvt
>>> term = libvt.Terminal(rows=4, cols=20)
>>> term.feed(b"\x1b(0lqqqk\x1b(B\r\n\x1b[4:3;38:2::40:180:160mwave\x1b[0m\x1b[6n")
>>> term.lines[:2]
('┌───┐', 'wave')
>>> term[1, 0].style.underline
<UnderlineStyle.CURLY: 3>
>>> term.drain_replies()
b'\x1b[2;5R'
```

## Rendering, stripping, and explaining

{func}`libvt.render` returns screen text after processing terminal output.
{func}`libvt.strip` removes control sequences without applying cursor
movements. {func}`libvt.explain` identifies the sequences in a byte stream:

```python
>>> import libvt
>>> libvt.render(b"\x1b[32mok\x1b[0m\r\nnext", rows=3, cols=10)
'ok\nnext'
>>> libvt.strip(b"\x1b[1mbold\x1b[0m plain")
'bold plain'
>>> [s.mnemonic for s in libvt.explain(b"\x1b[1;31mhi\x1b[0m")]
['SGR', 'TEXT', 'SGR']
```

## Encoding input

{meth}`libvt.Terminal.encode` encodes keyboard, mouse, focus, and paste events
({class}`libvt.KeyEvent`, {class}`libvt.MouseEvent`, {class}`libvt.FocusEvent`,
and {class}`libvt.PasteEvent`) according to active terminal modes (see
{doc}`topics/protocols-and-input` for paste validation options):

```python
>>> term = libvt.Terminal(rows=4, cols=20)
>>> term.feed(b"\x1b[>1u\x1b[?2004h")
>>> term.encode(libvt.KeyEvent.parse("ctrl+k"))
b'\x1b[107;5u'
>>> term.encode(libvt.PasteEvent("line 1\nline 2"))
b'\x1b[200~line 1\nline 2\x1b[201~'
```

## Pytest assertions

{func}`libvt.testing.assert_screen` compares visible text (including literal
box-drawing characters) and optional cursor coordinates.
{func}`libvt.testing.assert_screen_box` checks column-aligned `┌──┐` frames and
trailing `W` soft-wrap markers, showing expected and actual screens on failure:

```python
>>> from libvt.testing import assert_screen, assert_screen_box
>>> term = libvt.Terminal(rows=2, cols=8)
>>> term.feed("日本!\r\nok".encode())
>>> assert_screen(term, "日本!\nok", cursor=(1, 2))
>>> assert_screen_box(
...     term,
...     """
...     ┌────────┐
...     │日本!   │
...     │ok      │
...     └────────┘
...     """,
...     cursor=(1, 2),
... )
```

To include recent input bytes and their decoded VT sequences in failure
messages when diagnosing a failing test, pass `recorder=libvt.FlightRecorder()`
to `Terminal` (the {func}`~libvt.pytest_plugin.vt_terminal` pytest fixture
attaches a {class}`~libvt.terminal.FlightRecorder` by default).
