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

1. **`STRING_ESC` 7-bit string termination and separated wire modes (`libvt.parser`)**:
   - `Parser(encoding="utf-8")` ignores raw `0x80..0x9F` 8-bit `C1` bytes and
     UTF-8-encoded `U+0080..U+009F` (`0xC2 0x80..0x9F`) as control introducers;
     `Parser(encoding="latin-1")` provides complete 8-bit DEC `C1` `anywhere`
     transitions for legacy Latin-1 byte streams.
   - `OSC`, `DCS`, and `APC`/`SOS`/`PM` transition on `0x1B` into a dedicated
     `ParserState.STRING_ESC` substate: `\` (`0x5C`) dispatches with `DCS`
     header intermediates intact, `0x1B` inside a latched `DCS tmux;` envelope
     collapses `ESC ESC` into one payload `ESC` byte, and any other byte aborts
     and discards the unclosed payload per DEC STD 070 §5.1.2.3 before
     re-evaluating that byte in `ESCAPE`.
   - Incomplete UTF-8 sequences flush `U+FFFD` *before* executing any C0
     control (`< 0x20`), `ESC`, `CAN` (`0x18`), or `SUB` (`0x1A`).
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
3. **SHA-256-pinned Unicode 17.0.0 two-stage block table (`libvt._ucd`)**:
   - `scripts/generate_ucd.py` compiles official SHA-256-verified Unicode 17.0.0
     UCD and Emoji data (`GraphemeBreakProperty.txt`, `DerivedCoreProperties.txt`,
     `EastAsianWidth.txt`, `DerivedGeneralCategory.txt`, `emoji-data.txt`, and
     `emoji-variation-sequences.txt`) into a zlib-compressed base85 2-stage
     256-codepoint `u16` block table in `libvt._ucd` (`~70.5 KiB` unpacked,
     zero runtime dependencies), validated in `pytest` against all official
     `GraphemeBreakTest.txt` vectors.
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
