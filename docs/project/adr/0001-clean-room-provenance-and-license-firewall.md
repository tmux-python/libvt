(adr-clean-room-provenance)=

# ADR 0001: Clean-room MIT provenance, PyPI namespace, and Python 3.12+ floor

## Status

Accepted. 2026-11-21.

## Context

`libtmux` (`>= 3.11`), `tmuxp` (`>= 3.11`), and Python terminal test suites
need an in-memory virtual terminal emulator and streaming VT parser without
copyleft license obligations (`tmux-python/libtmux#886`). Existing pure-Python
and GTK terminal libraries carry copyleft terms (`pyte` under `LGPL-3.0`,
`libvte` under `LGPL-3.0`, and `asciinema`'s virtual terminal engine under
`GPL-3.0-or-later`) and omit contemporary terminal extensions used by `tmux`
and modern TUIs: DEC mode `?2026` synchronized output, DEC mode `?2027`
Unicode grapheme clustering, ITU T.416 colon-delimited `SGR` subparameters
(`4:3`, `38:2::R:G:B`, `58:2::R:G:B`), `OSC 8` hyperlinks, `OSC 133` semantic
shell prompts, `OSC 7501` program status, Kitty keyboard stacks (`CSI u`), and
`SIXEL` / Kitty graphics placement and erase tracking.

Three packaging and runtime constraints govern the package boundary:

1. **Distribution and import name availability**: Candidate package names
   `pyvt` (`https://pypi.org/project/pyvt/`) and `vt`
   (`https://pypi.org/project/vt/`, also used as `import vt` by `vt-py`) are
   already registered on PyPI, whereas `libvt` is available and matches the
   `libtmux` / `libvcs` naming family.
2. **Python interpreter floor**: Raising `requires-python` to `>= 3.14` (to
   use stdlib `compression.zstd` or PEP 649 runtime semantics) would block
   adoption in `libtmux`, `tmuxp`, and downstream CI matrices running Python
   3.12 and 3.13.
3. **Pure-Python wheel portability**: Shipping a mandatory C, Zig, or Rust
   extension complicates universal wheels, free-threaded CPython builds
   (`PYTHON_GIL=0`), PyPy, WebAssembly sandboxes, and offline CI runners.

## Decision

`libvt` ships as a standalone, MIT-licensed, pure-Python (`py3-none-any.whl`)
distribution under the `libvt` package and import name with
`dependencies = []` and `requires-python = ">=3.12,<4.0"`:

- **Clean-room license firewall**: Every parser transition, escape parameter
  rule, character-width table, and test vector is derived strictly from public
  standards (ECMA-48, DEC STD 070, ITU T.416, Paul Flo Williams' VT500 parser
  state diagram, Unicode UAX #11 and UAX #29) and permissively licensed
  (`MIT`, `ISC`, `Apache-2.0`, `BSD`) specifications and implementations
  (`tmux`, `ghostty` / `libghostty-vt`, `alacritty_terminal`, `wezterm` VT
  crates, `zellij`, `ratatui`, and the Rex `OSC 7501` specification).
- **Copyleft quarantine**: Source code under `LGPL`, `GPL`, `AGPL`, or `MPL`
  (including `pyte`, `libvte`, `asciinema`, `fish-shell`, and `helix`) MUST
  never be read, translated, or copied when developing or testing `libvt`.
- **Acyclic ecosystem dependency graph**: `stdlib -> libvt -> libtmux -> tmuxp`.
  `libvt` never imports `libtmux`.
- **Zero-GIL thread safety**: Mutable parser and SGR memoization tables live on
  `Parser` / `Terminal` instances rather than unlocked module-global
  dictionaries so concurrent `Terminal` instances run without data races on
  free-threaded CPython (`3.13t` / `3.14t`).

## Alternatives considered

| Candidate | License | PyPI name | Wheel | Python floor | Disqualifier or trade-off |
|---|---|---|---|---|---|
| `libvt` standalone pure-Python (chosen) | MIT | available | `py3-none-any.whl` | `>= 3.12` | Pure-Python CPU ceiling (~10-35 MiB/s vs SIMD native) |
| `pyvt` / `vt` standalone naming | MIT | occupied | `py3-none-any.whl` | `>= 3.12` | Name collision on PyPI and `import vt` conflict |
| Python `>= 3.14`-only pure-Python engine | MIT | occupied (`vt`) | `py3-none-any.whl` | `>= 3.14` | Excludes Python 3.12 and 3.13 consumers (`libtmux`, `tmuxp`) |
| Inline helper inside `libtmux.test.vt` | MIT | n/a | `py3-none-any.whl` | `>= 3.11` | Unusable without installing `libtmux` |
| Mandatory `libghostty-vt` C ABI FFI wheel | MIT | available | platform wheels | `>= 3.12` | Requires Zig build matrix; omits `SIXEL` |
| Retain `pyte` | LGPL-3.0 | occupied | `py3-none-any.whl` | `>= 3.8` | Copyleft license; lacks `?2026`, `?1049`, colon SGR, `OSC 8/133/7501` |

## Consequences

### Positive

- Downstream MIT, BSD, Apache-2.0, and proprietary consumers depend on `libvt`
  without copyleft review or native C/Zig/Rust toolchains.
- Python 3.12, 3.13, 3.14, 3.15, and PyPy share a single `py3-none-any.whl`
  artifact.
- Instance-local caches allow parallel test workers and free-threaded Python
  threads to drive separate `Terminal` instances concurrently.

### Tradeoffs

- Pure-Python byte ingestion (`~10-35 MiB/s` on ASCII/ANSI streams) is slower
  than SIMD-accelerated native engines (`>100 MiB/s`), while still processing
  typical `80x24` TUI frames in well under a millisecond.

### Risks

- Contributors consulting copyleft terminal emulators during bug fixes.
  Mitigation: state the quarantine in `AGENTS.md`, `.github/CONTRIBUTING.md`,
  and this ADR, and cite ECMA-48, DEC, `tmux`, or `ghostty` references in
  tests and commit messages.
