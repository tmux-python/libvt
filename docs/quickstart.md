(quickstart)=

# Quickstart

## Requirements

- Python >= 3.12 (CPython and PyPy)
- Zero runtime dependencies

## Installation

```console
$ uv add libvt
```

```console
$ pip install libvt
```

## One-liner helpers

{func}`libvt.render`, {func}`libvt.strip`, and {func}`libvt.explain`
cover one-shot rendering, control-sequence stripping, and byte-stream
disassembly:

```python
>>> import libvt
>>> libvt.render(b"\x1b[32mok\x1b[0m\r\nnext", rows=3, cols=10)
'ok\nnext'
>>> libvt.strip(b"\x1b[1mbold\x1b[0m plain")
'bold plain'
>>> [s.mnemonic for s in libvt.explain(b"\x1b[1;31mhi\x1b[0m")]
['SGR', 'TEXT', 'SGR']
```

## Stateful emulation

{class}`libvt.Terminal` uses keyword-only `(rows, cols)` dimensions and
zero-based `(row, col)` indexing across cells, rows, and 2D slices:

```python
>>> term = libvt.Terminal(rows=4, cols=20)
>>> term.feed(b"\x1b(0lqqqk\x1b(B\r\n\x1b[4:3mwave\x1b[0m")
>>> term.lines[:2]
('┌───┐', 'wave')
>>> term[1, 0].style.underline
<UnderlineStyle.CURLY: 3>
```

## Mode-aware input and paste safety

{meth}`libvt.Terminal.encode` serializes keys, mouse events, focus
changes, and bracketed pastes according to active terminal modes:

```python
>>> term = libvt.Terminal(rows=4, cols=20)
>>> term.feed(b"\x1b[>1u\x1b[?2004h")
>>> term.encode(libvt.KeyEvent.parse("ctrl+k"))
b'\x1b[107;5u'
>>> term.encode(libvt.PasteEvent("line 1\nline 2"))
b'\x1b[200~line 1\nline 2\x1b[201~'
```

## Pytest assertions

{func}`libvt.testing.assert_screen` compares visible text and optional
cursor coordinates, accepting either plain lines or boxed diagrams:

```python
>>> from libvt.testing import assert_screen
>>> term = libvt.Terminal(rows=2, cols=8)
>>> term.feed("日本!\r\nok".encode())
>>> assert_screen(term, "日本!\nok", cursor=(1, 2))
```
