(adr-input-encoding)=

# ADR 0008: Mode-aware input encoding, Kitty keyboard stacks, and paste safety

## Status

Accepted. 2026-11-21.

## Context

Driving interactive PTY sessions (`tmux`, shells, editors, modal TUIs) requires
the outbound half of the terminal contract: encoding structured key, mouse,
focus, and paste events into the exact byte sequences negotiated by the child
application's active modes (`DECCKM` `?1`, mouse `?1000`/`?1002`/`?1003`/`?1006`
/`?1016`, focus `?1004`, bracketed paste `?2004`, and the Kitty Keyboard
Protocol `CSI u`).

Two defects commonly break terminal input encoders:

1. **Shared primary/alternate Kitty keyboard state**: Full-screen TUIs push
   Kitty keyboard flags (`CSI > 1 u`) on the `?1049` alternate screen and crash
   without popping (`CSI < u`); sharing one stack corrupts primary-shell key
   encoding after `?1049l`. Furthermore, unprefixed `CSI u` is `SCORC` (restore
   cursor) and MUST NOT be confused with `CSI ? u` / `CSI > u` / `CSI < u` /
   `CSI = u`.
2. **Single-pass `.replace("\x1b[201~", "")` bracketed-paste RCE bypass**:
   Sanitizing pasted text with `text.replace("\x1b[201~", "")` stitches
   `"\x1b[20\x1b[201~1~; id\r"` directly back into `"\x1b[201~; id\r"`,
   breaking out of bracketed paste and executing arbitrary shell commands! It
   also misses 8-bit `CSI` (`\x9b201~`) and raw `\r`/`\n` command execution
   when `?2004` bracketed paste is disabled.

## Decision

Provide a pure, zero-dependency `libvt.input` module integrated with
`Terminal.encode(event: InputEvent) -> bytes`:

1. **Typed input events (`libvt.input`)**:
   - `KeyEvent(key: str, modifiers: KeyMod = KeyMod.NONE, action: KeyAction =
     KeyAction.PRESS, text: str = "")` with `KeyEvent.parse("ctrl+shift+p")`
     shorthand.
   - `MouseEvent(button: MouseButton, action: MouseAction, row: int, col: int,
     modifiers: KeyMod = KeyMod.NONE, pixel_x: int = 0, pixel_y: int = 0)` using
     0-based `(row, col)` matching `Terminal`.
   - `FocusEvent(focused: bool)` (`\x1b[I` / `\x1b[O` when `?1004` is set, else
     `b""`).
   - `PasteEvent(text: str)`.
2. **Independent Primary and Alternate Kitty keyboard stacks**:
   - `ScreenSet` maintains separate 8-entry (`Limits.max_keyboard_stack`)
     `KittyKeyboardStack` instances on `primary` and `alternate`.
   - `CSI > flags u` pushes `flags`, `CSI < count u` pops `count` entries,
     `CSI = flags ; mode u` sets/ORs/clears `flags` (`mode` `1`=`set`, `2`=`or`,
     `3`=`not`), and `CSI ? u` queues `b"\x1b[?" + str(flags) + b"u"` into
     `drain_replies()`. Unprefixed `CSI u` remains `SCORC`.
   - `DECSTR` and `RIS` clear the active (or both) keyboard stacks.
   - When active `KittyKeyboardFlags` is non-zero, `encode_input` encodes
     disambiguated `CSI codepoint ; modifiers [: event_type] u` sequences;
     otherwise it encodes legacy xterm / `DECCKM` (`\x1bOA` vs `\x1b[A`) bytes.
3. **Fail-closed `encode_paste()` (`UnsafePasteError`)**:
   - By default (`sanitize=False`), `encode_paste(text, *, bracketed: bool)`
     raises `UnsafePasteError(LibVtError)` if `text` contains `ESC` (`\x1b`),
     8-bit `C1` (`\x80..\x9f`, including `\x9b`), `NUL` (`\x00`), `DEL`
     (`\x7f`), or C0 controls other than `\t` and (when `bracketed=True`) `\n`
     / `\r`. When `bracketed=False`, `\r` and `\n` also raise `UnsafePasteError`
     so multi-line paste never executes unbracketed shell lines silently.
   - When callers explicitly opt into `sanitize=True`, all C0/C1/`ESC`/`DEL`
     codepoints are stripped character-by-character in a single pass over
     codepoints (never substring `.replace`), which cannot re-stitch `\x1b[201~`.

## Alternatives considered

- **Silent `.replace("\x1b[201~", "")` sanitization**: Rejected due to the
  nested-substring stitching exploit (`"\x1b[20\x1b[201~1~"` -> `"\x1b[201~"`).
- **Encoding mouse events with 1-based `(x, y)` coordinates**: Rejected by ADR
  0004; `MouseEvent(row=0, col=0)` stays 0-based `(row, col)` in Python and adds
  `+ 1` on the wire (`\x1b[<btn;col+1;row+1M`).

## Consequences

### Positive

- `term.encode(KeyEvent.parse("up"))` automatically switches between `\x1b[A`,
  `\x1bOA` (`?1` `DECCKM`), and Kitty `CSI u` based on live terminal state.
- Bracketed paste injection is blocked fail-closed with a dedicated exception.

### Tradeoffs

- Callers pasting multiline text into a shell that has not enabled `?2004`
  must either send explicit `KeyEvent("enter")` / raw `os.write` or pass
  `allow_unbracketed_newlines=True` intentionally.

### Risks

- Mouse motion floods when `?1003` is off. Mitigation: `encode_input` checks
  active `?1000` / `?1002` / `?1003` / `?1006` / `?1016` modes and returns `b""`
  for unsubscribed mouse events.
