(quickstart)=

# Quickstart

## Installation

```console
$ uv add libvt
```

```console
$ pip install libvt
```

## Feeding bytes and reading cells

Create a {class}`~libvt.terminal.Terminal` with keyword-only zero-based
`rows` and `cols`, feed raw VT bytes, and index cells or 2D slices:

```python
>>> from libvt import AnsiColor, Terminal
>>> term = Terminal(rows=6, cols=20)
>>> term.feed(b"\x1b[1;34mBuild\x1b[0m: pass\r\n\x1b[6;1H[0] 0:zsh*")
>>> term[0, 0].style.fg == AnsiColor.BLUE
True
>>> term[-1].text
'[0] 0:zsh*'
>>> term[0:2, 0:5].lines
('Build', '')
```

## Visual assertions in pytest

Use {func}`~libvt.testing.assert_screen` to compare visible lines and
cursor coordinates with boxed failure output:

```python
>>> from libvt.testing import assert_screen
>>> snap = Terminal(rows=4, cols=16)
>>> snap.feed_str("prompt$ ls\r\nREADME.md")
>>> assert_screen(snap, "prompt$ ls\nREADME.md", cursor=(1, 9))
```
