(adr-charsets-and-mode-truth)=

# ADR 0006: G0-G3 charsets, selective erase, and single-truth mode reconciliation

## Status

Accepted. 2026-11-21.

## Context

Differential testing against `tmux` and `ncurses` applications exposed three
classic VT100/VT420 gaps that break real-world terminal UIs even when modern
sequences work:

1. **DEC Special Graphics (`ESC ( 0` / `SO` / `SI`)**: `tmux` (`tty-acs.c`),
   `ncurses`, `dialog`, `make menuconfig`, and `htop` designate `G0` or `G1` as
   DEC Special Graphics (`0`) and print `l q k x m j w u t v n` to draw
   `┌ ─ ┐ │ └ ┘ ┬ ┤ ├ ┴ ┼`. Dropping charset designations renders literal
   `lqqqk` ASCII letters instead of box-drawing glyphs.
2. **Tabulation and selective erase**: `CBT` (`CSI Ps Z` backward tab) and
   `CHT` (`CSI Ps I` forward tab) are standard horizontal cursor primitives,
   and VT220/VT420 applications use `DECSCA` (`CSI Ps " q`) with `DECSED`
   (`CSI ? Ps J`) and `DECSEL` (`CSI ? Ps K`) to clear unprotected fields while
   preserving protected form labels. Kitty-compatible shells also emit
   `CSI 22 J` to push the visible screen into scrollback before clearing.
3. **Alternate-screen `DECRQM` desynchronization (`?47` / `?1047` / `?1049`)**:
   Tracking `ALT_SCREEN_47`, `ALT_SCREEN_1047`, and `ALT_SCREEN` (`?1049`) as
   independent booleans in `ModeState` causes `CSI ? 47 h` followed by
   `CSI ? 1049 l` (or `?1049h` followed by `?47l`) to return to the primary
   screen while `CSI ? 47 $ p` falsely replies `1` (`SET`) instead of `2`
   (`RESET`), and `Formatter(OutputFormat.VT)` re-emits `\x1b[?47h`!

## Decision

1. **Per-screen `G0`-`G3` charset slots and shifts (`libvt.terminal`)**:
   - Track four charset slots (`G0`, `G1`, `G2`, `G3`, each `'B'` US-ASCII,
     `'0'` DEC Special Graphics, or `'A'` UK), active locking-shift slot
     `gl: int` (`0` via `SI` `0x0F`, `1` via `SO` `0x0E`, `2` via `LS2`
     `ESC n`, `3` via `LS3` `ESC o`), and one-shot single shift
     `single_shift: int | None` (`2` via `SS2` `ESC N`, `3` via `SS3` `ESC O`).
   - Designate slots via `ESC ( F` (`G0`), `ESC ) F` (`G1`), `ESC * F` (`G2`),
     and `ESC + F` (`G3`). When the active slot for the next printed character
     is `'0'`, map `0x5F..0x7E` through the 32-entry DEC Special Graphics table
     (`┌`, `─`, `┐`, `│`, `└`, `┘`, `┼`, `◆`, `▒`, `°`, `±`, `≤`, `≥`, `π`,
     `≠`, `£`, `·`, etc.) before writing to `_Row`.
   - Save and restore `(g_sets, gl)` inside `_SavedCursor` (`DECSC` / `DECRC` /
     `?1049`) and reset to `('B', 'B', 'B', 'B'), gl=0` on `DECSTR` and `RIS`.
2. **Tab navigation, selective erase, and scroll-clear**:
   - Implement `CHT` (`CSI Ps I`: advance `Ps` tabstops clamped to right margin)
     and `CBT` (`CSI Ps Z`: retreat `Ps` tabstops clamped to left margin,
     clearing `pending_wrap`). Keep sorted tabstops cached on `Screen`.
   - Implement `DECSCA` (`CSI 0 " q` / `CSI 2 " q` unprotected, `CSI 1 " q`
     protected) stored on `_CellExt.protected`, respected by `DECSED`
     (`CSI ? Ps J`) and `DECSEL` (`CSI ? Ps K`) while `ED` / `EL` erase
     unconditionally.
   - Support `CSI 22 J` (push non-blank primary viewport lines into `FrozenRow`
     scrollback before clearing the viewport).
3. **Single-truth alternate-screen reconciliation (`libvt.modes`)**:
   - `ScreenSet.active_is_alt` is the sole source of truth for whether the
     alternate buffer is active. Exiting via *any* of `?47l`, `?1047l`, or
     `?1049l` clears all three alt-screen mode bits in `ModeState`, and
     `ModeState.decrpm()` reconciles `?47`, `?1047`, and `?1049` against
     `active_is_alt` so `DECRQM` and `Formatter(OutputFormat.VT)` never report
     stale alt-screen state.

## Alternatives considered

- **Ignoring `ESC ( 0` in UTF-8 mode**: Rejected because `ncurses`, `dialog`,
  and `tmux` still emit `ESC ( 0` whenever `acsc` terminfo capabilities are
  active.
- **Reporting `SET` for `?47` whenever `?1049` is active**: Keep `?1049` as the
  canonical bit set when entering via `?1049h` (and `?47` when entering via
  `?47h`), while clearing *all* three bits whenever *any* exit sequence returns
  `active_is_alt` to `False`.

## Consequences

### Positive

- `ncurses` and `tmux` ACS box-drawing borders render as real Unicode box
  characters (`┌───┐`) in `term.text` and `assert_screen()`.
- `?47` / `?1047` / `?1049` transitions never leave ghost mode flags in
  `DECRQM` replies or `VT` snapshot exports.

### Tradeoffs

- `on_print`'s ASCII slice fast path runs unhindered when the active charset is
  `'B'` (99%+ of traffic) and translates via `str.translate` when `'0'` or `'A'`
  is active.

### Risks

- Applications leaving `SO` (`0x0E` -> `G1 = '0'`) latched after crashing.
  Mitigation: `DECSTR` (`CSI ! p`), `RIS` (`ESC c`), and `sgr0` (`ESC ( B`
  emitted by `tput sgr0`) restore `G0` US-ASCII.
