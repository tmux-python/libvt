(topics-protocols-and-input)=

# Protocols & Input Encoding

## Program status (`OSC 7501`)

Applications use `OSC 7501` to report task status and progress. Read the
selected task through
{attr}`term.program_status <libvt.terminal.Terminal.program_status>`:

```python
>>> import libvt
>>> term = libvt.Terminal(rows=4, cols=20)
>>> term.feed(b"\x1b]7501;state=working:id=build:progress=60\x1b\\")
>>> term.program_status.value, term.program_status.progress
('working', 60)
```

Payloads carry `:`-joined `key=value` fields, with `title` and `msg` encoded as
UTF-8 base64. Nested tasks use `/`-delimited `id` paths (such as `ci/approve`)
and inherit `app` from the nearest ancestor on
{attr}`~libvt.status.ProgramStatus.effective_app`. When multiple tasks are
active, `term.program_status` selects the highest-urgency record (`blocked` >
`error` > `working` > `done` > `idle`), and ConEmu `OSC 9;4` progress updates
populate the root task until a native `OSC 7501` payload arrives:

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
{attr}`~libvt.config.Policy.allow_program_status_query` is `True` (the default)
and {attr}`~libvt.config.Profile.osc_7501_capabilities` is non-empty,
independent of whether `on_event` or `record_events=True` is attached.

## Semantic prompts (`OSC 133`)

`OSC 133` prompt markers record monotonic
{attr}`~libvt.events.PromptZone.abs_row` indices across scrollback eviction so
stored {attr}`~libvt.terminal.Terminal.prompt_zones` remain valid as output
scrolls:

```python
>>> term = libvt.Terminal(rows=2, cols=20, scrollback=10)
>>> term.feed(b"\x1b]133;A\x07$ \x1b]133;B\x07ls\r\nfile.py\r\n\x1b]133;D;0\x07")
>>> [(z.kind.value, z.exit_code) for z in term.prompt_zones]
[('A', None), ('B', None), ('D', 0)]
```

## Terminal queries and events

Queries (`DA1`/`DA2`, `DSR` `CSI 6 n`, `DECRQM`, `DECRQSS`, `XTVERSION`,
`XTGETTCAP`, `OSC 4` / `10`–`12` color queries, and `?2031` / `CSI ? 996 n`
color-scheme queries) write response bytes to
{meth}`~libvt.terminal.Terminal.drain_replies` according to active
{class}`~libvt.config.Profile` and {class}`~libvt.config.Policy` settings.
Notifications (`OSC 0`/`2` title, `OSC 7` working directory, `OSC 52`
clipboard, `OSC 133`, `OSC 7501`, `?2026`, and `DCS tmux;` passthrough) emit
typed {data}`~libvt.events.Event` records to `on_event` or
{meth}`~libvt.terminal.Terminal.drain_events`, while `OSC 8` hyperlinks attach
to {attr}`Cell.hyperlink <libvt.screen.Cell.hyperlink>`.

## Keyboard, mouse, and focus encoding

{func}`~libvt.input.encode_input` and {meth}`~libvt.terminal.Terminal.encode`
consult `DECCKM`, mouse modes (`?9`, `?1000`, `?1002`, `?1003`, `?1006`,
`?1007` {attr}`~libvt.modes.Mode.ALTERNATE_SCROLL`, and `?1016` pixel SGR),
focus reporting (`?1004`), bracketed paste (`?2004`), `XTMODKEYS`
(`CSI > 4 ; pv m` via {attr}`~libvt.terminal.Terminal.modify_other_keys`), and
the active screen's {class}`~libvt.input.KittyKeyboardStack`:

```python
>>> term = libvt.Terminal(rows=4, cols=20)
>>> term.feed(b"\x1b[?1h")
>>> term.encode(libvt.KeyEvent.parse("up"))
b'\x1bOA'
>>> term.feed(b"\x1b[>4;2m")
>>> term.encode(libvt.KeyEvent.parse("ctrl+;"))
b'\x1b[27;5;59~'
```

## Paste encoding

By default, {func}`~libvt.input.encode_paste` (and
{class}`~libvt.input.PasteEvent`) rejects disallowed control characters —
`ESC`, `NUL`, `DEL`, 8-bit `C1` controls (`U+0080`–`U+009F`), and other `C0`
controls — with {exc}`~libvt.exc.UnsafePasteError`. Tabs are permitted.
Newlines and carriage returns are permitted when bracketed paste (`?2004`) is
enabled.

`sanitize=True` removes disallowed characters.
`allow_unbracketed_newlines=True` permits newlines without bracketed paste:

```python
>>> from libvt.input import encode_paste
>>> encode_paste("a\x1b[2Jb", bracketed=True, sanitize=True)
b'\x1b[200~a[2Jb\x1b[201~'
>>> encode_paste("line 1\nline 2", bracketed=False, allow_unbracketed_newlines=True)
b'line 1\nline 2'
```
