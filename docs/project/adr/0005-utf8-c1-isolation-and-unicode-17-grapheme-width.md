(adr-utf8-and-unicode-17)=

# ADR 0005: UTF-8/C1 isolation, grapheme caps, and Unicode 17 two-stage table

## Status

Accepted. 2026-11-21.

## Context

Adversarial testing and differential fuzzing uncovered four Unicode and byte
framing hazards in terminal emulators:

1. **UTF-8 continuation `0x9C` vs 8-bit `C1` `ST` escape-injection breakout**:
   In UTF-8, valid multi-byte characters including `“` (`U+201C` =
   `\xe2\x80\x9c`), `本` (`U+672C` = `\xe6\x9c\xac`), `Ü` (`\xc3\x9c`), and `😜`
   (`\xf0\x9f\x98\x9c`) contain byte `0x9C`. Treating raw `0x9C` as 8-bit
   String Terminator (`ST`) inside `OSC`, `DCS`, or `APC` strings truncates
   non-ASCII titles/hyperlinks mid-codepoint and executes the remaining payload
   directly in `GROUND` state — enabling escape-sequence injection past callers
   that filter `\x07` and `\x1b`.
2. **Incomplete UTF-8 ordering before C0 controls and combining-mark
   teleportation**: Flushing incomplete UTF-8 lead bytes only on `ESC` (and not
   C0 controls like `\n` or `\r`) emits `U+FFFD` on the wrong line, while
   retaining `_last_printed_pos` across cursor movements (`CUP`, `CUB`, `LF`),
   erases, scrolls, or `?1049` switches causes subsequent combining marks to
   mutate a stale cell elsewhere on screen.
3. **Single-cell combining-mark DoS ("grapheme bomb")**: Appending combining
   marks without a per-cell codepoint cap or scanning the growing cluster string
   on every mark causes quadratic CPU freeze (`10+ s` on 200 KB of `U+0301`)
   and multi-megabyte single-cell strings.
4. **Unicode version drift across Python 3.12-3.15 and missing UAX #29 rules**:
   Python's `unicodedata` module bundles Unicode 15.0 in 3.12, 15.1 in 3.13,
   16.0 in 3.14, and 17.0 in 3.15, and lacks `Extended_Pictographic`,
   `Regional_Indicator` pair clustering (`🇯🇵`, `🇺🇸`), and UAX #29 `GB9c` Indic
   `InCB` (`Linker` / `Consonant` / `Extend`) properties needed for DEC mode
   `?2027`.

## Decision

1. **7-bit string termination by default in UTF-8 streams (`libvt.parser`)**:
   - `Parser(c1_controls: bool = False)` ignores raw `0x80..0x9F` 8-bit `C1`
     bytes as control introducers/terminators unless `c1_controls=True` is
     explicitly requested for legacy 8-bit Latin-1 streams. `OSC` terminates on
     `BEL` (`0x07`) or 7-bit `ESC \` (`0x1B 0x5C`), and `DCS`/`APC`/`SOS`/`PM`
     terminate strictly on `ESC \`, matching `tmux` (`input.c`) and `Ghostty`.
   - Incomplete UTF-8 sequences flush `U+FFFD` *before* executing any C0
     control (`< 0x20`), `ESC`, `CAN` (`0x18`), or `SUB` (`0x1A`).
   - A second `ESC` inside `ESCAPE` when `_return_from_esc_st` is set aborts
     the pending control string cleanly before entering `ESCAPE` so `ESC ESC \`
     never dispatches a cancelled `OSC`.
2. **Strict `_last_printed_pos` invalidation and `O(1)` combining cap**:
   - Any cursor motion, C0 execution, erase, scroll, resize, reset, or screen
     switch clears `_last_printed_pos = None`.
   - `Terminal` tracks `_cluster_codepoints: int` and enforces
     `Limits.max_grapheme_codepoints` (default `32`), dropping excess zero-width
     extenders in `O(1)` time without re-scanning the cluster string.
   - When `VS16` (`\ufe0f`) widens a 1-column base glyph at `col == right`
     (right margin), `Terminal` replaces `col` with `SPACER_HEAD` (`""`),
     soft-wraps to the next line, and writes the widened 2-column cluster at
     `left` (`col 0`). `VS15` (`\ufe0e`) narrows default-wide emoji to `1`
     column under `?2027`.
3. **Generated two-stage Unicode 17.0 block table (`libvt._ucd`)**:
   - `scripts/generate_ucd.py` compiles Unicode 17.0 East Asian Width,
     `General_Category`, `Extended_Pictographic`, `Regional_Indicator`,
     variation selectors, and `InCB` (`Consonant`, `Linker`, `Extend`) into a
     compact zlib-compressed base85 2-stage 256-codepoint block table in
     `libvt._ucd` (`stage2[(stage1[cp >> 8] << 8) | (cp & 0xFF)]`, `~90 KiB`
     unpacked `bytes` in RAM, zero runtime dependencies).
   - Python 3.12, 3.13, 3.14, and 3.15 therefore compute identical cell widths
     and UAX #29 `?2027` cluster boundaries (`GB9`, `GB9a`, `GB9c` Indic
     conjuncts, `GB11` ZWJ pictographics, and `GB12`/`GB13` Regional Indicator
     pairs).

## Alternatives considered

- **Calling stdlib `unicodedata` directly at runtime**: Rejected because emoji
  and Indic conjunct properties (`Extended_Pictographic`, `InCB`) are absent
  from `unicodedata` in Python 3.12-3.14, and Unicode table versions drift
  across interpreter versions.
- **Runtime `wcwidth` / `grapheme` PyPI dependency**: Violates ADR 0001's
  `dependencies = []` invariant.

## Consequences

### Positive

- UTF-8 titles, hyperlinks, and status messages containing `0x9C` continuation
  bytes never break out of `OSC`/`DCS`/`APC` framing.
- Hostile combining-mark floods stay bounded at `<= 32` codepoints per cell in
  `O(1)` time per byte.
- Flag emoji (`🇯🇵`), ZWJ sequences (`🧑‍🌾`), variation selectors (`❤️`), and
  Devanagari conjuncts cluster identically on Python 3.12 through 3.15.

### Tradeoffs

- `src/libvt/_ucd.py` embeds `~14 KiB` of compressed base85 lookup data,
  regenerated via `scripts/generate_ucd.py` when upgrading Unicode versions.

### Risks

- BiDi override characters (`U+202A..U+202E`, `U+2066..U+2069`) spoofing
  visual test diagnostics. Mitigation: `libvt._ucd.sanitize_terminal_text()`
  escapes C0, C1, and BiDi override codepoints in diagnostic views and rejects
  them in `OSC 7501` status fields.
