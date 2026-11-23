(adr-two-tier-storage)=

# ADR 0003: Two-tier lazy packed viewport rows and RLE FrozenRow scrollback

## Status

Accepted. 2026-11-21.

## Context

Grid and scrollback storage in pure Python faces three simultaneous traps:

1. **Object-per-cell heap blowup**: Representing a `10,000 x 80` scrollback
   buffer as `list[list[Cell]]` (800,000 frozen slotted dataclasses) consumes
   `56.46 MiB` of traced CPython 3.15 heap (`>130 MiB` without `__slots__`)
   and amplifies garbage-collection pause times.
2. **Pooled `StyleId` RGB leaks and `u16` overflows**: Designs that intern
   `Style` objects into a `StylePool` indexed by `array("I")` (`u32`) or
   `array("H")` (`u16`) break on 24-bit RGB (`SGR 38;2;r;g;b`) streams
   (`btop`, `chafa`, `timg`, smooth fades, or hostile PTYs). Without eviction,
   16.7 million RGB combinations leak gigabytes of `Style` objects across
   `RIS`; with `array("H")` (`u16`), style `#65,536` (reached in `<50 ms` of
   `240x80` truecolor frames) raises `OverflowError` or requires expensive
   stop-the-world reference counting across active and historical cells.
3. **Unwritten-blank vs space vs wide-spacer ambiguity**: Using `""` for
   unwritten blank cells causes cursor jumps (`CSI 1;10 H` + `"OK"`) to
   collapse interior columns when joining cell text (`"OK"` instead of
   `"         OK"`) and collides with `""` on wide-character `SPACER_TAIL`
   cells. Conversely, scanning backward across `" "` cells during `EL 0`
   (`CSI K`) must never erase explicit `" "` characters printed to the left of
   the erase start column.

## Decision

Adopt a two-tier memory architecture that uses **inline 64-bit bit-packed
attribute words** (zero `StylePool`) across both hot mutable viewport rows and
cold run-length-encoded (`RLE`) scrollback lines:

1. **Hot viewport `_Row` (`libvt._grid`)**:
   - `chars: list[str]` of length `cols`: `" "` for narrow blank or erased
     cells, single-codepoint or multi-codepoint grapheme strings for printed
     cells, and `""` reserved strictly for `WidthRole.SPACER_TAIL` and
     `WidthRole.SPACER_HEAD`.
   - `used: int`: High-water column index (`0..cols`) of explicitly written or
     non-default `BCE`-erased content. `clear_range(lo, cols, bce_word=0)`
     clamps `used = min(used, lo)` without scanning left past `lo`, preserving
     explicit spaces printed left of `lo`.
   - **Lazy 64-bit `attrs`**: Starts as `attrs: array.array[int] | None = None`
     with scalar `uniform_attr: int = 0` and `has_wide: bool = False`. Plain
     default-styled ASCII writes slice-assign `chars[col:end] = list(seg)`
     with zero `array("Q")` allocation. On the first non-uniform style,
     wide glyph, protected attribute, or `HAS_EXT` cell, `_Row` promotes
     `attrs = array.array("Q", [uniform_attr]) * cols`.
   - **64-bit inline bit layout (`0` == blank default narrow unprotected cell)**:
     - bits `0..24` (`25`b): foreground color (`0` = default, `1..16` =
       `AnsiColor`, `256..511` = `IndexedColor(0..255)` keeping `0..15`
       distinct from `AnsiColor`, `(1 << 24) | 0xRRGGBB` = `RgbColor`).
     - bits `25..49` (`25`b): background color (same encoding).
     - bits `50..56` (`7`b): `bold`, `dim`, `italic`, `blink`, `reverse`,
       `hidden`, `strikethrough`.
     - bits `57..59` (`3`b): `UnderlineStyle` (`0..5`).
     - bits `60..61` (`2`b): `WidthRole` (`NARROW=0`, `WIDE=1`,
       `SPACER_TAIL=2`, `SPACER_HEAD=3`).
     - bit `62` (`1`b): `overline`.
     - bit `63` (`1`b): `HAS_EXT`, gating lazy `ext: dict[int, _CellExt] | None`
       holding `underline_color: Color`, `hyperlink: Hyperlink | None`, and
       `protected: bool` (`DECSCA`).
2. **Cold scrollback `FrozenRow` (`libvt._grid`) and `_Row` recycling**:
   - When `Screen.scroll_up` evicts a full-width top row into `history`, it
     seals the row into an immutable `FrozenRow(text: str, cols: int,
     runs: tuple[tuple[int, int], ...], exts: tuple[tuple[int, _CellExt], ...],
     wrapped: bool)` where `runs` stores `(end_col, attr_u64)` RLE spans
     (sharing a singleton `()` tuple when completely default-styled) and
     recycles the cleared `_Row` instance to the bottom of the viewport.
   - Measured on CPython 3.15.1 (`10,000 x 80` styled CLI log rows),
     `FrozenRow` RLE scrollback consumes `~2.66 MiB` vs `22.59 MiB` for cloned
     mutable `_Row`s (`8.5x` reduction) and `56.46 MiB` for `Cell` grids
     (`21.2x` reduction) — with zero `zstd` dependency and zero `StylePool`
     leak.
3. **Unified public `RowView`, `RowRun`, and `Cell` views (`libvt.screen`)**:
   - `RowView` wraps either a live `_Row` or a `FrozenRow`, exposing `.text`,
     `.wrapped`, `row[col] -> Cell` (returning `BLANK_CELL` when default), and
     `.runs -> tuple[RowRun, ...]` for `O(runs)` styled span rendering.
   - Primary scrollback stores `FrozenRow` records in `256`-row copy-on-write
     tuple blocks (`_FrozenHistory` / `_HistoryView`), so `Screen.history`,
     `Terminal.history`, and `Snapshot.history` satisfy
     `collections.abc.Sequence[RowView]` with `O(1)` snapshot capture and
     `O(1)` random row indexing without leaking mutable storage.

## Alternatives considered

| Storage model | `10,000 x 80` heap (Py 3.15) | 24-bit RGB safety | Multi-codepoint graphemes | Verdict |
|---|---|---|---|---|
| Lazy 64-bit `_Row` + RLE `FrozenRow` (chosen) | `~2.66 MiB` | inline 64b (leak-free) | native `str` per cell | Adopted |
| Uncompressed cloned `_Row` scrollback | `22.59 MiB` | inline 64b (leak-free) | native `str` per cell | `8.5x` heavier scrollback |
| `array("I")` codepoint + `u32` `StylePool` | `11.04 MiB` + pool | leaks unbounded RGB styles | side-dict per cluster | OOM on RGB streams |
| `CompactRow` `array("H")` `u16` `StyleId` | `~1.7-3.0 MiB` + pool | `OverflowError` at 65,536 styles | native `str` per cell | Crashes/corrupts on truecolor |
| `list[list[Cell]]` grid | `56.46 MiB` | per-cell objects | native `str` per cell | High RAM and GC pressure |

## Consequences

### Positive

- Truecolor animations (`16.7M` colors) allocate zero persistent style-table
  entries and never overflow 16-bit IDs.
- Unstyled ASCII logs skip `array("Q")` allocation on active rows and compress
  to one `str` + shared empty tuple in scrollback, recycling `_Row` buffers on
  every scroll.
- `IndexedColor(0..15)` (`SGR 38;5;0..15`) round-trips distinctly from
  `AnsiColor(0..15)` (`SGR 30..37`, `90..97`).

### Tradeoffs

- Random cell indexing into `FrozenRow` (`history[i][col]`) performs a short
  tuple scan (`bisect` / linear over typically 1-6 RLE spans) rather than
  `O(1)` array indexing.

### Risks

- Wide-character `WIDE` / `SPACER_TAIL` pairing corruption during `ICH`
  (`CSI @`) or `DCH` (`CSI P`). Mitigation: heal split wide pairs *after* or at
  the exact cut boundaries (`col` when `col` is `SPACER_TAIL`, and `right` when
  a shifted `WIDE` head loses its tail at the right margin), verified by
  400+ randomized fuzz trials.
