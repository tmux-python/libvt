# libvt

`libvt` (`pip install libvt`, `import libvt`) is a pure-Python,
zero-dependency, typed virtual terminal emulator and streaming VT parser
(`py3-none-any.whl` — no C shared library required).

[![tests](https://github.com/tmux-python/libvt/actions/workflows/tests.yml/badge.svg)](https://github.com/tmux-python/libvt/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue)](https://github.com/tmux-python/libvt/blob/master/LICENSE)

## Requirements

- Python >= 3.12 (CPython and PyPy)
- Zero runtime dependencies

## Installation

Install with `uv`:

```console
$ uv add libvt
```

Install with `pip`:

```console
$ pip install libvt
```

`libvt` is pre-1.0; pin a minor range in downstream projects
(`libvt>=0.1,<0.2`).

## Quickstart

`Terminal` is sans-I/O, uses zero-based `(row, col)` coordinates
everywhere, and leaves `asyncio` and `pytest` unloaded on `import libvt`:

```python
>>> from libvt import AnsiColor, Terminal
>>> term = Terminal(rows=24, cols=80)
>>> term.feed(b"\x1b[1;32mOK\x1b[0m \xe6\x97\xa5\xe6\x9c\xac\r\n> \x1b[6n")
>>> term.text
'OK 日本\n> '
>>> (term[0, 0].text, term[0, 0].style.bold, term[0, 0].style.fg)
('O', True, <AnsiColor.GREEN: 2>)
>>> term.committed[0:2, 0:2].lines
('OK', '> ')
>>> term.drain_replies()
b'\x1b[2;3R'
```

## License

MIT License. See [LICENSE](LICENSE).
