(adr-coordinates-and-sync-stepping)=

# ADR 0004: Zero-based (row, col) coordinates, resize reflow, and ?2026 stepping

## Status

Accepted. 2026-11-21.

## Context

Three coordinate and frame-lifecycle problems recur across terminal APIs:

1. **Axis inversion**: Legacy Python libraries pass `(cols, rows)` to
   constructors and `(x, y)` on cursors while indexing `screen[row][col]`,
   inverting horizontal and vertical axes between construction and indexing.
2. **Cursor allocation churn and `pending_wrap` edge cases**: Storing `Screen`'s
   live cursor as a frozen dataclass re-allocated on every character, `CR`, and
   `LF` consumed `28%` of total CPU in `cProfile` runs (`90,000+` allocations
   per `30,000` lines). In addition, `xterm`/`Ghostty` and `tmux` (`s->cx == sx`
   in `screen-write.c`) differ subtly when `CUB` (`CSI D`) or `BS` (`0x08`)
   arrives during `pending_wrap` at the right margin.
3. **Torn `?2026` frames, timer races, and resize reflow**: Eagerly cloning all
   24 rows on every `CSI ? 2026 h` wastes work when `CSI ? 2026 l` arrives in
   the same `feed()` buffer, while untagged async timeouts clearing a held
   snapshot can clobber a *subsequent* `?2026` batch. On `resize()`, cropping
   rows without rejoining soft-wrapped (`wrapped=True`) primary lines breaks
   shell prompts and wrapped CLI output compared to `tmux` and `Ghostty`.

## Decision

1. **Universal 0-based `(row, col)` order**:
   - Every constructor, helper, and `resize()` method accepts keyword-only
     `(*, rows: int = 24, cols: int = 80)`. Zero `(col, row)` types exist in
     `libvt`; `Terminal.from_size(os.terminal_size)` converts `os.terminal_size`
     (`columns`, `lines`) explicitly.
   - `Screen` stores `_cur_row: int`, `_cur_col: int`, `_pending_wrap: bool`,
     `_cur_visible: bool`, `_cur_shape: CursorShape`, and `_cur_blinking: bool`
     as scalars on the hot path, materializing a frozen `Cursor` dataclass on
     demand in `@property def cursor`.
   - Indexing uses NumPy-style 2D syntax on `Terminal`, `Screen`, and
     `Snapshot`: `term[row] -> RowView`, `term[row, col] -> Cell`, and
     `term[r1:r2, c1:c2] -> RegionView` (supporting negative indices such as
     `term[-1]` for status lines).
   - `Profile.pending_wrap_phantom_col` + `Mode.REVERSE_WRAP` (`?45`) model
     both `xterm`/`Ghostty` (`DEFAULT_PROFILE`) and `tmux` (`TMUX_PROFILE`,
     where `pending_wrap` acts as phantom column `cols` on `CUB`/`BS` and `BS`
     at column `0` reverse-wraps across `wrapped` lines).
2. **Copy-on-write `?2026` render hold and generation-tagged expiry**:
   - On `CSI ? 2026 h`, `Terminal` increments `_sync_generation: int` and
     marks `_sync_hold_pending = True` without cloning rows immediately; only if
     `term.committed` is read while `?2026` remains active (or before the first
     post-hold mutation when a waiter is attached) is the pre-batch `Snapshot`
     materialized, and `CSI ? 2026 l` in the same chunk skips cloning altogether.
   - `term.expire_synchronized_output(sync_generation: int | None = None) ->
     bool` releases a stalled `?2026` hold, clears `Mode.SYNCHRONIZED_OUTPUT`,
     and emits `RenderHoldChanged(active=False, expired=True)` only when
     `sync_generation` matches `_sync_generation`, preventing stale async timers
     from releasing a newer frame.
   - `term.feed_until(data, *, offset=0, stop=...)` bulk-scans `GROUND` and
     control-string interiors (`OSC`/`DCS`/`APC`) to `\x07`/`\x1b` boundaries
     and checks `not parser.utf8_pending` before reporting `StopOn.GROUND`.
3. **Primary-screen soft-wrap resize reflow (`resize(*, rows, cols, reflow=True)`)**:
   - Resizing `primary` when `cols` changes and `reflow=True` rejoins
     `wrapped=True` logical lines across `history + viewport` (eliding
     `WidthRole.SPACER_HEAD` wrap-padding cells), re-wraps them to the new
     column width while tracking cursor `(row, col)`, and pushes excess top
     rows into `FrozenRow` scrollback rather than truncating bottom lines.
   - Resizing `alternate` (`?1049`) crops/pads in place without reflow, matching
     full-screen TUI `SIGWINCH` redraw expectations; when `Mode.IN_BAND_RESIZE`
     (`?2048`) is enabled, `resize()` also queues `CSI 48 ; rows ; cols ; 0 ; 0 t`
     and emits `InBandResize`.

## Alternatives considered

- **Positional `Terminal(80, 24)` or `Terminal(columns=80, rows=24)`**: Rejected
  because mixing `(X, Y)` construction with `[row, col]` indexing invites silent
  axis transposition.
- **Exposing four `CoordSpace` enums and `StaleGridRefError` cell handles**:
  Rejected because immutable `Cell` values, `Snapshot`, and `term.history`
  provide safe inspection without runtime borrow-checker exceptions in Python.

## Consequences

### Positive

- Eliminating per-character `Cursor` allocation and `feed_until` 1-byte slicing
  removes the primary pure-Python CPU bottlenecks.
- Primary-screen `resize()` matches `tmux` and `Ghostty` soft-wrap reflow.
- Generation-tagged `?2026` expiry eliminates async timer races.

### Tradeoffs

- `reflow=True` unpacks wrapped lines during `cols` changes (`O(history + rows)`),
  which callers can bypass with `reflow=False` when batch-resizing.

### Risks

- Wide characters landing on `cols - 1` during reflow. Mitigation: emit
  `SPACER_HEAD` at `cols - 1` and wrap the 2-column glyph cleanly to column `0`
  of the next reflowed row.
