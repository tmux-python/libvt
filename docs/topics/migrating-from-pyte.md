(topics-migrating-from-pyte)=

# Migrating from `pyte`

Moving from `pyte` changes how you construct the terminal, feed output, and
inspect its state. This guide covers those API differences, alternate screens,
and terminal replies.

## Dimension order and coordinates

`libvt` uses keyword-only `(rows, cols)` dimensions and zero-based `(row, col)`
indexing everywhere:

```python
>>> import libvt
>>> term = libvt.Terminal(rows=24, cols=80)
>>> term.feed(b"\x1b[2;5Hhello")
>>> term.cursor.row, term.cursor.col
(1, 9)
>>> term[1, 4].text
'h'
```

## Single byte-stream entry point

Instead of pairing a separate `pyte` stream with a screen object, feed `bytes`,
`bytearray`, or {class}`memoryview` directly to {meth}`libvt.Terminal.feed`
(or UTF-8 strings via {meth}`libvt.Terminal.feed_str`):

```python
>>> term = libvt.Terminal(rows=4, cols=20, scrollback=500)
>>> term.feed(b"first\r\n\x1b[?1049hfullscreen\x1b[?1049l")
>>> term.text
'first'
```

## Bidirectional replies and side-effect events

Terminal queries (`CSI 6 n`, `CSI c`, `DECRQM`, `DECRQSS`, `XTVERSION`,
`XTGETTCAP`, `OSC 10/11 ; ?`) accumulate in
{meth}`libvt.Terminal.drain_replies` rather than being dropped, and structured
notifications (`OSC 0/2`, `OSC 7`, `OSC 52`, `OSC 133`, `OSC 7501`, `?2026`)
flow through `on_event` or {meth}`libvt.Terminal.drain_events`.
