(adr-osc-7501-and-prompts)=

# ADR 0007: Rex OSC 7501 program status, semantic prompts, and host queries

## Status

Accepted. 2026-11-21.

## Context

Modern CLI tools, build runners, and AI coding agents report structured task
progress via the Program Status protocol (`OSC 7501`), supported in `tmux`
([`input.c` at `cce5f10`](https://github.com/tmux/tmux/blob/cce5f10/input.c))
and `Ghostty` / `libghostty-vt`
([`terminal.zig` at `5b0690d`](https://github.com/ghostty-org/ghostty/blob/5b0690d/src/terminal/c/terminal.zig)),
alongside FinalTerm / shell-integration prompt markers (`OSC 133`), ConEmu/OSC
`9;4` progress bars, `OSC 7` working-directory notifications, `OSC 52`
clipboard writes, and startup capability queries (`XTVERSION`, `DECRQSS`,
`XTGETTCAP`, `OSC 10/11;?`, `?2031` / `CSI ? 996 n`).

Upstream sources exhibit a concrete behavioral divergence on the `OSC 7501 ; ?`
capability query:

- `tmux` (`cce5f10`) replies unconditionally with bare `"\033]7501;?\033\\"`.
- Full Rex `OSC 7501` registries advertise supported states and kinds:
  `"\033]7501;?:states=idle,working,done,blocked,error:kinds=permission,question,auth\033\\"`.
- `libghostty-vt` (`5b0690d`) suppresses the `OSC 7501 ; ?` reply unless a
  `program_status` effect consumer is attached, so applications avoid sending
  status updates nobody observes.

## Decision

1. **Spec-compliant `ProgramStatusStore` (`libvt.status`)**:
   - Enforce wire payload ceiling `<= 4,096` bytes before parsing.
   - Parse colon-delimited `[a-z]{1,16}=value` pairs:
     - `state`: `idle | working | done | blocked | error | clear` (required on
       non-query payloads).
     - `id`: optional slash-delimited hierarchical task identifier (`<= 128`
       bytes, `<= 8` `/` segments, `[A-Za-z0-9_.-]` segments; `""` = root).
     - `kind`: optional `permission | question | auth` (valid with
       `state=blocked`).
     - `progress`: optional integer `0..100`.
     - `app`: optional `[A-Za-z0-9_.+-]{1,32}`, inheriting `effective_app` from
       nearest ancestor `id` prefix when omitted on child tasks.
     - `title`: optional base64-encoded UTF-8 (`<= 256` encoded / `<= 192`
       decoded bytes); `msg`: optional base64-encoded UTF-8 (`<= 2,732` encoded
       / `<= 2,048` decoded bytes). Reject malformed base64, invalid UTF-8, C0
       controls (`< 0x20` except `\t` in `msg`), `DEL`/C1 (`0x7F..0x9F`), and
       Unicode BiDi overrides.
   - `state=clear` removes `id` and all `id/*` descendants (when `id == ""`,
     clears the entire store). Total active records stay bounded by
     `Limits.max_status_records` (`64`).
   - Expose `term.program_status` (most urgent active `ProgramStatus` ordered
     `blocked > error > working > done > idle`, or root record) and
     `term.program_statuses -> dict[str, ProgramStatus]`.
   - **Consumer-gated `OSC 7501 ; ?` reply**: Reply only when
     `Policy.allow_program_status_query` is `True` *or* an event consumer
     (`on_event` or `record_events=True`) is attached (matching `libghostty-vt`
     `5b0690d`), formatting the capability payload from
     `Profile.osc_7501_capabilities` (full `states=...:kinds=...` on
     `DEFAULT_PROFILE`, bare `?` on `TMUX_PROFILE`) and mirroring the incoming
     `BEL` (`\x07`) vs `ST` (`\x1b\\`) terminator.
   - **Lifecycle and `OSC 9;4` fallback**: Map `OSC 9;4;st;prog` into synthetic
     root status until the first native `OSC 7501` arrives (re-enabled on
     `RIS`). On `OSC 133;A` prompt start (ignoring `k=s` secondary continuation
     prompts) or `term.notify_process_exit()`, prune transient `working`,
     `blocked`, and `idle` entries while retaining `done` and `error`; survive
     `DECSTR` soft reset and clear all records on `RIS`.
2. **Monotonic `OSC 133` prompt zones and host queries (`libvt.terminal`)**:
   - Store `PromptZone(kind: PromptKind, row: int, abs_row: int, col: int,
     exit_code: int | None)` in a `collections.deque` bounded by
     `Limits.max_prompt_zones` (`1,024`), updating viewport `row` via
     `abs_row - scr.scrolled_total` so scrolling never desyncs prompt zones.
   - Support `OSC 7` and `OSC 9;9` (`term.cwd` + `WorkingDirectoryChanged`),
     `OSC 52` write (`ClipboardWrite` when `Policy.allow_clipboard_write` or
     recorded as event; `OSC 52 ; c ; ?` denied unless
     `Policy.allow_clipboard_read`), `XTVERSION` (`CSI > q` -> `DCS > | ... ST`),
     `DECRQSS` (`DCS $ q ... ST` for `m` SGR, `r` `DECSTBM`, `s` `DECSLRM`,
     ` q` `DECSCUSR`, `"q` `DECSCA`), `XTGETTCAP` (`DCS + q <hex> ST` for
     `TN`/`Co`/`RGB`/`Smulx`), `OSC 10;?` / `OSC 11;?` foreground/background
     queries, `?2031` + `CSI ? 996 n` color-scheme DSR, and `DCS tmux; ESC ESC
     ... ST` passthrough unwrapping (gated by `Policy.unwrap_tmux_passthrough`).

## Alternatives considered

- **Simplified `OSC 7501;value;progress` string split**: Rejected because it is
  wire-incompatible with `tmux`, `Ghostty`, and Rex CLI emitters.
- **Unconditional `OSC 7501 ; ?` reply when no consumer is listening**:
  Rejected in favor of `libghostty-vt`'s consumer/policy gate so passive text
  scrapers do not solicit unused status traffic.

## Consequences

### Positive

- `libvt` interoperates out of the box with `tmux` 3.6+/3.8+, `Ghostty`, and
  CLI tools emitting Rex `OSC 7501`, `OSC 9;4`, and `OSC 133`.
- Startup capability handshakes (`tmux attach`, Neovim, Fish, Helix) complete
  immediately via `drain_replies()` without 1-2 second query timeouts.

### Tradeoffs

- `ProgramStatusStore` validates base64 and UTF-8 bounds eagerly on `OSC 7501`
  frames (`<= 4 KiB`), dropping non-conformant fields safely.

### Risks

- Untrusted PTY output flooding `OSC 133;A` or `OSC 7501` unique IDs.
  Mitigation: `Limits.max_prompt_zones = 1_024` (`deque` FIFO) and
  `Limits.max_status_records = 64`.
