(migrating-from-pyte)=

# Migrating from `pyte`

`libvt` is a clean-room MIT library designed for Python 3.12+ test suites
and terminal multiplexers upgrading from `pyte`.

| Concept | `pyte` | `libvt` |
|---|---|---|
| License | LGPL-3.0 | MIT |
| Construction | `Screen(cols, lines)` + `ByteStream(screen)` | `Terminal(rows=24, cols=80)` |
| Feeding bytes | `stream.feed(b"...")` | `term.feed(b"...")` / `term.feed_str("...")` |
| Cursor coordinates | `screen.cursor.y`, `screen.cursor.x` | `term.cursor.row`, `term.cursor.col` |
| Visible lines | `screen.display` | `term.lines` / `term.text` |
| Cell access | `screen.buffer[row][col]` | `term[row, col]` (`Cell`) |
| Synchronized output (`?2026`) | Not supported | `term.committed` / `StopOn.SYNC_END` |
| Hyperlinks (`OSC 8`) | Not supported | `term[r, c].hyperlink` (survives `SGR 0`) |
