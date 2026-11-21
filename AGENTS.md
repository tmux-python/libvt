# AGENTS.md

libvt is a pure-Python, zero-dependency, typed virtual terminal emulator
and streaming VT parser (`Parser` → `Terminal` / `Screen` / `Snapshot` +
`Event`s). It ships a transport-only `asyncio` stream and POSIX PTY
adapter (`libvt.aio`), a namespace-safe pytest plugin
(`libvt.pytest_plugin`), mode-aware input encoders (`libvt.input`), and
visual boxed screen assertions (`libvt.testing`), serving
[libtmux](https://github.com/tmux-python/libtmux) and general Python
terminal testing without LGPL copyleft obligations.

Follow the conventions already in the tree, and keep a change scoped to
what was asked for.

## What is here

| Path | What it is |
| ---- | ---------- |
| `src/libvt/parser.py`, `modes.py`, `style.py`, `config.py` | VT500 byte parser, CSI subparameters, modes, SGR styles, and `Profile` / `Policy` / `Limits` |
| `src/libvt/_ucd.py`, `_grid.py`, `screen.py`, `terminal.py` | Unicode 17 width/clustering tables, packed viewport & RLE scrollback rows, `Screen` / `ScreenSet`, `Terminal`, `Snapshot`, and `Formatter` |
| `src/libvt/status.py`, `events.py`, `input.py`, `codec.py` | Rex `OSC 7501` program status store, closed `Event` union, mode-aware input encoders, and cold-path VT sequence explainer |
| `src/libvt/aio.py`, `__main__.py` | `AsyncTerminalStream`, POSIX `open_pty()`, and `python -m libvt` CLI (`explain`, `render`, `strip`) |
| `src/libvt/testing.py`, `pytest_plugin.py`, `exc.py` | Boxed `assert_screen`, `FlightRecorder` diagnostics, `vt_*` pytest fixtures, and exceptions |
| `tests/` | Unit, property fuzz, PTY, CLI, and fixture tests |
| `docs/` | Sphinx/MyST site: `topics/` (concepts), `api/` (autodoc), `project/` (contributing, releasing, ADRs `0001`–`0010`) |
| `scripts/` | Unicode table generator (`generate_ucd.py`) and VT throughput benchmarks (`bench_vt.py`, `benchmark.py`) |
| `README.md` | Doctested quickstart — every `>>> ` block is a real test |
| `CHANGES` | The changelog, rendered as a docs page |
| `MIGRATION` | Deprecation and migration notes |
| `conftest.py` | Root fixtures, including `doctest_namespace` seeding |

## Which policy applies

- Documentation, user-facing text, `CHANGES`, release notes, commit
  messages, docstrings, and source comments:
  [.github/WRITING.md](.github/WRITING.md)
- Environment, the gates, tests, documentation builds, releases, and
  pull requests: [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md)

Each of those is the single home for its subject. Where a rule seems to
be stated twice, the file listed above is the one that governs.

## Change discipline

- Make the smallest coherent change that solves the verified problem;
  keep unrelated cleanup out of it.
- Reuse an existing file, helper, API, or test before adding a new one.
- Add a file only for a durable boundary — a distinct responsibility,
  independent reuse, or splitting an oversized module — not for a
  single-use helper or a one-line re-export.
- Keep new APIs private until a caller outside the module needs them.
- Add a test for every user-visible behaviour change, and a `CHANGES`
  entry for every change to the public API, configuration, or output.
- A passing gate is evidence only once it has been shown capable of
  failing. Pair a new test with a deliberate break that proves it bites.
- Maintain strict clean-room MIT provenance: derive state transitions
  and test vectors from public standards (ECMA-48, ITU T.416, Unicode
  UAX #11 / #29, Paul Flo Williams' VT500 state diagram) and permissive
  (`MIT`, `ISC`, `Apache-2.0`, `BSD`) references — never copy or
  translate from `LGPL` or `GPL` terminal implementations (`pyte`,
  `vte`/GTK, `asciinema`).

Python >= 3.12 is the compatibility floor, and runtime dependencies stay
empty (`dependencies = []`). Importing `libvt` performs no I/O and does
not load `asyncio`, `pty`, or `pytest`.

## References

- Repository: https://github.com/tmux-python/libvt
- Documentation: https://libvt.git-pull.com/
- libtmux: https://libtmux.git-pull.com/
- DEC ANSI Parser state machine: https://vt100.net/emu/dec_ansi_parser
