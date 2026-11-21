(adr-three-layer-architecture)=

# ADR 0002: Three-layer VT architecture and sans-I/O effects

## Status

Accepted. 2026-11-21.

## Context

A virtual terminal library serves three distinct callers:

1. Byte-level stream analyzers and fuzzers that want structured `Print`,
   `Execute`, `EscDispatch`, `CsiDispatch`, `OscDispatch`, `DcsDispatch`,
   and `ApcDispatch` events without allocating a character grid.
2. Deterministic in-memory screen tests (`Terminal.feed(b"...")`) that must
   never block on sockets, file descriptors, or event loops.
3. Live PTY harnesses (`tmux` panes, interactive shells) that must answer
   terminal capability queries (`DA1`, `DA2`, `DSR 5`, `DSR 6`, `DECRQM`)
   without allowing untrusted escape sequences to exfiltrate window titles
   or clipboard contents.

Coupling byte parsing, grid mutation, and PTY replies into a single class
makes isolated parser testing awkward and risks unsolicited reply loops.

## Decision

Structure `libvt` in three layers aligned with `libghostty-vt`'s separation
of byte parser, read-only stream handler, and terminal state:

1. **Layer 1 — `libvt.parser.Parser`**: A 14-state Paul Williams VT500 byte
   state machine with UTF-8 incomplete-sequence retention, colon/semicolon
   `CsiParams` subparameter masks, and hard caps on parameter count and
   `OSC`/`DCS`/`APC` payload bytes so malformed streams always recover to
   `GROUND`.
2. **Layer 2 — `libvt.terminal.Effects` and `Terminal.drain_replies()`**:
   `Terminal` is sans-I/O and read-only on external channels by default.
   Standard bounded terminal queries (`DA1`, `DA2`, `DSR 5`, `DSR 6`,
   `DECRQM`) write into a capped internal reply buffer drained via
   `term.drain_replies() -> bytes` (or forwarded immediately if
   `Effects.on_pty_write` is configured). Potentially sensitive reports
   (`allow_title_report`, `allow_checksum_report`, `allow_clipboard_read`)
   default to `False`. Graphics (`on_sixel`, `on_kitty_graphics`,
   `on_erase`) and semantic shell events (`on_osc_133`, `on_osc_7501`) emit
   structured dataclasses via optional `Effects` callbacks without pulling
   image codecs into `libvt`.
3. **Layer 3 — `libvt.screen.Screen` / `ScreenSet` and
   `libvt.terminal.Terminal`**: Owns primary and `?1049` alternate screens,
   bounded scrollback (`collections.deque`), cursor save slots (`DECSC`),
   `DECSTBM` / `DECLRMM` scroll margins, and immutable `Snapshot` views.

## Alternatives considered

- **Single monolithic `Screen` + `Stream` pair without reply buffering**:
  Forces callers driving real PTYs (`tmux` client tests, shells querying
  `DSR 6` cursor position) to re-parse `CSI 6 n` externally.
- **Unconditional query replies for all `OSC` / `DCS` queries**: Exposes
  test harnesses replaying untrusted logs to `OSC 52` clipboard reads and
  title echo amplification.

## Consequences

### Positive

- `Parser` can be unit-tested and fuzzed independently of screen geometry.
- `Terminal` works identically whether fed static `bytes` in a unit test or
  connected to a live PTY via `drain_replies()`.
- Security-sensitive reports remain off unless explicitly opted in on
  `Effects`.

### Tradeoffs

- Callers driving an interactive PTY without `AsyncTerminalStream` must call
  `pty.write(term.drain_replies())` after `feed()` when the child process
  blocks on `DSR` or `DA1`.

### Risks

- Unbounded reply accumulation if a caller never calls `drain_replies()`
  on a hostile stream. Mitigation: cap the internal reply buffer and drop
  excess bytes once full until drained.
