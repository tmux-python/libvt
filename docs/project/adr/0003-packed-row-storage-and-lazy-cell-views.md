(adr-packed-row-storage)=

# ADR 0003: Packed row storage and lazy cell views

## Status

Accepted. 2026-11-21.

## Context

Representing every terminal cell as an eager Python object (`dataclass` or
`NamedTuple` per `(row, col)`) allocates thousands of heap objects per
screen and hundreds of thousands across scrollback. Local measurements on
CPython 3.15 across an 80-column grid with `10,050` rows showed:

- Standard dict-backed `Cell` objects: `136.41 MiB` (`14,232` bytes/row).
- Slotted frozen `Cell` objects: `67.93 MiB` (`7,088` bytes/row).
- Packed `chars: list[str]` + `array.array("Q")` rows: `13.57 MiB` (`1,416`
  bytes/row), an order-of-magnitude reduction with zero `Cell` allocations
  on the `feed()` write path.

At the same time, Python callers inspecting cells in assertions want rich,
immutable value objects (`term[0, 0].text`, `term[0, 0].style.fg`,
`term[0, 0].hyperlink`).

## Decision

Separate internal row storage from public inspection views:

- **Internal `_Row` (`libvt._grid`)**:
  - `chars: list[str]` of length `cols`, storing `" "` for blank cells,
    single-character strings (immortalized by CPython for ASCII), native
    multi-codepoint grapheme clusters (`base + VS16` or combining marks),
    and `""` for wide-character spacer cells.
  - `attrs: array.array("Q")` of length `cols`, packing a 64-bit unsigned
    word where `0` represents a blank narrow cell with `DEFAULT_STYLE`:
    - bits `0..24` (`25` bits): foreground color (`0` = default,
      `1..16` = ANSI 16, `256..511` = 256-palette index,
      `(1 << 24) | 0xRRGGBB` = 24-bit RGB).
    - bits `25..49` (`25` bits): background color.
    - bits `50..56` (`7` bits): `bold`, `dim`, `italic`, `blink`,
      `reverse`, `hidden`, `strikethrough`.
    - bits `57..59` (`3` bits): `UnderlineStyle` (`NONE`, `SINGLE`,
      `DOUBLE`, `CURLY`, `DOTTED`, `DASHED`).
    - bits `60..61` (`2` bits): `WidthRole` (`NARROW`, `WIDE`,
      `SPACER_TAIL`, `SPACER_HEAD`).
    - bit `62` (`1` bit): `overline`.
    - bit `63` (`1` bit): `HAS_EXT`, gating a sparse `ext: dict[int, _CellExt]`
      for `OSC 8` hyperlinks, `SGR 58` underline color, and `OSC 133` prompt
      markers.
  - `wrapped: bool`: soft-wrap continuation flag (`DECAWM`).
- **Public lazy views (`libvt.screen`)**:
  - `term[row, col]` materializes a frozen `Cell` on demand (returning a
    shared `BLANK_CELL` singleton when `char == " "` and `attr == 0`).
  - `term[row]` returns a lightweight `RowView`; `term[r1:r2, c1:c2]`
    returns a 2D `RegionView` without copying the underlying grid until a
    `Snapshot` is requested.

## Alternatives considered

- **Eager `list[list[Cell]]` grid**: Simple to write, but allocates on every
  printed character and inflates scrollback memory by `5–10x`.
- **UTF-32 `array("I")` codepoint buffer**: Cannot store multi-codepoint
  grapheme clusters (`ZWJ`, `VS16`, combining marks) inline without a side
  table for every non-BMP or combining cell.

## Consequences

### Positive

- `feed()` mutates `chars[col]` and `attrs[col]` in place without allocating
  `Cell` or `Style` instances per character, caching packed 64-bit pen words
  per active `Style`.
- Scrollback history of `2,000–10,000` lines remains compact in pure Python.
- `Snapshot` cloning copies compact `list[str]` and `array("Q")` buffers per
  row (`O(rows)` C-level buffer copies).

### Tradeoffs

- Packing and unpacking 64-bit attribute words adds bitmask helpers in
  `libvt._grid` that must stay synchronized with `libvt.style.Style`.

### Risks

- Stale sparse `ext` entries when overwriting a cell that previously carried
  `HAS_EXT`. Mitigation: clear `ext.pop(col, None)` whenever a write, erase,
  or wide-character spacer split clears bit `63` at `col`.
