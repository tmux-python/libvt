(adr-coordinates-and-sync-stepping)=

# ADR 0004: Zero-based (row, col) coordinates and sync-aware stepping

## Status

Accepted. 2026-11-21.

## Context

Two recurring ergonomic traps affect terminal emulator libraries:

1. **Coordinate mismatch**: ECMA-48 wire sequences (`CSI row ; col H`,
   `DECSTBM`) are 1-based `(row, col)`, whereas older Python libraries pass
   `(columns, lines)` to constructors and expose `(x, y)` on cursors,
   inverting axes relative to 2D Python indexing `grid[row][col]`.
2. **Mid-frame tearing**: Modern TUIs and multiplexers wrap screen updates
   in DEC private mode `?2026` (`CSI ? 2026 h ... CSI ? 2026 l`). Reading
   `term.text` mid-chunk while `?2026` is active observes half-erased lines
   and transient cursor jumps. Conversely, exporting a terminal snapshot as
   VT sequences must not leave `?2026` asserted in the static output.

## Decision

1. **0-based `(row, col)` everywhere in Python**:
   - Constructors and resizes take keyword-only `(*, rows: int = 24,
     cols: int = 80)`.
   - `Cursor` exposes `.row` and `.col` (0-based), plus `pending_wrap: bool`
     when a glyph lands in `cols - 1` under `DECAWM` before the next
     printable character arrives.
   - Indexing uses NumPy-style 2D syntax: `term[row]` -> `RowView`,
     `term[row, col]` -> `Cell`, `term[r1:r2, c1:c2]` -> `RegionView`, with
     standard Python negative indices (`term[-1]` for status bars).
   - 1-based wire translation (`max(1, param) - 1` and clamping to active
     `DECSTBM` / `DECLRMM` margins under `DECOM`) occurs solely inside VT
     sequence handlers.
2. **Sync-aware ingestion and tear-free snapshots**:
   - `term.feed(data: Buffer) -> None` and `term.feed_str(text: str) -> None`
     consume complete buffers while preserving unfinished UTF-8 and escape
     sequences across calls.
   - `term.feed_until(data: Buffer, *, offset: int = 0, stop: StopOn =
     StopOn.EOF | StopOn.SYNC_END) -> StepResult` halts at exact frame or
     prompt boundaries and reports the next byte offset.
   - When mode `?2026` is active (`term.synchronized == True`), `term`
     retains the pre-batch `Snapshot` at `term.committed` until `CSI ? 2026 l`
     commits the new frame. `term.snapshot()` always captures live state,
     and `Formatter` omits transient `?2026` when serializing VT state.

## Alternatives considered

- **1-based indexing matching `CSI H`**: Violates Python sequence slicing
  (`term[0]`, `term[-1]`, `term[1:4, 6:18]`) and surprises every Python
  caller.
- **`(cols, rows)` positional constructor arguments**: Easy to transpose
  accidentally (`Terminal(24, 80)` vs `Terminal(80, 24)`); keyword-only
  `(*, rows=24, cols=80)` eliminates positional ambiguity.

## Consequences

### Positive

- 2D popup and pane region assertions read naturally (`term[1:4, 6:18].text`,
  `term[-1].text`).
- `term.committed` gives synchronous and async test runners tear-free `?2026`
  frames without custom buffering.

### Tradeoffs

- Callers translating raw `CSI row ; col H` numbers from wire logs must
  subtract `1` when indexing `term[row - 1, col - 1]`.

### Risks

- A crashed child process leaving `?2026` set forever. Mitigation:
  `term.snapshot()` still exposes live state for inspection, `reset()` clears
  `?2026`, and `AsyncTerminalStream` enforces a configurable `sync_timeout`
  (default `1.0` second) before releasing a held frame.
