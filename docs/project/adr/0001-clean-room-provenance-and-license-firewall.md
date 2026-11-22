(adr-clean-room-provenance)=

# ADR 0001: Clean-room MIT provenance and pure-Python distribution

## Status

Accepted. 2026-11-21.

## Context

`libtmux` and downstream Python terminal test suites need an in-memory
virtual terminal emulator and streaming VT parser without copyleft license
obligations (`tmux-python/libtmux#886`). `pyte` (`0.8.2`) and `libvte` are
licensed under the GNU LGPL, while `asciinema`'s virtual terminal engine is
licensed under `GPL-3.0-or-later`. Beyond licensing, `pyte` predates modern
terminal extensions required by contemporary `tmux` and TUI test suites:
DEC mode `?2026` synchronized output, colon-delimited `SGR` subparameters
(`4:3`, `38:2::R:G:B`, `58:2::R:G:B`), `OSC 8` hyperlinks, `OSC 133`
semantic shell prompts, `OSC 7501` program status, and `SIXEL` / Kitty
graphics placement and erase notifications.

At the same time, shipping a mandatory C or Zig extension wheel complicates
installation across CPython 3.12+, pre-release Python interpreters, PyPy,
and air-gapped CI environments.

## Decision

`libvt` is a standalone, MIT-licensed, pure-Python (`py3-none-any.whl`)
package with `dependencies = []` and a Python `>= 3.12` floor:

- **Clean-room license firewall**: Every state transition, parameter parser,
  width rule, and test vector is derived strictly from public standards
  (ECMA-48, DEC STD 070, Paul Flo Williams' VT500 parser state diagram,
  Unicode UAX #11 and UAX #29) and permissively licensed (`MIT`, `ISC`,
  `Apache-2.0`, `BSD`) reference implementations (`ghostty`, `tmux`,
  `zellij`, `wezterm` VT crates, `alacritty_terminal`, `ratatui`, `rmux`,
  and `shell-use`).
- **Copyleft quarantine**: Source code under `LGPL`, `GPL`, `AGPL`, or `MPL`
  (including `pyte`, `vte`, `asciinema`, `fish-shell`, and `helix`) must
  never be read, translated, or copied when developing or testing `libvt`.
- **One-way dependency graph**: `stdlib -> libvt -> libtmux`. `libvt` never
  imports `libtmux`.

## Alternatives considered

| Approach | License | Portability | Reusable outside `libtmux` | Modern VT parity |
|---|---|---|---|---|
| Standalone pure-Python `libvt` (chosen) | MIT | `py3-none-any.whl` | yes | full |
| Inline helper inside `libtmux.test` | MIT | `py3-none-any.whl` | no | partial |
| Mandatory `libghostty-vt` native FFI wheel | MIT | platform wheels + C ABI | yes | omits SIXEL |
| Retain `pyte` | LGPL-3.0 | `py3-none-any.whl` | yes | missing `?2026`, `OSC 8/133/7501` |

## Consequences

### Positive

- Downstream MIT, BSD, Apache-2.0, and proprietary consumers can depend on
  `libvt` without LGPL compliance review.
- Universal `py3-none-any.whl` wheels install instantaneously on Linux,
  macOS, Windows, CPython 3.12–3.15, and PyPy with zero compiler toolchain.
- External Python TUI frameworks and CLI test suites can adopt `libvt`
  without installing `tmux` or `libtmux`.

### Tradeoffs

- Pure-Python throughput (`~6–9 MiB/s` on escape-heavy streams) is lower
  than native SIMD C/Zig/Rust parsers (`>100 MiB/s`), though sub-millisecond
  per `80x24` frame (`~0.3–0.7 ms`), which keeps interactive test suites
  fast.

### Risks

- Accidental exposure to copyleft reference code by contributors.
  Mitigation: document the copyleft quarantine in `AGENTS.md`,
  `.github/CONTRIBUTING.md`, and this ADR, and anchor parser behavior to
  citeable ECMA-48 / DEC / `tmux` / `ghostty` specifications.
