# libvt

[![PyPI version](https://img.shields.io/pypi/v/libvt.svg)](https://pypi.org/project/libvt/)
[![tests](https://github.com/tmux-python/libvt/actions/workflows/tests.yml/badge.svg)](https://github.com/tmux-python/libvt/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue)](https://github.com/tmux-python/libvt/blob/master/LICENSE)

`libvt` is a headless terminal emulator and VT parser for Python. Feed it
terminal output to inspect screen contents, cursor positions, and cell styles,
or use it to test terminal programs.

Requires Python 3.12 or later (CPython and PyPy). Written in Python, with type
annotations and no runtime dependencies.

## Installation

Install with `uv`:

```console
$ uv add "libvt>=0.0.1a0,<0.1"
```

Install with `pip`:

```console
$ pip install "libvt>=0.0.1a0,<0.1"
```

`libvt` is in pre-alpha (`0.0.1a0`). Minor and `0.0.x` releases may include
breaking API changes; pin `libvt>=0.0.1a0,<0.1` while the package is pre-1.0.

## Quickstart

Each call to `feed()` updates the terminal's screen. Read the resulting text,
cells, and cursor position:

```python
>>> import libvt
>>> term = libvt.Terminal(rows=2, cols=20)
>>> term.feed(b"Loading 10%")
>>> term.feed(b"\r\x1b[2KLoading 20%")
>>> term.text
'Loading 20%'
>>> term.cursor.row, term.cursor.col
(0, 11)
```

`assert_screen_box()` checks screen contents, wide-character column alignment,
and cursor coordinates in tests:

```python
>>> from libvt.testing import assert_screen_box
>>> t = libvt.Terminal(rows=2, cols=8)
>>> t.feed("日本ok\r\ndone".encode())
>>> assert_screen_box(
...     t,
...     """
...     ┌────────┐
...     │日本ok  │
...     │done    │
...     └────────┘
...     """,
...     cursor=(1, 4),
... )
```

`render()` returns screen text after processing terminal output. `strip()`
removes control sequences without applying cursor movements. `explain()`
identifies the sequences in a byte stream:

```python
>>> libvt.render(b"\x1b[1;32mready\x1b[0m\r\nitem 1", rows=4, cols=20)
'ready\nitem 1'
>>> libvt.strip(b"\x1b[31merror:\x1b[0m failed")
'error: failed'
>>> [s.mnemonic for s in libvt.explain(b"\x1b[31merror:\x1b[0m")]
['SGR', 'TEXT', 'SGR']
```

## Features

- Check screen contents and cursor positions in tests, with expected and actual
  screens shown on failure.
- Handle wide characters and emoji sequences using Unicode 17 width and
  grapheme rules.
- Keep scrollback and reflow wrapped lines when the primary screen is resized.
- Inspect completed redraws from applications that use synchronized output
  (`?2026`).
- Encode keyboard, mouse, focus, and paste events according to the active
  terminal modes.
- Run terminal programs on POSIX PTYs or connect to `asyncio` streams.

Read `tmux` captures as screen cells and decode control-mode output with
[`libvt.tmux`](https://libvt.git-pull.com/api/tmux/). Full guides and API
reference are at [libvt.git-pull.com](https://libvt.git-pull.com/).

## License

MIT License. See [LICENSE](LICENSE).
