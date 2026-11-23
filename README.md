# libvt

`libvt` (`pip install libvt`, `import libvt`) is a pure-Python,
zero-dependency, typed virtual terminal emulator and streaming VT parser
(`py3-none-any.whl` — no C shared library required).

[![tests](https://github.com/tmux-python/libvt/actions/workflows/tests.yml/badge.svg)](https://github.com/tmux-python/libvt/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue)](https://github.com/tmux-python/libvt/blob/master/LICENSE)

## Highlights

- **Sans-I/O core**: Zero runtime dependencies, zero I/O on `import libvt`,
  and no `asyncio`, `pty`, or `pytest` imports until requested.
- **Compact two-tier grid**: Lazy 64-bit inline viewport attributes plus
  run-length-encoded scrollback rows and primary-screen soft-wrap resize
  reflow.
- **Unicode 17 clustering**: Bundled two-stage width and UAX #29 tables
  handling CJK wide glyphs, ZWJ emoji, Regional Indicator flag pairs,
  Indic conjuncts, and `VS15` / `VS16` under DEC `?2027`.
- **Modern terminal protocols**: Rex `OSC 7501` Program Status (with
  `OSC 9;4` fallback), `OSC 133` semantic prompt zones, `OSC 7` working
  directory, `OSC 8` hyperlinks, `OSC 52` clipboard, `?2026`
  synchronized-output stepping, `DECRQM`, `DECRQSS`, `XTGETTCAP`, and
  `DCS tmux;` passthrough events.
- **Mode-aware input & testing**: Outbound key/mouse/focus/paste
  encoding (including Kitty `CSI u` and fail-closed bracketed paste),
  `libvt.aio` stream and controlling-TTY POSIX PTY helpers, `vt_*`
  pytest fixtures, and `assert_screen` / `assert_screen_box` diffs with
  `FlightRecorder` disassembly.

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

One-liner helpers render, strip, or disassemble VT byte streams without
managing an emulator instance:

```python
>>> import libvt
>>> libvt.render(b"\x1b[1;32mready\x1b[0m\r\nitem 1", rows=4, cols=20)
'ready\nitem 1'
>>> libvt.strip(b"\x1b[31merror:\x1b[0m failed")
'error: failed'
```

Stateful emulation tracks zero-based `(row, col)` cells, SGR colon
subparameters, query replies, and `OSC 7501` program status:

```python
>>> term = libvt.Terminal(rows=4, cols=20)
>>> term.feed(b"\x1b[1;4:3;38:2::40:180:160mstatus\x1b[0m\x1b[6n")
>>> term[0, 0].style.underline
<UnderlineStyle.CURLY: 3>
>>> term.drain_replies()
b'\x1b[1;7R'
>>> term.feed(b"\x1b]7501;state=working:id=build:progress=60\x1b\\")
>>> term.program_status.value, term.program_status.progress
('working', 60)
```

Boxed visual assertions verify CJK wide-character alignment and cursor
coordinates in tests:

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

## License

MIT License. See [LICENSE](LICENSE).
