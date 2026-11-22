(architecture)=

# Architecture

`libvt` separates byte stream parsing, external terminal side effects, and
grid state into three layers:

1. **{mod}`libvt.parser` (`Parser`)**: A 14-state VT500 byte parser that
   retains incomplete UTF-8 sequences and split escape sequences across
   arbitrary `os.read()` chunk boundaries. Parameter lists (`CsiParams`)
   track semicolon and colon subparameter positions via bitmasks, and
   oversized `CSI`, `OSC`, `DCS`, or `APC` sequences recover cleanly to
   `GROUND`.
2. **{class}`~libvt.terminal.Effects` and `drain_replies()`**: `Terminal`
   performs no socket or file-descriptor I/O. Safe capability and cursor
   queries (`DA1`, `DA2`, `DSR 5`, `DSR 6`, `DECRQM`) write bounded reply
   bytes read via {meth}`~libvt.terminal.Terminal.drain_replies`, while
   `OSC 133`, `OSC 7501`, `SIXEL`, Kitty graphics, and cell-erase events
   invoke optional `Effects` callbacks.
3. **{mod}`libvt.screen` and {mod}`libvt.terminal` (`Screen`, `Terminal`,
   `Snapshot`)**: Each internal row packs characters in `list[str]` and
   attributes in `array.array('Q')` (64-bit unsigned words), materializing
   frozen {class}`~libvt.screen.Cell`, {class}`~libvt.screen.RowView`, and
   {class}`~libvt.screen.RegionView` objects lazily on index access.

```python
>>> from libvt import Parser, Terminal
>>> parser = Parser()
>>> parser.feed(b"\x1b[1;3")
[]
>>> actions = parser.feed(b"2mHi")
>>> len(actions)
2
```
