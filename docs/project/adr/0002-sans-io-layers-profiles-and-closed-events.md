(adr-sans-io-layers)=

# ADR 0002: Sans-I/O layering, Profile/Policy/Limits separation, and closed Events

## Status

Accepted. 2026-11-21.

## Context

A virtual terminal package serves four audiences with competing ergonomic and
performance requirements:

1. **One-line script and CLI callers** that want rendered plain text or
   ANSI-stripped text from a `bytes` capture (`libvt.render(raw)` /
   `libvt.strip(raw)`) without instantiating classes.
2. **Deterministic unit tests** (`term = Terminal(rows=24, cols=80);
   term.feed(raw)`) that inspect `term.text`, `term[row, col]`, and
   `term.cursor` with zero file descriptors, sockets, clocks, or threads.
3. **Interactive PTY harnesses** (`tmux attach-session`, shells, full-screen
   TUIs) that must flush bidirectional capability replies (`DA1`, `DA2`,
   `XTVERSION`, `DSR`, `DECRQM`, `DECRQSS`, `XTGETTCAP`, `OSC 7501 ; ?`,
   `CSI ? u`) back to the child PTY while blocking untrusted sequences from
   exfiltrating clipboard data or window titles.
4. **Protocol tools and host integrators** that subscribe to structured side
   effects (`Bell`, `TitleChanged`, `WorkingDirectoryChanged`, `ClipboardWrite`,
   `PromptMark`, `ProgramStatusChanged`, `SixelGraphic`, `KittyGraphic`,
   `ScreenErased`) with exhaustive static type narrowing.

Three anti-patterns surfaced when evaluating candidate designs:

- **Returning `list[Effect]` or `Update` from every `feed()` call**: Allocates
  wrapper lists/objects on 100% of chunks even when callers only inspect grid
  state at quiescence, and encourages callers to treat arbitrary `os.read()`
  chunk cuts as semantic transaction boundaries.
- **Mixing security policy booleans and event callbacks on a single `Effects`
  bag**, or exposing `ActionSink` methods (`on_print`, `on_csi`, `on_osc`)
  publicly on `Terminal`: Confuses IDE autocompletion between internal parser
  dispatch methods and host event hooks.
- **Splitting every CSI/OSC sequence into ~40 micro-modules and dispatching
  hot-path `feed()` via boxed `Token` -> `Command` dataclasses**: Inflates
  import time and object allocation on SGR/cursor-heavy streams.

## Decision

Organize `libvt` as a flat ~15-module Sans-I/O package with progressive
disclosure and explicit separation between terminal identity (`Profile`),
security gates (`Policy`), resource bounds (`Limits`), outbound wire replies
(`drain_replies()`), and semantic host events (`Event`):

1. **Progressive-disclosure entry points (`libvt`)**:
   - Level 0 pure helpers: `render(data, *, rows=24, cols=80) -> str`,
     `strip(data) -> str`, and `explain(data) -> tuple[ExplainedSpan, ...]`.
   - Level 1 stateful facade: `Terminal(*, rows=24, cols=80, ...)`.
   - Level 2 standalone building blocks: `Parser`, `parse_sgr`, `encode_input`,
     `Formatter`, `AsyncTerminalStream`, `open_pty`, and `assert_screen`.
2. **Three immutable configuration objects (`libvt.config`)**:
   - `Profile`: Terminal identity and compatibility behavior (`name`, `da1`,
     `da2`, `xtversion`, `osc_7501_capabilities`, `color_scheme`,
     `fg_rgb`, `bg_rgb`, `pending_wrap_phantom_col`), with presets
     `DEFAULT_PROFILE` (`XTERM_PROFILE`) and `TMUX_PROFILE`.
   - `Policy`: Default-deny host security gates (`allow_title_report=False`,
     `allow_checksum_report=False`, `allow_clipboard_read=False`,
     `allow_clipboard_write=False`, `allow_color_queries=True`,
     `allow_program_status_query=False`, `allow_unsafe_uris=False`,
     `unwrap_tmux_passthrough=True`, `strict=False`).
   - `Limits`: Hard memory and CPU bounds (`max_grapheme_codepoints=32`,
     `max_csi_params=32`, `max_osc_bytes=131_072`, `max_dcs_bytes=4_194_304`,
     `max_apc_bytes=4_194_304`, `max_reply_bytes=65_536`,
     `max_rep_count=4_096`, `max_prompt_zones=1_024`,
     `max_status_records=64`, `max_events=4_096`, `max_keyboard_stack=8`).
3. **Zero-allocation hot path with private `_TerminalSink`**:
   - `Terminal.feed(data, /) -> None` mutates terminal state in place through
     an internal `_TerminalSink` passed to `Parser.feed_to()`. Public
     `Terminal` attributes contain no `on_print` / `on_csi` plumbing methods.
   - Outbound wire replies accumulate in a `Limits.max_reply_bytes`-bounded
     `bytearray` drained via `term.drain_replies() -> bytes` (or forwarded via
     `on_pty_write` when attached).
4. **Closed algebraic `Event` union (`libvt.events`)**:
   - `type Event = Bell | TitleChanged | WorkingDirectoryChanged |
     ClipboardWrite | PromptMark | ProgramStatusChanged | RenderHoldChanged |
     ColorSchemeQueried | InBandResize | SixelGraphic | KittyGraphic |
     ScreenErased | UnhandledSequence`.
   - Callers either pass `on_event: Callable[[Event], None]` (push) or set
     `record_events: bool = True` and call `term.drain_events() ->
     tuple[Event, ...]` (pull). When neither is enabled, `feed()` allocates
     zero `Event` objects.

## Alternatives considered

- **`feed(data) -> list[Effect]` or `feed(data) -> Update`**: Rejected because
  allocating return containers on every `os.read()` chunk penalizes the 95%
  case where callers inspect `term.text` or `term.committed` after feeding.
- **Two-object `Screen` + `Stream(screen)` split**: Rejected because sans-I/O
  reply generation (`DA1`, `DSR`, `DECRQM`), dual-screen mode reconciliation,
  and `?2026` held snapshots span parser dispatch and screen state together.
- **40+ file per-sequence subpackage hierarchy**: Rejected per `AGENTS.md`
  file-boundary rules; keeping ~15 cohesive modules preserves fast imports and
  direct dispatch.

## Consequences

### Positive

- Callers match `Event` exhaustively (`case _: t.assert_never(ev)`) under `ty`,
  `mypy`, and `pyright`.
- `Policy`, `Profile`, and `Limits` separate security review from capability
  emulation and DoS budgets.
- `Terminal` keeps a small, discoverable method surface without leaking parser
  sink callbacks.

### Tradeoffs

- Callers wanting pull-based event inspection pass `record_events=True` (or an
  `on_event` callback) instead of receiving events implicitly from `feed()`.

### Risks

- Event queue growth when `record_events=True` and `drain_events()` is never
  called. Mitigation: store buffered events in a `collections.deque` bounded by
  `Limits.max_events`.
