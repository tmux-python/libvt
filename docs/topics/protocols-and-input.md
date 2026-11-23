(topics-protocols-and-input)=

# Protocols & Input Encoding

## Rex `OSC 7501` Program Status

`libvt` implements the Rex `OSC 7501` Program Status grammar (`:`-joined
`key=value` pairs, hierarchical `/` task identifiers, ancestor `app`
inheritance, base64 `title` and `msg`, and `OSC 9;4` ConEmu progress
fallback):

```python
>>> import base64, libvt
>>> term = libvt.Terminal(rows=4, cols=30, record_events=True)
>>> title_b64 = base64.b64encode(b"Running pytest").decode("ascii")
>>> term.feed(
...     f"\x1b]7501;state=working:id=ci:app=pytest:progress=75:title={title_b64}\x1b\\".encode()
... )
>>> term.feed(b"\x1b]7501;state=blocked:id=ci/approve:kind=permission\x1b\\")
>>> term.program_status.id, term.program_status.effective_app
('ci/approve', 'pytest')
```

`OSC 7501 ; ?` capability queries answer whenever
`Policy.allow_program_status_query` is `True` (the default) and
`Profile.osc_7501_capabilities` is non-empty, independent of whether
`on_event` or `record_events=True` is attached.

## Semantic prompts (`OSC 133`) and queries

`OSC 133` prompt markers record monotonic `abs_row` indices across
scrollback eviction so stored prompt zones remain valid as output
scrolls:

```python
>>> term = libvt.Terminal(rows=2, cols=20, scrollback=10)
>>> term.feed(b"\x1b]133;A\x07$ \x1b]133;B\x07ls\r\nfile.py\r\n\x1b]133;D;0\x07")
>>> [(z.kind.value, z.exit_code) for z in term.prompt_zones]
[('A', None), ('B', None), ('D', 0)]
```

## Outbound input encoding and paste safety

{func}`~libvt.input.encode_input` and {meth}`~libvt.terminal.Terminal.encode`
consult `DECCKM`, mouse modes (`?9`, `?1000`, `?1002`, `?1003`, `?1006`,
`?1016`), focus reporting (`?1004`), bracketed paste (`?2004`), and the
active screen's Kitty `CSI u` keyboard stack:

```python
>>> term = libvt.Terminal(rows=4, cols=20)
>>> term.feed(b"\x1b[?1h")
>>> term.encode(libvt.KeyEvent.parse("up"))
b'\x1bOA'
```

{func}`~libvt.input.encode_paste` fails closed with
{exc}`~libvt.exc.UnsafePasteError` whenever pasted text contains `ESC`,
8-bit `C1` bytes, `NUL`, `DEL`, or unbracketed newlines rather than
relying on single-pass substring replacement.
